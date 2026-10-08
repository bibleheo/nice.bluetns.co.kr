"""서버에서 NICE 로그인 경로만 점검 — 실패 경로에서 스크린샷이 생기는지 확인용.

  docker compose exec nice-worker python -m nice_bizline.app.server.login_check           # 정상 계정
  docker compose exec nice-worker python -m nice_bizline.app.server.login_check --wrong   # 일부러 틀린 비밀번호

출력: 로그인 결과(성공/실패 + 화면 문구), 실행 전·후 debug/ 폴더 파일 목록(이름만).
비밀번호 값은 어디에도 출력하지 않는다.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import yaml

from ..core.collector import CollectorError, NiceBizlineCollector

_ROOT = Path(__file__).resolve().parents[2]


def _list_debug(d: Path) -> list[str]:
    return sorted(p.name for p in d.glob("*.png")) if d.is_dir() else []


def main() -> int:
    wrong = "--wrong" in sys.argv
    data_dir = Path(os.environ.get("DATA_DIR", "/data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    os.chdir(data_dir)                          # worker 와 동일: debug/ 는 /data/debug
    debug_dir = data_dir / "debug"

    cfg = yaml.safe_load((_ROOT / "config.yaml").read_text(encoding="utf-8"))
    uid = os.environ.get("NICE_ID", "")
    pw = "wrong-password-on-purpose" if wrong else os.environ.get("NICE_PW", "")
    if not uid or not pw:
        print("NICE_ID/NICE_PW 환경변수가 없습니다 (env_file 확인)")
        return 2

    before = _list_debug(debug_dir)
    print(f"[점검 모드] {'일부러 틀린 비밀번호' if wrong else '정상 계정'}")
    print(f"[debug/ 실행 전] {len(before)}개: {before}")

    col = NiceBizlineCollector(cfg)
    try:
        col.login(uid, pw)
        print("[로그인] 성공")
        rc = 0
    except CollectorError as e:
        print(f"[로그인] 실패 — 화면/오류 문구: {e}")
        rc = 1
    except Exception as e:                      # 네트워크 등
        print(f"[로그인] 예외 — {type(e).__name__}: {e}")
        rc = 1
    finally:
        try:
            col.close()
        except Exception:
            pass

    after = _list_debug(debug_dir)
    new = [n for n in after if n not in before]
    print(f"[debug/ 실행 후] {len(after)}개: {after}")
    print(f"[이번 실행에서 새로 생긴 스크린샷] {len(new)}개: {new}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
