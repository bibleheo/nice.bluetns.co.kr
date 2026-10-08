"""실행별 파일 로깅 - 결과 엑셀과 짝지어 .log 파일 생성."""
from __future__ import annotations

import logging
import os
from datetime import datetime
from .timeutil import now_seoul


_LEVEL_MAP = {
    "info": logging.INFO,
    "warn": logging.WARNING,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


def setup_run_logger(input_path: str, level: int = logging.INFO) -> tuple[logging.Logger, str]:
    """입력 파일과 짝지어 로그 파일을 만든다.

    예: 고객사목록.xlsx → 고객사목록_나이스비즈라인로그_YYYYMMDD_HHMM.log
    반환: (logger, 로그파일 경로)
    """
    base, _ = os.path.splitext(input_path)
    ts = now_seoul().strftime("%Y%m%d_%H%M")
    log_path = f"{base}_나이스비즈라인로그_{ts}.log"

    logger = logging.getLogger(f"nice_bizline.run.{ts}")
    logger.setLevel(level)
    # 기존 핸들러 정리 (재시작 안전)
    for h in list(logger.handlers):
        try:
            h.close()
        except Exception:
            pass
        logger.removeHandler(h)

    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s  [%(levelname)s]  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logger.addHandler(handler)
    logger.propagate = False
    return logger, log_path


def write(logger: logging.Logger, level: str, msg: str) -> None:
    """UI 콜백의 (level_string, message) 형식을 logging 레벨로 변환."""
    logger.log(_LEVEL_MAP.get(level, logging.INFO), msg)


def close(logger: logging.Logger) -> None:
    for h in list(logger.handlers):
        try:
            h.close()
        except Exception:
            pass
        logger.removeHandler(h)
