"""비밀값 가리기: 로그·오류 문구·화면에 계정 값이 남지 않도록 한다.

Playwright 는 입력 실패 시 오류 문구(Call log)에 입력하려던 값을 그대로 넣는다.
예: fill("비밀번호") → 'Timeout ... - fill("비밀번호")'. 그래서 로그로 나가는 모든 문자열을
마지막에 한 번 더 걸러 낸다.
"""
from __future__ import annotations

import os

MASK = "***"
_ENV_KEYS = ("NICE_PW", "NICE_ID")
_MIN_LEN = 3            # 너무 짧은 값은 일반 글자와 겹쳐 오탐이 많아 가리지 않는다


def secret_values(extra: tuple[str, ...] | list[str] = ()) -> list[str]:
    """가릴 값 목록: 환경변수의 계정 값 + 호출자가 넘긴 값. 긴 것부터(부분 겹침 방지)."""
    vals = {os.environ.get(k, "") for k in _ENV_KEYS} | set(extra or ())
    return sorted((v for v in vals if v and len(v) >= _MIN_LEN and v != "mock"),
                  key=len, reverse=True)


def redact(text, extra: tuple[str, ...] | list[str] = ()) -> str:
    """text 안의 계정 값을 *** 로 바꾼다. None·비문자열도 받는다."""
    s = "" if text is None else str(text)
    for v in secret_values(extra):
        if v in s:
            s = s.replace(v, MASK)
    return s
