"""체크포인트 저장/로드 - 중단 시 처리분 보존 + 다음 실행 시 재개 지원.

체크포인트 파일은 입력 엑셀과 짝지어 같은 폴더에 저장됩니다.
예: 고객사목록.xlsx → 고객사목록.progress.json
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from .timeutil import now_seoul

CHECKPOINT_SUFFIX = ".progress.json"


def path_for(input_path: str) -> str:
    base, _ = os.path.splitext(input_path)
    return base + CHECKPOINT_SUFFIX


def exists(input_path: str) -> bool:
    return os.path.exists(path_for(input_path))


def load(input_path: str) -> dict | None:
    """체크포인트 로드. 없거나 파싱 실패 시 None."""
    p = path_for(input_path)
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def save(input_path: str, state: dict) -> bool:
    """원자적 저장: 임시 파일 작성 후 rename."""
    p = path_for(input_path)
    tmp = p + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp, p)
        return True
    except OSError:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False


def clear(input_path: str) -> None:
    p = path_for(input_path)
    if os.path.exists(p):
        try:
            os.remove(p)
        except OSError:
            pass


def summarize(state: dict) -> str:
    """다이얼로그에 보여줄 한 줄 요약."""
    saved = state.get("saved_at", "?")
    done = len(state.get("processed_keys", []))
    total = state.get("total", "?")
    years = state.get("finance_years", 1)
    return f"{saved} 저장됨 / 진행 {done}/{total}건 / 재무 {years}개년"


def build_state(processed_keys: list[str], records: list[dict],
                unfound: list[dict], ambiguous: list[dict],
                total: int, finance_years: int) -> dict:
    return {
        "saved_at": now_seoul().strftime("%Y-%m-%d %H:%M:%S"),
        "total": total,
        "finance_years": finance_years,
        "processed_keys": processed_keys,
        "records": records,
        "unfound": unfound,
        "ambiguous": ambiguous,
    }
