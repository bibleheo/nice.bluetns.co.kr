"""필드 정규화 - 금액, 날짜, 사업자번호 통일."""
from __future__ import annotations

import re
from datetime import datetime


_NUM_RE = re.compile(r"[^\d\-]")
_DATE_PATTERNS = ("%Y-%m-%d", "%Y.%m.%d", "%Y/%m/%d", "%Y%m%d", "%Y년 %m월 %d일")


def normalize_amount(value) -> int | None:
    """금액 문자열에서 숫자만 추출. '1,234백만원' → 1234. 단위는 별도 헤더에 명시."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value)
    # 음수 부호 보존, 그 외 비숫자 제거
    cleaned = _NUM_RE.sub("", s)
    if cleaned in ("", "-"):
        return None
    try:
        return int(cleaned)
    except ValueError:
        return None


# 나이스비즈라인 상세 KPI는 값마다 단위(억원/만원 등)가 달라 → 백만원으로 통일.
_UNIT_TO_MILLION = {
    "조원": 1_000_000, "조": 1_000_000,
    "억원": 100, "억": 100,
    "백만원": 1, "백만": 1,
    "만원": 0.01, "만": 0.01,
    "천원": 0.001, "천": 0.001,
    "원": 0.000001,
}


def amount_to_millions(number, unit) -> int | None:
    """표시값 + 단위를 백만원 정수로 변환.

    예: (20.4, "억원") -> 2040, (1558.3, "만원") -> 16, ("-", "억원") -> None
    단위를 모르면 값 자체를 반올림해 반환(이미 백만원으로 간주).
    """
    if number is None:
        return None
    s = str(number).strip().replace(",", "")
    if s in ("", "-"):
        return None
    try:
        val = float(s)
    except ValueError:
        return None
    factor = _UNIT_TO_MILLION.get((unit or "").strip())
    if factor is None:
        return round(val)
    return round(val * factor)


def normalize_date(value) -> str | None:
    """날짜를 YYYY-MM-DD로 통일."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    s = str(value).strip()
    for fmt in _DATE_PATTERNS:
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return s  # 형식 불명이면 원본 유지


def normalize_biz_number(value) -> str | None:
    """사업자번호 000-00-00000 형식 통일."""
    if value is None or value == "":
        return None
    digits = re.sub(r"\D", "", str(value))
    if len(digits) != 10:
        return str(value).strip()  # 형식 이상이면 원본
    return f"{digits[:3]}-{digits[3:5]}-{digits[5:]}"


def normalize_record(raw: dict) -> dict:
    """수집기에서 받은 원본 dict를 정규화."""
    out = dict(raw)
    if "사업자번호" in out:
        out["사업자번호"] = normalize_biz_number(out["사업자번호"])
    if "설립일" in out:
        out["설립일"] = normalize_date(out["설립일"])
    for key in ("매출액", "영업이익", "당기순이익", "종업원수"):
        if key in out:
            out[key] = normalize_amount(out[key])
    return out
