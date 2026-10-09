"""nice-worker: 공유 볼륨의 작업 큐를 하나씩 실행하는 데몬.

  python -m nice_bizline.app.server.worker

환경변수:
  DATA_DIR          작업 큐 루트 (기본 /data)
  NICE_ID, NICE_PW  나이스비즈라인 계정 (env_file 로만 주입; 코드·로그에 남기지 않음)
  MOCK_MODE=1       사이트 접속 없이 MockCollector 로 실행 (시험용)
  RETENTION_DAYS    끝난 작업 보관 일수 (기본 30)
  DEBUG_RETENTION_DAYS  debug/ 스크린샷 보관 일수 (기본 7)
  POLL_SEC          큐 폴링 간격 초 (기본 5)

동작:
- 시작 시 running 으로 남은(죽은) 작업을 pending 으로 되돌려 체크포인트부터 재개
- 작업은 전체에서 한 번에 하나만 실행 (NICE 계정 동시접속 1명)
- 화면(nice-web)의 취소 요청(cancel 플래그)은 pipeline stop_check 로 안전 정지
- 하루 한 번 보관 기간 지난 작업·스크린샷 삭제
"""
from __future__ import annotations

import logging
import os
import sys
import time
import traceback
from pathlib import Path

import yaml

from ..core.collector import MockCollector, NiceBizlineCollector
from ..core.pipeline import PipelineOptions, PipelineState, run_pipeline
from ..core.timeutil import now_seoul
from ..excelio.reader import read_company_list
from ..excelio.writer import write_results
from . import jobs as J

log = logging.getLogger("nice-worker")

_ROOT = Path(__file__).resolve().parents[2]       # nice_bizline/
CONFIG_PATH = _ROOT / "config.yaml"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _load_cfg() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def run_job(store: J.JobStore, job: dict, cfg: dict, collector=None) -> str:
    """작업 하나 실행. 최종 상태 문자열을 반환."""
    job_id = job["id"]
    inp = store.input_path(job_id)
    opts_in = job.get("options") or {}

    def say(level: str, msg: str) -> None:
        store.append_log(job_id, f"{now_seoul().strftime('%H:%M:%S')}  [{level}] {msg}")

    try:
        companies = read_company_list(inp)
    except Exception as e:
        say("error", f"입력 엑셀 읽기 실패: {e}")
        store.finish(job_id, J.ERROR, error=f"입력 읽기 실패: {e}")
        return J.ERROR
    if not companies:
        store.finish(job_id, J.ERROR, error="입력이 비어 있음")
        return J.ERROR
    store.set_input_total(job_id, len(companies))     # 예전 작업 보정

    mock = os.environ.get("MOCK_MODE") == "1"
    if collector is None:
        collector = MockCollector(cfg) if mock else NiceBizlineCollector(cfg)
    user_id = "mock" if mock else os.environ.get("NICE_ID", "")
    password = "mock" if mock else os.environ.get("NICE_PW", "")
    if not mock and not (user_id and password):
        say("error", "NICE_ID/NICE_PW 환경변수가 없습니다 (env_file 확인)")
        store.finish(job_id, J.ERROR, error="서버 계정 미설정")
        return J.ERROR

    opts = PipelineOptions(
        user_id=user_id, password=password, companies=companies,
        finance_years=1, input_path=inp,
        resume=True,                                   # 재배포 후에도 멈춘 지점부터
        checkpoint_every=10,
        narrow_fields=opts_in.get("narrow_fields"),
        result_filter=opts_in.get("result_filter"),
    )
    state = PipelineState()
    say("info", f"작업 시작 - {len(companies)}건, 소유자 {job.get('owner_email', '')}")

    # 컨테이너 stdout(docker compose logs)에도 운영자가 볼 핵심 이벤트를 남긴다.
    # 계정 ID·비밀번호는 어떤 메시지에도 포함되지 않는다.
    _STDOUT_MARKERS = ("로그인 성공", "자동 재로그인", "재로그인 실패", "세션 연장",
                       "재개 시작", "체크포인트 로드", "처리 확인", "안전 정지", "사용자 중단")
    progress_every = _env_int("LOG_EVERY", 10)
    tag = f"[{job_id}]"
    last_progress_save = 0.0
    last_error_msg = ""
    try:
        for ev in run_pipeline(collector, cfg, opts, state,
                               stop_check=lambda: store.cancel_requested(job_id)):
            t = ev.get("type")
            if t == "log":
                say(ev["level"], ev["message"])
                msg = ev["message"]
                if ev["level"] == "error":
                    last_error_msg = msg
                if ev["level"] == "error" or any(m in msg for m in _STDOUT_MARKERS):
                    (log.error if ev["level"] == "error" else log.info)("%s %s", tag, msg)
            elif t == "progress":
                cur, total = ev["current"], ev["total"]
                # job.json 갱신은 2초에 한 번으로 제한 (디스크 부담 완화)
                if time.time() - last_progress_save > 2:
                    store.update_progress(job_id, cur, total, ev["name"])
                    last_progress_save = time.time()
                # N건마다 진행 상황을 stdout 에 요약
                if progress_every and cur % progress_every == 0:
                    c = _live_counts(state)
                    log.info("%s 진행 %d/%d  성공 %d · 미발견 %d · 확인필요 %d · 오류 %d · 필터제외 %d",
                             tag, cur, total, c["성공"], c["미발견"], c["확인필요"], c["오류"],
                             state.summary.get("필터제외", 0))
    except Exception as e:
        say("error", f"예상치 못한 오류: {e}")
        store.append_log(job_id, traceback.format_exc())
        store.finish(job_id, J.ERROR, error=str(e))
        return J.ERROR

    # 결과 저장 (중단·정지여도 처리분은 저장)
    try:
        write_results(store.result_path(job_id), records=state.records,
                      unfound=state.unfound, ambiguous=state.ambiguous,
                      summary=state.summary, finance_years=1)
    except Exception as e:
        say("error", f"결과 저장 실패: {e}")
        store.finish(job_id, J.ERROR, error=f"결과 저장 실패: {e}")
        return J.ERROR

    s = state.summary
    # total = 입력 회사 수, processed = 지금까지 처리한 회사 수(체크포인트 누적)
    counts = {k: s.get(k, 0) for k in ("total", "success", "not_found", "ambiguous", "error")}
    counts["필터제외"] = s.get("필터제외", 0)
    counts["processed"] = len(state.processed_keys)

    if store.cancel_requested(job_id):
        status = J.CANCELED
    elif s.get("stopped"):
        status = J.STOPPED          # 연결 장애 등으로 안전 정지 → 재큐잉 가능
    else:
        status = J.DONE
    if status == J.DONE:
        store.update_progress(job_id, s.get("total", 0), s.get("total", 0), "완료")
    else:
        # 멈춘 지점을 남긴다 (화면의 "1,630 / 16,000 · 처리분 저장됨")
        store.update_progress(job_id, counts["processed"], s.get("total", 0), "")
    say("info", f"작업 종료 - 상태 {status}, 요약 {counts}")
    store.finish(job_id, status, counts=counts,
                 stop_reason=stop_reason_text(last_error_msg) if status == J.STOPPED else "")
    return status


def stop_reason_text(msg: str) -> str:
    """안전 정지 로그를 화면용 이유 문구로 바꾼다. 예: '사이트 연결 오류가 5번 이어져'"""
    import re
    m = re.search(r"연속 (\d+)건 오류", msg or "")
    if m:
        return f"사이트 연결 오류가 {m.group(1)}번 이어져"
    if "재로그인" in (msg or ""):
        return "다시 로그인이 계속 실패해"
    return ""


def _live_counts(state: PipelineState) -> dict:
    """진행 중 상태 집계 (stdout 진행 로그용)."""
    c = {"성공": 0, "미발견": 0, "확인필요": 0, "오류": 0}
    for r in state.records:
        s = r.get("조회상태")
        if s in c:
            c[s] += 1
    return c


def cleanup(store: J.JobStore) -> None:
    removed = store.cleanup(_env_int("RETENTION_DAYS", 30))
    if removed:
        log.info("보관 기간 경과 작업 삭제: %d건", len(removed))
    # debug/ 스크린샷 정리 (collector 가 cwd/debug 에 저장)
    dbg = Path("debug")
    if dbg.is_dir():
        cutoff = time.time() - _env_int("DEBUG_RETENTION_DAYS", 7) * 86400
        for p in dbg.glob("*.png"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                pass


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    data_dir = os.environ.get("DATA_DIR", "/data")
    os.makedirs(data_dir, exist_ok=True)
    os.chdir(data_dir)                 # debug/ 스크린샷도 볼륨 안에 저장
    store = J.JobStore(data_dir)
    cfg = _load_cfg()
    poll = _env_int("POLL_SEC", 5)

    reset = store.reset_stale_running()
    if reset:
        log.info("재시작: 중단된 작업 %d건을 재개 대기열로 복귀", len(reset))
    log.info("nice-worker 시작 (data=%s, mock=%s)", data_dir, os.environ.get("MOCK_MODE") == "1")

    last_cleanup = 0.0
    while True:
        job = store.claim_next()
        if job is None:
            if time.time() - last_cleanup > 86400:
                cleanup(store)
                last_cleanup = time.time()
            time.sleep(poll)
            continue
        log.info("작업 시작 %s (%s)", job["id"], job.get("name"))
        try:
            status = run_job(store, job, cfg)
        except Exception as e:         # run_job 내부에서 못 잡은 경우의 최후 방어
            log.exception("작업 실패 %s", job["id"])
            store.finish(job["id"], J.ERROR, error=str(e))
            status = J.ERROR
        log.info("작업 종료 %s → %s", job["id"], status)


if __name__ == "__main__":
    sys.exit(main())
