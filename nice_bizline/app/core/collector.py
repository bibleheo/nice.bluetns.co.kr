"""나이스비즈라인 수집기.

- BaseCollector: 인터페이스 (공식 API 전환 시 교체 지점)
- NiceBizlineCollector: Playwright 기반 수집기
- MockCollector: 사이트 접근 없이 파이프라인 검증용

실제 사용 전 config.yaml의 selectors를 실제 페이지 DOM으로 채워야 합니다.
"""
from __future__ import annotations

import random
import re
import time
from typing import Protocol

from .matcher import CORP_FORM_RE
from .normalizer import amount_to_millions
from .timeutil import now_seoul


class CollectorError(Exception):
    pass


class LoginRequired(CollectorError):
    """세션 만료로 로그인 페이지로 튕긴 경우."""


class BaseCollector(Protocol):
    def login(self, user_id: str, password: str) -> None: ...
    def search(self, company_name: str) -> list[dict]: ...
    def fetch_detail(self, candidate: dict, finance_years: int = 1) -> dict: ...
    def extend_session(self) -> bool: ...
    def is_login_page(self) -> bool: ...
    def close(self) -> None: ...


# ── 실 수집기 (Playwright) ────────────────────────────────────────────────────


class NiceBizlineCollector:
    """Playwright 기반 나이스비즈라인 수집기.

    셀렉터는 config['selectors']에서 모두 외부 주입.
    재무 권한이 없는 항목은 None으로 반환 (호출자가 비고에 사유 기록).
    """

    def __init__(self, config: dict):
        # 지연 import: GUI만 띄울 때는 Playwright가 없어도 됩니다.
        from playwright.sync_api import sync_playwright

        self._cfg = config
        self._pw = sync_playwright().start()
        browser_cfg = config.get("browser", {})
        self._browser = self._pw.chromium.launch(headless=browser_cfg.get("headless", True))
        ctx_kwargs = {}
        if ua := browser_cfg.get("user_agent"):
            ctx_kwargs["user_agent"] = ua
        self._context = self._browser.new_context(**ctx_kwargs)
        self._page = self._context.new_page()
        self._page.set_default_timeout(int(config["timing"].get("page_timeout_sec", 30)) * 1000)

    def close(self) -> None:
        try:
            self._context.close()
            self._browser.close()
        finally:
            self._pw.stop()

    # ── 로그인 ──
    def login(self, user_id: str, password: str) -> None:
        sel = self._cfg["selectors"]["login"]
        self._page.goto(self._cfg["site"]["login_url"])
        # 세션이 아직 살아있으면 로그인 폼 대신 '나의정보' 마커가 바로 보인다.
        if self._visible(sel["logged_in_marker"], timeout=4000):
            return
        # 최대 2회: 1차 제출 후 동시접속 팝업으로 기존 접속을 끊고, 필요 시 재제출.
        for _ in range(2):
            if not self._visible(sel["id_input"], timeout=4000):
                break
            self._page.fill(sel["id_input"], user_id)
            self._page.fill(sel["pw_input"], password)
            self._page.click(sel["submit_btn"])
            self._dismiss_concurrent_popup()
            if self._visible(sel["logged_in_marker"], timeout=10000):
                return
        raise CollectorError(
            "로그인 후 '나의정보' 미노출 (동시접속 팝업 처리 실패 가능)")

    def _dismiss_concurrent_popup(self) -> bool:
        """동시접속 제한(1명) 팝업 처리: 접속 종료 → 예 → 확인 순으로 기존 접속 종료.

        각 버튼이 화면에 나타날 때까지 기다렸다 누른다(다이얼로그가 순차 등장).
        팝업이 없으면 아무것도 안 하고 False.
        """
        sess = self._cfg["selectors"].get("session", {}) or {}
        steps = sess.get("disconnect_steps") or ["접속 종료", "예", "확인"]
        if not steps:
            return False
        # 첫 버튼(접속 종료)이 안 뜨면 팝업이 없는 것
        try:
            self._page.get_by_role("button", name=steps[0], exact=True).first.wait_for(
                state="visible", timeout=5000)
        except Exception:
            return False
        for name in steps:
            try:
                btn = self._page.get_by_role("button", name=name, exact=True).first
                btn.wait_for(state="visible", timeout=6000)
                btn.click(timeout=5000)
            except Exception:
                pass
            self._page.wait_for_timeout(600)   # 다음 단계 다이얼로그 등장 대기
        return True

    def _visible(self, selector: str, timeout: int = 4000) -> bool:
        try:
            self._page.wait_for_selector(selector, timeout=timeout, state="visible")
            return True
        except Exception:
            return False

    # ── 세션 연장 ──
    def extend_session(self) -> bool:
        """'로그인 연장' 버튼을 눌러 세션을 10분 연장. 성공 시 True.

        재로그인(중복 로그인 팝업 등으로 실패)을 피하기 위한 기본 유지 방식.
        버튼이 현재 화면에 없으면 False (호출측이 재로그인으로 폴백).
        """
        btn_sel = (self._cfg["selectors"].get("session", {}) or {}).get(
            "extend_btn") or "button:has-text('로그인 연장')"
        if not self._visible(btn_sel, timeout=2000):
            return False
        try:
            self._page.click(btn_sel, timeout=3000)
            return True
        except Exception:
            return False

    # ── 검색 (페이지네이션) ──
    def search(self, company_name: str) -> list[dict]:
        self._delay()
        sel = self._cfg["selectors"]["search"]
        # SPA 검색이 간헐적으로 결과를 늦게/안 채우는 경우가 있어 0건이면 1회 재시도
        for attempt in (1, 2):
            self._open_search(company_name)
            if attempt == 2:
                self._page.wait_for_timeout(2000)   # 재시도는 더 여유 있게
            out: list[dict] = []
            for row in self._each_result_row(sel):
                # 기업명 버튼이 없는 행은 결과 행이 아님 (다른 표 오인 방지)
                if not row.query_selector(sel["result_detail_btn"]):
                    continue
                name = _text(row, sel["result_company_name"])
                if not name:
                    continue
                out.append({
                    "회사명": name,
                    "기업유형": _text(row, sel.get("result_type") or ""),
                    "사업자번호": _text(row, sel["result_biz_no"]),
                    "대표자명": _text(row, sel["result_ceo"]),
                    "주소": _text(row, sel["result_address"]),
                    "업종": _text(row, sel.get("result_industry") or ""),
                })
            if out:
                return out
            if attempt == 1:
                # 빈 결과: 홈으로 이동해 SPA 컨텍스트를 완전히 초기화 후 재시도
                # (검색창은 어느 화면에나 있어, 잘못된 화면에서 검색하면 계속 0건이 됨)
                try:
                    self._page.goto(self._cfg["site"].get("base_url")
                                    or self._cfg["site"]["search_url"])
                except Exception:
                    pass
                self._page.wait_for_timeout(1500)
        return []

    def _open_search(self, company_name: str) -> None:
        """검색창을 확보한 뒤 회사명을 입력하고 검색을 실행.

        검색 URL 경로 토큰이 세션마다 바뀔 수 있으므로, 현재 페이지에 검색창이
        있으면 그대로 쓰고, 없으면 search_url → base_url 순으로 이동해 확보한다.
        입력 상호의 (주)/주식회사 등 법인격 표기는 검색 정확도를 위해 제거한다.
        """
        sel = self._cfg["selectors"]["search"]
        q = sel["query_input"]
        company_name = _search_term(company_name) or company_name
        if not self._search_box_ready(q):
            for url in (self._cfg["site"].get("search_url"),
                        self._cfg["site"].get("base_url")):
                if not url:
                    continue
                self._page.goto(url)
                if self.is_login_page():
                    raise LoginRequired()
                if self._search_box_ready(q):
                    break
        if not self._search_box_ready(q):
            raise CollectorError("검색창을 찾지 못함")

        self._page.fill(q, company_name)
        # SPA 검색 실행: 검색 아이콘 클릭 우선, 실패 시 Enter
        btn = self._page.query_selector(sel.get("submit_btn") or "")
        if btn:
            try:
                btn.click()
                return
            except Exception:
                pass
        self._page.press(q, "Enter")

    def _search_box_ready(self, selector: str, timeout: int = 3000) -> bool:
        try:
            self._page.wait_for_selector(selector, timeout=timeout, state="visible")
            return True
        except Exception:
            return False

    def _set_page_size_40(self) -> None:
        """검색 결과 '페이지 당 목록 수'를 40으로 변경 (동명 다수를 한 페이지에서 처리)."""
        try:
            footer = self._page.locator(".v-data-footer__select").first
            if footer.count() == 0:
                return
            cur = footer.locator(".v-select__selection").first
            if cur.count() and cur.inner_text(timeout=1000).strip() == "40":
                return
            footer.locator("div[role='button']").first.click(timeout=2000)
            self._page.locator(".v-menu__content .v-list-item",
                               has_text="40").first.click(timeout=3000)
            self._page.wait_for_timeout(800)   # 표 재렌더 대기
        except Exception:
            pass

    def _each_result_row(self, sel):
        """결과 표의 행을 페이지네이션 한도까지 순회하며 yield."""
        max_pages = int(self._cfg.get("timing", {}).get("max_search_pages", 1))
        next_sel = sel.get("next_page_btn") or ""
        for page_num in range(1, max(1, max_pages) + 1):
            try:
                self._page.wait_for_selector(sel["result_rows"], timeout=5000)
            except Exception:
                return
            if page_num == 1:
                self._set_page_size_40()
            for row in self._page.query_selector_all(sel["result_rows"]):
                yield row
            if not next_sel or page_num >= max_pages:
                return
            nxt = self._page.query_selector(next_sel)
            if not nxt:
                return
            self._delay()
            try:
                nxt.click()
            except Exception:
                return

    # ── 상세 파싱 ──
    # 상세는 검색 결과에서 '개요' 클릭 시 같은 페이지에 인라인 렌더된다(href 없음).
    def fetch_detail(self, candidate: dict, finance_years: int = 1) -> dict:
        self._delay()
        # 검색 결과 행에 이미 있는 기본정보로 시작 → 상세 진입이 실패해도 이건 확보.
        base = {
            "회사명": candidate.get("회사명"),
            "사업자번호": candidate.get("사업자번호"),
            "대표자": candidate.get("대표자명"),
            "주소": candidate.get("주소"),
            "업종": candidate.get("업종"),
            "설립일": None, "대표번호": None, "종업원수": None, "휴폐업정보": None,
            "매출액": None, "영업이익": None, "당기순이익": None,
            "신용등급": None, "결산일자": None,
        }
        # 상세 진입은 SPA 재렌더로 불안정 → 실패 시 1회 재시도
        detail = None
        for _ in range(2):
            try:
                self._open_detail(candidate)
                detail = self._parse_detail(candidate)
                break
            except LoginRequired:
                raise
            except CollectorError:
                detail = None
        # 상세에서 얻은 값으로 보강(빈 값은 base 유지)
        if detail:
            for k, v in detail.items():
                if v not in (None, ""):
                    base[k] = v
        else:
            base["_detail_failed"] = True   # 결과 비고에 '상세 미진입' 표기용
        return base

    def _open_detail(self, candidate: dict) -> None:
        sel = self._cfg["selectors"]["search"]
        dsel = self._cfg["selectors"]["detail"]
        target_biz = _digits(candidate.get("사업자번호"))
        target_name = _norm_name(candidate.get("회사명"))

        # 상세 진입 전 항상 새로 검색해 깨끗한 결과 목록에서 대상 행을 찾는다.
        # (직전에 상세를 본 뒤 목록이 사라지거나 어긋나 '행 못 찾음'이 나던 문제 방지)
        self._open_search(candidate.get("회사명") or "")
        # 결과 표가 비동기로 채워질 시간을 잠깐 준다(클릭 레이스 방지)
        self._page.wait_for_timeout(800)

        max_pages = int(self._cfg.get("timing", {}).get("max_search_pages", 1))
        next_sel = sel.get("next_page_btn") or ""
        # ElementHandle은 SPA 재렌더 시 detached 되므로 Locator로 매번 재조회한다.
        for page_num in range(1, max(1, max_pages) + 1):
            try:
                self._page.wait_for_selector(sel["result_rows"], timeout=8000)
            except Exception:
                break
            if page_num == 1:
                self._set_page_size_40()
            rows = self._page.locator(sel["result_rows"])
            for i in range(rows.count()):
                row = rows.nth(i)
                try:
                    rb = _digits(row.locator(sel["result_biz_no"]).inner_text(timeout=1500))
                    rn = _norm_name(row.locator(sel["result_company_name"]).inner_text(timeout=1500))
                except Exception:
                    continue
                if (target_biz and rb == target_biz) or (not target_biz and rn == target_name):
                    btn = row.locator(sel["result_detail_btn"]).first
                    pages_before = len(self._context.pages)
                    btn.click(timeout=5000)
                    self._adopt_new_page(pages_before)
                    # Vue 핸들러가 늦게 붙어 클릭이 씹히는 경우 → 1회 재클릭
                    if not self._detail_ready(dsel, timeout=6000):
                        try:
                            btn.click(timeout=3000)
                            self._adopt_new_page(pages_before)
                        except Exception:
                            pass
                    self._wait_detail_loaded(dsel)
                    return
            if not next_sel or page_num >= max_pages:
                break
            nxt = self._page.locator(next_sel)
            if nxt.count() == 0:
                break
            self._delay()
            try:
                nxt.first.click(timeout=3000)
            except Exception:
                break
        raise CollectorError("상세 진입 대상 행을 찾지 못함")

    def _adopt_new_page(self, pages_before: int) -> None:
        """클릭이 새 탭을 열었으면 그 탭으로 작업 대상을 전환."""
        try:
            self._page.wait_for_timeout(1200)   # 새 탭이 열릴 시간
            pages = self._context.pages
            if len(pages) > pages_before:
                self._page = pages[-1]
                self._page.set_default_timeout(
                    int(self._cfg["timing"].get("page_timeout_sec", 30)) * 1000)
        except Exception:
            pass

    def _detail_ready(self, dsel: dict, timeout: int = 6000) -> bool:
        try:
            self._page.wait_for_selector(dsel["ready_marker"], timeout=timeout,
                                         state="visible")
            return True
        except Exception:
            return False

    def _wait_detail_loaded(self, dsel: dict) -> None:
        if not self._detail_ready(dsel, timeout=12000):
            if self.is_login_page():
                raise LoginRequired()
            shot = self._debug_shot("detail_fail")
            raise CollectorError(
                f"상세 로딩 실패 (url={self._page.url}"
                + (f", 스크린샷={shot}" if shot else "") + ")")
        if self.is_login_page():
            raise LoginRequired()

    def _debug_shot(self, tag: str) -> str | None:
        """실패 순간 화면을 저장해 원인 파악에 사용."""
        try:
            import os
            os.makedirs("debug", exist_ok=True)
            path = f"debug/{tag}_{now_seoul().strftime('%H%M%S')}.png"
            self._page.screenshot(path=path)
            return path
        except Exception:
            return None

    def _click_tab(self, name: str) -> bool:
        """상세 화면 상단 탭('기업 정보'/'주요 재무 정보') 클릭. 없으면 False."""
        try:
            tab = self._page.locator(".v-tab", has_text=name).first
            if tab.count() == 0:
                return False
            tab.click(timeout=3000)
            self._page.wait_for_timeout(700)
            return True
        except Exception:
            return False

    def _lazy_scroll(self) -> None:
        """v-lazy(스크롤 시 렌더) 영역 렌더 유도."""
        try:
            for frac in (0.35, 0.7, 1.0):
                self._page.evaluate(
                    f"window.scrollTo(0, document.body.scrollHeight * {frac})")
                self._page.wait_for_timeout(400)
        except Exception:
            pass

    def _wait_fin_values(self, dsel: dict) -> None:
        """재무 KPI 값(API)이 화면에 찰 때까지 대기. 미제공 기업이면 그냥 통과."""
        try:
            fin_val = f"{dsel['finance_card']} {dsel['finance_value']}"
            self._page.wait_for_selector(fin_val, timeout=6000, state="visible")
            self._page.wait_for_timeout(500)
        except Exception:
            pass

    def _parse_detail(self, candidate: dict) -> dict:
        dsel = self._cfg["selectors"]["detail"]
        args = {
            "card": dsel["finance_card"],
            "label": dsel["finance_label"],
            "value": dsel["finance_value"],
            "symbol": dsel["finance_symbol"],
            "addr": dsel["address_value"],
            "settle": dsel.get("settlement_text", "결산 일자"),
        }
        # 1) '기업 정보' 탭에서 기본정보 파싱 (탭이 없으면 현재 화면 그대로)
        self._click_tab("기업 정보")
        self._lazy_scroll()
        data = self._page.evaluate(_DETAIL_JS, args)

        # 2) '주요 재무 정보' 탭이 있으면 클릭해 재무 파싱 후 병합.
        #    (탭 구조가 아니면 1)에서 이미 스크롤로 재무까지 읽힘)
        need_fin = not (data.get("fin") or data.get("table"))
        if self._click_tab("주요 재무 정보"):
            self._lazy_scroll()
            self._wait_fin_values(dsel)
            d2 = self._page.evaluate(_DETAIL_JS, args)
            data = _merge_detail(data, d2)
            # ⚠️ 재무 탭은 SPA 컨텍스트를 바꿔 이후 검색이 전부 0건이 되므로 반드시 복귀
            self._click_tab("기업 정보")
        elif need_fin:
            self._lazy_scroll()
            self._wait_fin_values(dsel)
            d2 = self._page.evaluate(_DETAIL_JS, args)
            data = _merge_detail(data, d2)
        basic = data.get("basic", {})
        fin = data.get("fin", {})
        table = data.get("table", {}) or {}
        tunit = (data.get("tunit") or "천원").strip()

        def fin_amt(key):
            # 1순위: KPI 카드(값+단위) / 2순위: 재무제표 표(단위: 천원 등)
            e = fin.get(key)
            v = amount_to_millions(e["v"], e["u"]) if e else None
            if v is None:
                tv = table.get(key)
                if tv not in (None, "", "-"):
                    v = amount_to_millions(tv, tunit)
            return v

        out = {
            "회사명": candidate.get("회사명"),
            "사업자번호": candidate.get("사업자번호"),
            "대표자": _lookup(basic, dsel["label_ceo"]),
            "주소": data.get("address") or candidate.get("주소"),
            "업종": _lookup(basic, dsel["label_industry"]),
            "설립일": _lookup(basic, dsel["label_founded"]),
            "대표번호": (data.get("tel") or "").strip() or None,
            "종업원수": _leading_int(_lookup(basic, dsel["label_employees"])),
            "휴폐업정보": _lookup(basic, "휴폐업정보"),
            "매출액": fin_amt("매출액"),
            "영업이익": fin_amt("영업이익"),
            "당기순이익": fin_amt("당기순이익"),
            "신용등급": None,   # '개요'에는 없음(신용/등급 탭). 미제공으로 기록됨.
            "결산일자": _first_date(data.get("settlement")) or _first_date(data.get("tdate")),
        }
        return out

    def is_login_page(self) -> bool:
        marker = self._cfg["selectors"]["login_page_markers"]
        url_hint = marker.get("url_contains")
        dom_hint = marker.get("dom_marker")
        if url_hint and url_hint in self._page.url:
            return True
        if dom_hint and self._page.query_selector(dom_hint):
            return True
        return False

    def _delay(self) -> None:
        t = self._cfg["timing"]
        time.sleep(random.uniform(
            float(t.get("request_delay_min_sec", 1.0)),
            float(t.get("request_delay_max_sec", 3.0)),
        ))


def _text(el, css: str) -> str | None:
    node = el.query_selector(css)
    return (node.inner_text().strip() if node else None) or None


def _digits(s) -> str:
    return re.sub(r"\D", "", s or "")


# 검색어에서 법인격 표기만 제거(핵심 상호로 검색해 적중률↑). 공백은 유지.
# 패턴은 matcher.CORP_FORM_RE 와 공유 (상법 5종 + 민법·특별법인 전부).
def _search_term(name) -> str:
    s = CORP_FORM_RE.sub(" ", name or "")
    return re.sub(r"\s+", " ", s).strip()


# 이름 비교용 정규화: matcher와 동일 규칙 사용 (법인격 표기 + 기호 제거)
_SYMBOL_STRIP = re.compile(r"[\s()\[\]·.,\-_/]")


def _norm_name(s) -> str:
    return _SYMBOL_STRIP.sub("", CORP_FORM_RE.sub("", s or "")).lower()


def _lookup(basic: dict, label: str) -> str | None:
    """기본정보 라벨→값 dict에서 라벨(부분일치 허용)로 값을 찾는다."""
    if not label:
        return None
    v = basic.get(label)
    if v is None:
        v = next((val for k, val in basic.items() if label in k), None)
    if v is None:
        return None
    v = v.strip()
    return v or None


_DATE_IN = re.compile(r"\d{4}[.\-/]\d{1,2}[.\-/]\d{1,2}")


def _first_date(text) -> str | None:
    if not text:
        return None
    m = _DATE_IN.search(str(text))
    return m.group(0) if m else None


def _merge_detail(a: dict, b: dict) -> dict:
    """두 번의 상세 파싱 결과 병합. dict류는 합치고 단일 값은 먼저 있는 것 우선."""
    out = dict(a)
    for k in ("basic", "fin", "table"):
        merged = dict(a.get(k) or {})
        for kk, vv in (b.get(k) or {}).items():
            if kk not in merged or merged[kk] in ("", "-", None):
                merged[kk] = vv
        out[k] = merged
    for k in ("address", "settlement", "tel", "tunit", "tdate"):
        out[k] = a.get(k) or b.get(k)
    return out


def _leading_int(text) -> int | None:
    """'5명 (2007.12.31 기준)' → 5. 날짜 등 뒤 숫자가 붙는 오염 방지."""
    if text in (None, ""):
        return None
    m = re.match(r"\s*([\d,]+)", str(text))
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


# 상세(개요) 페이지 파싱 JS.
#  - 기본정보: th(scope=row) 라벨 → 다음 td 값
#  - 재무: KPI 카드 라벨 → 값(단위 심볼 분리)
#  - 주소/결산일자
_DETAIL_JS = r"""
(s) => {
  const basic = {};
  document.querySelectorAll('th[scope="row"]').forEach(th => {
    const td = th.nextElementSibling;
    if (td && td.tagName === 'TD') basic[th.innerText.trim()] = td.innerText.trim();
  });
  const fin = {};
  document.querySelectorAll(s.card).forEach(card => {
    const labelEl = card.querySelector(s.label);
    const valEl = card.querySelector(s.value);
    if (!labelEl || !valEl) return;
    const symEl = valEl.querySelector(s.symbol);
    const unit = symEl ? symEl.innerText.trim() : "";
    let v = valEl.innerText.trim();
    if (unit) v = v.replace(unit, "").trim();
    fin[labelEl.innerText.trim()] = { v: v, u: unit };
  });
  const addrEl = document.querySelector(s.addr);
  const address = addrEl ? addrEl.innerText.trim() : "";
  let settlement = "";
  for (const el of document.querySelectorAll('span, div')) {
    const t = (el.innerText || "").trim();
    if (t.indexOf(s.settle) === 0) { settlement = t; break; }
  }
  // 전화번호: 라벨 없이 'Tel: 02-123-4567 / Fax: ...' 형태의 td 에 있음
  let tel = "";
  for (const td of document.querySelectorAll('td')) {
    const t = (td.innerText || "");
    const m = t.match(/Tel\s*[:.]?\s*([0-9][0-9\-.() ]{6,})/i);
    if (m) { tel = m[1].trim(); break; }
  }
  // 재무상태표/(포괄)손익계산서 표: 라벨(th) → 마지막 연도 열 값. 단위/결산일자 포함.
  let tunit = "";
  for (const el of document.querySelectorAll('.nbl--info__data, .section__header__desc')) {
    const t = (el.innerText || "").trim();
    const m = t.match(/단위\s*[::]\s*([^\s]+)/);
    if (m) { tunit = m[1]; break; }
  }
  const table = {};
  let tdate = "";
  for (const cap of document.querySelectorAll('table > caption')) {
    const nm = (cap.innerText || "").trim();
    if (nm.indexOf('손익계산서') === -1 && nm.indexOf('재무상태표') === -1) continue;
    const tbl = cap.parentElement;
    const ths = tbl.querySelectorAll('thead tr:last-child th');
    if (ths.length) {
      const t = (ths[ths.length - 1].innerText || "").trim();
      if (/\d{4}/.test(t)) tdate = t;
    }
    for (const tr of tbl.querySelectorAll('tbody tr')) {
      const th = tr.querySelector('th');
      const tds = tr.querySelectorAll('td');
      if (!th || !tds.length) continue;
      table[(th.innerText || "").trim()] = (tds[tds.length - 1].innerText || "").trim();
    }
  }
  return { basic: basic, fin: fin, address: address, settlement: settlement,
           tel: tel, table: table, tunit: tunit, tdate: tdate };
}
"""


# ── 모의 수집기 (UI/파이프라인 검증용) ────────────────────────────────────────

_MOCK_DB = {
    "삼성전자": [{
        "회사명": "삼성전자(주)", "사업자번호": "1248100998", "대표자명": "한종희",
        "주소": "경기 수원시 영통구",
        "_detail": {
            "업종": "반도체 제조업", "설립일": "1969-01-13", "대표번호": "031-200-1114",
            "종업원수": "125819", "매출액": "258900000", "영업이익": "6566900",
            "당기순이익": "15486800", "신용등급": "AAA",
        },
        "_finance_years": [
            {"연도": "2025", "매출액": "258900000", "영업이익": "6566900", "당기순이익": "15486800"},
            {"연도": "2024", "매출액": "302231000", "영업이익": "32827300", "당기순이익": "26970300"},
            {"연도": "2023", "매출액": "279604800", "영업이익": "43376800", "당기순이익": "55654100"},
        ],
    }],
    "현대자동차": [{
        "회사명": "현대자동차(주)", "사업자번호": "1018114965", "대표자명": "장재훈",
        "주소": "서울 서초구",
        "_detail": {
            "업종": "자동차 제조업", "설립일": "1967-12-29", "대표번호": "02-3464-1114",
            "종업원수": "75091", "매출액": "162663700", "영업이익": "15126700",
            "당기순이익": "12270200", "신용등급": "AA+",
        },
        "_finance_years": [
            {"연도": "2025", "매출액": "162663700", "영업이익": "15126700", "당기순이익": "12270200"},
            {"연도": "2024", "매출액": "142523000", "영업이익": "9824400", "당기순이익": "7991800"},
            {"연도": "2023", "매출액": "117610000", "영업이익": "6679000", "당기순이익": "5693400"},
        ],
    }],
}


class MockCollector:
    """사이트 접근 없이 GUI/파이프라인을 검증할 수 있는 가짜 수집기."""

    def __init__(self, config: dict | None = None):
        self._logged_in = False

    def login(self, user_id: str, password: str) -> None:
        if not user_id or not password:
            raise CollectorError("ID/PW 누락")
        time.sleep(0.2)
        self._logged_in = True

    def search(self, company_name: str) -> list[dict]:
        time.sleep(0.1)
        for key, rows in _MOCK_DB.items():
            if key in company_name or company_name in key:
                return [{k: v for k, v in r.items() if not k.startswith("_")} | {"_detail_link": key}
                        for r in rows]
        return []

    def fetch_detail(self, candidate: dict, finance_years: int = 1) -> dict:
        time.sleep(0.1)
        key = candidate.get("_detail_link")
        for k, rows in _MOCK_DB.items():
            if k == key:
                base = {kk: vv for kk, vv in rows[0].items() if not kk.startswith("_")}
                base["대표자"] = base.pop("대표자명", None)
                base.update(rows[0]["_detail"])
                if finance_years > 1:
                    base["재무_연도별"] = rows[0].get("_finance_years", [])[:finance_years]
                return base
        raise CollectorError("상세 정보 없음")

    def extend_session(self) -> bool:
        return self._logged_in

    def is_login_page(self) -> bool:
        return not self._logged_in

    def close(self) -> None:
        self._logged_in = False
