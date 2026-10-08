"""검색 결과 후보에서 1건 채택하기 위한 점수 매칭."""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class MatchResult:
    candidate: dict | None
    score: int
    status: str          # 'auto' | 'unique' | 'ambiguous' | 'none'
    others: list[dict]   # ambiguous일 때 다른 후보들 (검수용)


@dataclass
class SelectResult:
    """검색 결과에서 실제로 수집할 대상을 고른 결과.

    status:
      'none'      - 실제 기업 후보 없음 (검색 0건 또는 전부 펀드/ETF)
      'biz'       - 입력 사업자번호와 일치하는 1건 확정
      'single'    - 동명 회사 1건
      'multiple'  - 동명 회사 여러 건 → 전부 수집
      'ambiguous' - 이름이 정확히 일치하는 후보가 없어 판단 보류 (검수 필요)
    picks:   상세를 수집할 대상 리스트 (multiple이면 2건 이상)
    others:  검수용으로 남기는 나머지 후보
    dropped: 사업자번호가 없어 제외된 비기업(펀드/ETF 등) 건수
    """
    status: str
    picks: list[dict]
    others: list[dict]
    dropped: int = 0


def _digits(s: str | None) -> str:
    return re.sub(r"\D", "", s or "")


# 상호에서 법인격 표기를 제거해 이름 비교/검색을 정확히 한다.
# 상법상 회사 5종 + 민법·특별법상 법인 (정식 명칭·괄호 약자·원문자).
# 주의: 괄호형과 명시적 단어형만 제거. 단독 음절('주' 등)은 건드리지 않음
# (예: '주성엔지니어링'의 '주'는 유지).
CORP_FORM_RE = re.compile(
    r"㈜|㈓"
    r"|\(\s*(?:유한책임|사복|유한|합자|합명|주|유|재|사|학|의|복|협|종|특)\s*\)"
    r"|주식회사|유한책임회사|유한회사|합자회사|합명회사"
    r"|재단법인|사단법인|학교법인|의료법인|사회복지법인|영농조합법인|협동조합|종교법인|특수법인"
)
_CORP_FORM = CORP_FORM_RE
_STRIP_NON_NAME = re.compile(r"[\s()\[\]·.,\-_/]")


def _norm_company(name: str | None) -> str:
    s = _CORP_FORM.sub("", name or "")
    s = _STRIP_NON_NAME.sub("", s)
    return s.lower()


def _has_biz(candidate: dict) -> bool:
    """실제 기업이면 사업자번호가 있다. 펀드/ETF 등은 '-'/공란."""
    return bool(_digits(candidate.get("사업자번호")))


def _excluded_type(candidate: dict) -> bool:
    """기업유형 배지가 '개인' 또는 '폐업'이면 수집 제외."""
    t = (candidate.get("기업유형") or "").strip()
    return ("개인" in t) or ("폐업" in t)


def _dedup_by_biz(cands: list[dict]) -> list[dict]:
    """사업자번호 기준 중복 제거(첫 등장 유지)."""
    seen: set[str] = set()
    out: list[dict] = []
    for c in cands:
        b = _digits(c.get("사업자번호"))
        if b in seen:
            continue
        seen.add(b)
        out.append(c)
    return out


def select_matches(query: dict, candidates: list[dict], weights: dict,
                   narrow_fields: list | set | None = None) -> SelectResult:
    """수집 대상 후보를 선별한다.

    규칙:
      1. 사업자번호 없는 후보(펀드/ETF 등)는 제외한다.
      2. 입력에 사업자번호가 있으면 그 번호와 일치하는 1건만 확정한다.
      3. 사업자번호를 모르면, 상호가 정확히 일치하는 실제 기업을 채택하되,
         narrow_fields(대표자명/주소)로 동명 후보를 좁힌다.
         - 1건이면 'single', 여러 건(동명이인)이면 'multiple' → 전부 수집.
         - 정확 일치가 없으면 'ambiguous'로 두어 검수하도록 한다.

    narrow_fields: 중복 필터에 사용할 컬럼 집합. None이면 존재하는 값 모두 사용.
    """
    real = [c for c in candidates if _has_biz(c) and not _excluded_type(c)]
    dropped = len(candidates) - len(real)

    if not real:
        return SelectResult("none", [], [], dropped)

    # 2) 입력 사업자번호로 확정
    q_biz = _digits(query.get("사업자번호"))
    if q_biz:
        exact = [c for c in real if _digits(c.get("사업자번호")) == q_biz]
        if exact:
            others = [c for c in real if c not in exact]
            return SelectResult("biz", exact[:1], others, dropped)
        # 사업자번호를 줬는데 결과에 없음 → 이름으로 재판단 (아래로 진행)

    # 3) 상호 정확 일치로 동명 회사 채택
    qn = _norm_company(query.get("회사명"))
    name_matches = [c for c in real if _norm_company(c.get("회사명")) == qn] if qn else []
    # 같은 회사가 상호 표기만 달리해 여러 번 뜨는 경우 사업자번호로 중복 제거
    name_matches = _dedup_by_biz(name_matches)
    # 선택된 컬럼(대표자명/주소)으로 동명 후보를 좁혀 중복을 줄인다
    name_matches = _narrow_by_query(query, name_matches, narrow_fields)
    others = [c for c in real if c not in name_matches]

    if not name_matches:
        return SelectResult("ambiguous", [], real, dropped)
    if len(name_matches) == 1:
        return SelectResult("single", name_matches, others, dropped)
    return SelectResult("multiple", name_matches, others, dropped)


def _addr_tokens(addr: str | None) -> set[str]:
    """주소에서 시/구 단위 키워드 추출 (간단 토큰화)."""
    if not addr:
        return set()
    tokens = re.findall(r"[가-힣A-Za-z]+(?:특별시|광역시|특별자치시|특별자치도|도|시|군|구)", addr)
    return set(tokens)


# 시/도 정식 명칭 → 축약 (검색 결과는 '경기 안양시'처럼 축약 표기)
_PROV_MAP = {
    "서울특별시": "서울", "서울시": "서울", "부산광역시": "부산", "대구광역시": "대구",
    "인천광역시": "인천", "광주광역시": "광주", "대전광역시": "대전", "울산광역시": "울산",
    "세종특별자치시": "세종", "경기도": "경기", "강원도": "강원", "강원특별자치도": "강원",
    "충청북도": "충북", "충청남도": "충남", "전라북도": "전북", "전북특별자치도": "전북",
    "전라남도": "전남", "경상북도": "경북", "경상남도": "경남",
    "제주특별자치도": "제주", "제주도": "제주",
}
_PROV_SHORT = set(_PROV_MAP.values())


def _region_parts(addr: str | None) -> tuple[str | None, str | None]:
    """주소 앞부분에서 (시/도, 시/군)을 추출. 예: '경기도 안양시 동안구 …' → ('경기','안양시')."""
    if not addr:
        return (None, None)
    s = re.sub(r"\(\d{3,6}\)", " ", str(addr))   # 우편번호 제거
    s = re.sub(r"[,()]", " ", s)
    prov = city = None
    for t in s.split()[:3]:
        n = _PROV_MAP.get(t, t)
        if n in _PROV_SHORT and prov is None:
            prov = n
            continue
        if city is None and t.endswith(("시", "군")):
            city = t
    return (prov, city)


def region_match(query_addr: str | None, cand_addr: str | None) -> bool:
    """주소 앞부분(시/군까지)만 비교. 전체 일치 불요.

    시/군이 양쪽에 있으면 시/군 일치로 판단, 없으면(서울 등 광역) 시/도 일치로 판단.
    """
    qp, qc = _region_parts(query_addr)
    cp, cc = _region_parts(cand_addr)
    if qc and cc:
        return qc == cc
    if qp and cp:
        return qp == cp
    return False


def _narrow_by_query(query: dict, cands: list[dict],
                     fields: list | set | None = None) -> list[dict]:
    """동명 후보를 입력의 대표자명/주소로 좁힌다 (엄격 모드).

    입력에 해당 값이 있으면 '일치하는 회사만' 남긴다. 아무도 일치하지 않으면
    빈 목록을 반환해 확인필요로 분류되게 한다(잘못된 회사 수집 방지).
    fields: 사용할 컬럼 집합. None이면 값이 있는 컬럼 모두 사용.
    """
    if len(cands) <= 1:
        return cands
    use = None if fields is None else set(fields)
    out = cands

    if use is None or "대표자명" in use:
        q_ceo = (query.get("대표자명") or "").strip()
        if q_ceo:
            def _ceo_ok(c):
                cc = (c.get("대표자명") or "").strip()
                # 공동대표('유홍철/지명하') 표기 대비 상호 포함 허용
                return bool(cc) and (q_ceo in cc or cc in q_ceo)
            out = [c for c in out if _ceo_ok(c)]

    if (use is None or "주소" in use) and out:
        q_addr = (query.get("주소") or "").strip()
        if q_addr:
            out = [c for c in out if region_match(q_addr, c.get("주소"))]
    return out


def score_candidate(query: dict, candidate: dict, weights: dict) -> int:
    """입력값(query)과 후보(candidate)의 일치 점수."""
    score = 0
    q_biz = _digits(query.get("사업자번호"))
    c_biz = _digits(candidate.get("사업자번호"))
    if q_biz and c_biz and q_biz == c_biz:
        score += int(weights.get("weight_biz_no", 100))

    q_ceo = (query.get("대표자명") or "").strip()
    c_ceo = (candidate.get("대표자명") or "").strip()
    if q_ceo and c_ceo and q_ceo == c_ceo:
        score += int(weights.get("weight_ceo", 30))

    q_tokens = _addr_tokens(query.get("주소"))
    c_tokens = _addr_tokens(candidate.get("주소"))
    if q_tokens & c_tokens:
        score += int(weights.get("weight_address", 20))

    return score


def pick(query: dict, candidates: list[dict], weights: dict) -> MatchResult:
    if not candidates:
        return MatchResult(None, 0, "none", [])
    if len(candidates) == 1:
        return MatchResult(candidates[0], 0, "unique", [])

    # 사업자번호 일치는 즉시 확정
    q_biz = _digits(query.get("사업자번호"))
    if q_biz:
        for c in candidates:
            if _digits(c.get("사업자번호")) == q_biz:
                return MatchResult(c, int(weights.get("weight_biz_no", 100)), "auto", [])

    scored = sorted(
        ((score_candidate(query, c, weights), c) for c in candidates),
        key=lambda x: x[0],
        reverse=True,
    )
    top_score, top_cand = scored[0]
    second_score = scored[1][0] if len(scored) > 1 else 0
    threshold = int(weights.get("accept_threshold", 25))

    # 최상위가 임계치 이상이고 2위와 차이가 있으면 채택
    if top_score >= threshold and top_score > second_score:
        return MatchResult(top_cand, top_score, "auto", [c for _, c in scored[1:]])

    return MatchResult(top_cand, top_score, "ambiguous", [c for _, c in scored[1:]])
