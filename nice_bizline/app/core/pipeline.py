"""수집 파이프라인 코어 - generator로 진행 이벤트 스트리밍.

Worker 스레드와 Streamlit 웹 UI가 이 함수를 공유합니다.
Worker: 스레드에서 이 generator를 소비하며 콜백으로 UI에 전달.
Streamlit: 메인 스레드에서 generator를 for-loop으로 소비하며 위젯 갱신.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterator

from . import checkpoint
from .collector import CollectorError, LoginRequired
from .matcher import select_matches
from .normalizer import normalize_amount, normalize_record
from .session import SessionManager
from .timeutil import now_seoul


@dataclass
class PipelineState:
    records: list[dict] = field(default_factory=list)
    unfound: list[dict] = field(default_factory=list)
    ambiguous: list[dict] = field(default_factory=list)
    processed_keys: set = field(default_factory=set)
    summary: dict = field(default_factory=dict)


@dataclass
class PipelineOptions:
    user_id: str
    password: str
    companies: list[dict]
    finance_years: int = 1
    input_path: str = ""
    resume: bool = False
    checkpoint_every: int = 10
    # 중복(동명) 필터에 사용할 컬럼. None이면 존재하는 값 모두 사용.
    narrow_fields: list | None = None
    # 결과 필터: 수집된 값이 조건을 충족하는 회사만 기록.
    #   {"min_employees": 20, "min_sales": 1000(백만원), "mode": "AND"|"OR"}
    result_filter: dict | None = None


def run_pipeline(collector, cfg: dict, opts: PipelineOptions,
                 state: PipelineState | None = None,
                 stop_check=lambda: False) -> Iterator[dict]:
    """수집 파이프라인을 실행하며 이벤트를 yield.

    이벤트 형식:
      {"type": "log", "level": "info|warn|error", "message": "..."}
      {"type": "progress", "current": N, "total": M, "name": "..."}
      {"type": "done", "state": PipelineState}

    stop_check: 매 회사 처리 전 호출. True면 안전 정지.
    """
    if state is None:
        state = PipelineState()

    started = now_seoul()
    state.summary["started_at"] = started.strftime("%Y-%m-%d %H:%M:%S")

    session = SessionManager(cfg["timing"].get("relogin_threshold_minutes", 9))
    weights = cfg.get("matching", {})

    # 재개
    if opts.resume and opts.input_path:
        loaded = checkpoint.load(opts.input_path)
        if loaded:
            state.records = list(loaded.get("records", []))
            state.unfound = list(loaded.get("unfound", []))
            state.ambiguous = list(loaded.get("ambiguous", []))
            state.processed_keys = set(loaded.get("processed_keys", []))
            yield _log("info", f"체크포인트 로드 - 이미 처리된 {len(state.processed_keys)}건 스킵")

    # 로그인
    try:
        collector.login(opts.user_id, opts.password)
        session.mark_login()
        yield _log("info", "로그인 성공")
    except CollectorError as e:
        yield _log("error", f"로그인 실패: {e}")
        _finalize(state, opts, session, stopped=False, collector=collector)
        yield {"type": "done", "state": state}
        return

    total = len(opts.companies)
    stopped = False
    seen_this_run: set = set()
    consecutive_errors = 0        # 연속 회사 처리 오류 (인터넷 장기 단절 등)
    _MAX_CONSECUTIVE_ERRORS = 5   # 이만큼 연속 오류면 안전 정지(이어서 재개 가능)
    recent_error_items: list[tuple[str, str]] = []   # 연속 오류의 (key, 회사명)
    for i, query in enumerate(opts.companies, 1):
        if stop_check():
            yield _log("warn", "사용자 중단 - 처리분까지 저장합니다.")
            stopped = True
            break

        name = query.get("회사명", "")
        key = _key_for(query)

        # 이미 처리된 것 스킵 - 원인에 따라 안내
        if key in state.processed_keys:
            if key in seen_this_run:
                # 같은 실행 안에서 동일 입력이 다시 등장 → 입력 중복
                yield _log("warn", f"[{name}] 중복 입력 - 스킵 (같은 회사명|사업자번호가 이미 처리됨)")
                yield {"type": "progress", "current": i, "total": total,
                       "name": f"{name} (중복 입력 - 스킵)"}
            else:
                # 체크포인트에서 로드된 기처리분 → 재개 스킵
                yield {"type": "progress", "current": i, "total": total,
                       "name": f"{name} (이미 처리 - 스킵)"}
            continue

        seen_this_run.add(key)
        yield {"type": "progress", "current": i, "total": total, "name": name}

        # 세션 유지: 우선 '로그인 연장' 버튼 클릭, 안 되면 재로그인으로 폴백
        if session.needs_relogin():
            extended = False
            try:
                extended = collector.extend_session()
            except Exception:
                extended = False
            if extended:
                session.mark_login()
                yield _log("info", "세션 연장 (+10분)")
            else:
                yield _log("info", "세션 연장 버튼 없음 → 자동 재로그인")
                ok = yield from _relogin_with_backoff(collector, opts, session)
                if not ok:
                    # 이 회사는 처리 안 된 상태로 정지 → 재개 시 여기부터 다시
                    state.processed_keys.discard(key)
                    yield _log("error",
                               "재로그인이 계속 실패해 안전 정지합니다. 처리분은 저장되며, "
                               "다음 실행 시 자동으로 이어서 진행됩니다.")
                    stopped = True
                    break

        errors_before = sum(1 for u in state.unfound if u.get("조회상태") == "오류")
        yield from _process_one(collector, opts, state, session, weights, query, name)
        state.processed_keys.add(key)

        # 연속 오류 감지: 인터넷 장기 단절 등이면 안전 정지 후 나중에 이어서
        errors_after = sum(1 for u in state.unfound if u.get("조회상태") == "오류")
        if errors_after > errors_before:
            consecutive_errors += 1
            recent_error_items.append((key, name))
        else:
            consecutive_errors = 0
            recent_error_items.clear()
        if consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
            # 이 오류들은 연결 문제일 가능성이 높으므로, 재개 시 다시 시도되도록
            # 처리 목록과 오류 기록에서 제거한다.
            err_names = {n for _, n in recent_error_items}
            for k, _ in recent_error_items:
                state.processed_keys.discard(k)
            state.unfound = [u for u in state.unfound
                             if not (u.get("조회상태") == "오류" and u.get("회사명") in err_names)]
            state.records = [r for r in state.records
                             if not (r.get("조회상태") == "오류" and r.get("회사명") in err_names)]
            yield _log("error",
                       f"연속 {consecutive_errors}건 오류 - 연결 문제로 보입니다. "
                       "안전 정지합니다. 다음 실행에서 '이어서 진행'을 선택하면 "
                       "이 회사들부터 다시 수집합니다.")
            stopped = True
            break

        # 주기적 체크포인트
        if opts.input_path and i % max(1, opts.checkpoint_every) == 0:
            if checkpoint.save(opts.input_path, checkpoint.build_state(
                processed_keys=sorted(state.processed_keys),
                records=state.records,
                unfound=state.unfound,
                ambiguous=state.ambiguous,
                total=total,
                finance_years=opts.finance_years,
            )):
                yield _log("info", f"체크포인트 저장 ({len(state.processed_keys)}건)")

    _finalize(state, opts, session, stopped=stopped, collector=collector)
    yield {"type": "done", "state": state}


def _relogin_with_backoff(collector, opts, session):
    """접속이 끊겼을 때 로그인 페이지부터 자동 재로그인 (사람 개입 불필요).

    실패하면 15초→30초→1분→2분→4분→5분×3 간격으로 계속 재시도 (총 약 22분).
    중복 로그인으로 튕긴 경우 동시접속 팝업 처리(접속 종료)가 login() 안에 포함됨.
    """
    import time as _time
    delays = (15, 30, 60, 120, 240, 300, 300, 300)
    for i, delay in enumerate(delays, 1):
        try:
            collector.login(opts.user_id, opts.password)
            session.mark_login()
            if i > 1:
                yield _log("info", f"자동 재로그인 성공 ({i}번째 시도)")
            return True
        except Exception as e:
            yield _log("warn",
                       f"재로그인 실패({i}/{len(delays)}): {e} → {delay}초 후 자동 재시도")
            _time.sleep(delay)
    return False


def _process_one(collector, opts, state, session, weights, query, name):
    """한 회사 수집. 실패 시(세션 만료·강제 로그아웃·인터넷 단절 등)
    로그인 페이지부터 다시 로그인하고 같은 회사를 1회 재시도한다."""
    for attempt in (1, 2):
        try:
            yield from _collect(collector, opts, state, weights, query, name)
            return
        except LoginRequired:
            if attempt == 2:
                _record_error(state, name, "세션 만료 재시도 실패", query)
                return
            yield _log("warn", f"[{name}] 세션 만료/강제 로그아웃 감지 → 처음부터 재로그인 후 재시도")
        except CollectorError as e:
            if attempt == 2:
                yield _log("error", f"[{name}] 수집 오류: {e}")
                _record_error(state, name, str(e), query)
                return
            yield _log("warn", f"[{name}] 수집 오류({e}) → 재로그인 후 재시도")
        except Exception as e:
            if attempt == 2:
                yield _log("error", f"[{name}] 예외: {e}")
                _record_error(state, name, f"예외: {e}", query)
                return
            yield _log("warn", f"[{name}] 연결 오류 감지({type(e).__name__}) → 처음부터 재로그인 후 재시도")
        # 복구: 로그인 페이지로 돌아가 자동 재로그인 (동시접속 팝업 처리 + 백오프)
        ok = yield from _relogin_with_backoff(collector, opts, session)
        if not ok:
            _record_error(state, name, "재로그인 반복 실패", query)
            return


def _collect(collector, opts, state, weights, query, name):
    candidates = collector.search(name)
    res = select_matches(query, candidates, weights, opts.narrow_fields)

    if res.dropped:
        yield _log("info", f"[{name}] 제외 {res.dropped}건 (펀드/ETF·개인·폐업)")

    if res.status == "none":
        yield _log("warn", f"[{name}] 미발견")
        reason = "검색 결과 0건" if not candidates else "실제 기업 후보 없음(전부 펀드/ETF 등)"
        state.unfound.append({"회사명": name, "조회상태": "미발견", "사유": reason})
        state.records.append(_with_input(_blank_row(name, "미발견", reason), query, True))
        return

    if res.status == "ambiguous":
        others_desc = "; ".join(
            f"{c.get('회사명','')}/{c.get('사업자번호','')}" for c in res.others[:5]
        )
        yield _log("warn", f"[{name}] 상호 정확 일치 없음 - 확인필요 ({len(res.others)}건 후보)")
        state.ambiguous.append({
            "회사명": name,
            "채택후보": "",
            "다른후보들": others_desc,
            "사유": "상호가 정확히 일치하는 후보 없음",
        })
        state.records.append(_with_input(
            _blank_row(name, "확인필요", "상호 정확 일치 후보 없음"), query, True))
        return

    # biz / single / multiple → 채택된 후보를 전부 상세 수집
    picks = res.picks
    total_picks = len(picks)
    if total_picks > 1:
        yield _log("info", f"[{name}] 동명 회사 {total_picks}건 - 전부 수집")

    # LoginRequired 재시도 시 부분 중복을 막기 위해 로컬에 모은 뒤 일괄 반영
    new_records: list[dict] = []
    for idx, cand in enumerate(picks, 1):
        try:
            detail = collector.fetch_detail(cand, opts.finance_years)
        except LoginRequired:
            raise  # 아직 state에 반영 전 → 재로그인 후 처음부터 안전하게 재시도
        except CollectorError as e:
            new_records.append(_blank_row(
                cand.get("회사명") or name, "성공", f"상세 파싱 실패: {e}"))
            continue
        rec = normalize_record(detail)
        rec.update(_finance_columns(rec, opts.finance_years))
        rec["조회상태"] = "성공"
        rec["조회일시"] = now_seoul().strftime("%Y-%m-%d %H:%M:%S")
        missing = [k for k in ("매출액", "영업이익", "당기순이익", "신용등급")
                   if rec.get(k) in (None, "")]
        notes = []
        if total_picks > 1:
            notes.append(f"동명 {total_picks}건 중 {idx}")
        if rec.pop("_detail_failed", None):
            notes.append("상세 미진입(기본정보만)")
        if missing:
            notes.append(f"권한없음/미제공: {', '.join(missing)}")
        # 결과 필터: 조건 미충족 회사는 기록하지 않음
        passed, why = _passes_result_filter(rec, opts.result_filter)
        if not passed:
            state.summary["필터제외"] = state.summary.get("필터제외", 0) + 1
            yield _log("info", f"[{rec.get('회사명', name)}] 결과 필터 제외 ({why})")
            continue

        rec["비고"] = " / ".join(notes)
        new_records.append(rec)

    # 입력 행 정보 연결: 동명 여러 건이어도 입력 정보는 첫 행에만 표시
    for j, rec in enumerate(new_records):
        _with_input(rec, query, j == 0)
    state.records.extend(new_records)
    for idx, rec in enumerate(new_records, 1):
        suffix = f" ({idx}/{total_picks})" if total_picks > 1 else ""
        yield _log("info", f"[{rec.get('회사명', name)}] 수집 완료{suffix}")


def _finalize(state, opts, session, stopped, collector):
    if opts.input_path:
        checkpoint.save(opts.input_path, checkpoint.build_state(
            processed_keys=sorted(state.processed_keys),
            records=state.records,
            unfound=state.unfound,
            ambiguous=state.ambiguous,
            total=len(opts.companies),
            finance_years=opts.finance_years,
        ))

    s = state.summary
    s["ended_at"] = now_seoul().strftime("%Y-%m-%d %H:%M:%S")
    s["total"] = len(opts.companies)
    s["success"] = sum(1 for r in state.records if r.get("조회상태") == "성공")
    s["not_found"] = sum(1 for r in state.records if r.get("조회상태") == "미발견")
    s["ambiguous"] = sum(1 for r in state.records if r.get("조회상태") == "확인필요")
    s["error"] = sum(1 for r in state.records if r.get("조회상태") == "오류")
    s["stopped"] = stopped
    try:
        collector.close()
    except Exception:
        pass


def _log(level: str, message: str) -> dict:
    return {"type": "log", "level": level, "message": message}


def _key_for(query: dict) -> str:
    return f"{query.get('회사명', '')}|{query.get('사업자번호', '')}"


def _passes_result_filter(rec: dict, f: dict | None) -> tuple[bool, str]:
    """수집된 값이 결과 필터를 충족하는지 검사.

    조건: min_employees(명), min_sales(백만원). 값이 없으면 미충족으로 간주.
    mode: "AND"=모든 조건 충족해야 수집, "OR"=하나만 충족해도 수집.
    반환: (통과 여부, 미충족 사유)
    """
    if not f:
        return True, ""
    checks: list[tuple[bool, str]] = []
    if f.get("min_employees") is not None:
        emp = rec.get("종업원수")
        ok = isinstance(emp, (int, float)) and emp >= f["min_employees"]
        checks.append((ok, f"종업원수 {emp if emp is not None else '없음'}"))
    if f.get("min_sales") is not None:
        sales = rec.get("매출액")
        ok = isinstance(sales, (int, float)) and sales >= f["min_sales"]
        checks.append((ok, f"매출액 {f'{sales}백만원' if sales is not None else '없음'}"))
    if not checks:
        return True, ""
    if (f.get("mode") or "AND").upper() == "OR":
        passed = any(ok for ok, _ in checks)
    else:
        passed = all(ok for ok, _ in checks)
    reason = ", ".join(desc for ok, desc in checks if not ok)
    return passed, reason


def _with_input(rec: dict, query: dict, show: bool) -> dict:
    """결과 행에 원본 입력 행을 연결. show=True인 행에만 입력값을 표시(동명 중복 시 1회)."""
    rec["_input"] = dict(query)
    rec["_input_show"] = bool(show)
    return rec


def _record_error(state, name: str, reason: str, query: dict | None = None) -> None:
    state.unfound.append({"회사명": name, "조회상태": "오류", "사유": reason})
    row = _blank_row(name, "오류", reason)
    if query is not None:
        _with_input(row, query, True)
    state.records.append(row)


def _blank_row(name: str, status: str, reason: str = "") -> dict:
    return {
        "회사명": name,
        "조회상태": status,
        "조회일시": now_seoul().strftime("%Y-%m-%d %H:%M:%S"),
        "비고": reason,
    }


def _finance_columns(rec: dict, finance_years: int) -> dict:
    if finance_years <= 1:
        return {
            "매출액(백만원)": rec.get("매출액"),
            "영업이익(백만원)": rec.get("영업이익"),
            "당기순이익(백만원)": rec.get("당기순이익"),
        }
    out: dict = {}
    for entry in (rec.get("재무_연도별") or [])[:finance_years]:
        year = entry.get("연도", "?")
        out[f"매출액({year})"] = normalize_amount(entry.get("매출액"))
        out[f"영업이익({year})"] = normalize_amount(entry.get("영업이익"))
        out[f"당기순이익({year})"] = normalize_amount(entry.get("당기순이익"))
    return out
