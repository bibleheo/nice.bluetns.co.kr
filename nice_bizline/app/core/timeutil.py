"""표시용 시각 - 항상 서울(Asia/Seoul) 기준.

Codespaces 등 UTC 환경에서 실행해도 결과 엑셀/로그의 시각이
한국 시간으로 기록되도록 한다.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
    _SEOUL = ZoneInfo("Asia/Seoul")
except Exception:                       # tzdata 없는 환경 폴백 (KST=UTC+9 고정)
    _SEOUL = timezone(timedelta(hours=9), name="KST")


def now_seoul() -> datetime:
    return datetime.now(_SEOUL)
