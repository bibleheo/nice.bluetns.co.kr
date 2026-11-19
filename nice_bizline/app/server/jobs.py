"""파일 기반 작업 큐 - nice-web(등록)과 nice-worker(실행)가 공유 볼륨으로 주고받는다.

디렉터리 구조 (DATA_DIR, 기본 /data):
  jobs/<job_id>/
    job.json        작업 메타 (상태·소유자·옵션·진행률·집계)
    input.xlsx      업로드 원본
    result.xlsx     결과 (완료 시)
    log.txt         실행 로그
    cancel          취소 요청 플래그 (존재 여부만 사용)
    input.xlsx.progress.json  체크포인트 (pipeline이 input 경로 옆에 저장)

설계 원칙:
- DB 없이 디렉터리·JSON만 사용 → 재배포·재시작에 안전, 백업 쉬움
- job.json 쓰기는 임시파일 + os.replace 로 원자적
- worker 는 하나(동시 실행 1)라는 전제 → claim 경쟁 없음
"""
from __future__ import annotations

import json
import os
import shutil
import time
import uuid

from ..core.timeutil import now_seoul

PENDING = "pending"
RUNNING = "running"
DONE = "done"
STOPPED = "stopped"      # 안전 정지(재개 가능 상태로 종료)
CANCELED = "canceled"
PAUSED = "paused"        # 사용자가 일시정지 (이어서 재개 가능, 자동 재시작 안 함)
ERROR = "error"

FINISHED = (DONE, STOPPED, CANCELED, PAUSED, ERROR)

# 안전 정지된 작업을 서버가 스스로 다시 시작하기까지 기다리는 시간(분). 0 이면 끔.
AUTO_RESUME_MIN = int(os.environ.get("AUTO_RESUME_MIN", "10") or 0)
# 자동 재시작을 이만큼 연달아 해도 한 건도 더 처리하지 못하면 그만둔다 (사람이 봐야 하는 장애).
AUTO_RESUME_MAX_NO_PROGRESS = 3


def _processed(job: dict) -> int:
    return int((job.get("counts") or {}).get("processed")
               or (job.get("progress") or {}).get("current") or 0)


def auto_resume_state(job: dict) -> tuple[bool, int]:
    """(자동 재시작 대상인가, 남은 초). 화면 안내와 worker 가 같은 규칙을 쓴다."""
    if job.get("status") != STOPPED or AUTO_RESUME_MIN <= 0:
        return False, 0
    ar = job.get("auto_resume") or {}
    if int(ar.get("no_progress") or 0) >= AUTO_RESUME_MAX_NO_PROGRESS:
        return False, 0
    import datetime as _dt
    try:
        fin = _dt.datetime.strptime(job.get("finished_at") or "", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return True, 0
    elapsed = (now_seoul().replace(tzinfo=None) - fin).total_seconds()
    return True, max(0, int(AUTO_RESUME_MIN * 60 - elapsed))


def _ts() -> str:
    return now_seoul().strftime("%Y-%m-%d %H:%M:%S")


class JobStore:
    def __init__(self, data_dir: str):
        self.root = os.path.join(data_dir, "jobs")
        os.makedirs(self.root, exist_ok=True)

    # ── 경로 ──
    def job_dir(self, job_id: str) -> str:
        return os.path.join(self.root, job_id)

    def input_path(self, job_id: str) -> str:
        return os.path.join(self.job_dir(job_id), "input.xlsx")

    def result_path(self, job_id: str) -> str:
        return os.path.join(self.job_dir(job_id), "result.xlsx")

    def log_path(self, job_id: str) -> str:
        return os.path.join(self.job_dir(job_id), "log.txt")

    def _meta_path(self, job_id: str) -> str:
        return os.path.join(self.job_dir(job_id), "job.json")

    def _cancel_path(self, job_id: str) -> str:
        return os.path.join(self.job_dir(job_id), "cancel")

    # ── 생성/조회 ──
    def create(self, *, name: str, input_bytes: bytes, owner_email: str,
               options: dict | None = None, input_total: int | None = None) -> dict:
        job_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        os.makedirs(self.job_dir(job_id), exist_ok=True)
        with open(self.input_path(job_id), "wb") as f:
            f.write(input_bytes)
        job = {
            "id": job_id,
            "name": name,
            "owner_email": owner_email,
            "status": PENDING,
            "created_at": _ts(),
            "started_at": None,
            "finished_at": None,
            "options": options or {},
            # 입력 회사 수. 대기 중 예상 시간·"처리 N / 입력 M" 표시에 쓴다.
            "input_total": int(input_total or 0),
            "progress": {"current": 0, "total": 0, "name": ""},
            "counts": {},
            "error": "",
            "downloads": [],
        }
        self.save(job)
        return job

    def get(self, job_id: str) -> dict | None:
        try:
            with open(self._meta_path(job_id), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return None

    def list(self) -> list[dict]:
        out = []
        try:
            ids = sorted(os.listdir(self.root))
        except OSError:
            return out
        for job_id in ids:
            job = self.get(job_id)
            if job:
                out.append(job)
        return out

    def save(self, job: dict) -> None:
        path = self._meta_path(job["id"])
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(job, f, ensure_ascii=False, indent=1)
        os.replace(tmp, path)

    # ── worker 쪽 ──
    def claim_next(self) -> dict | None:
        """가장 오래된 pending 작업을 running 으로 전환해 반환."""
        for job in self.list():
            if job["status"] == PENDING:
                job["status"] = RUNNING
                job["started_at"] = _ts()
                self.save(job)
                return job
        return None

    def reset_stale_running(self) -> list[str]:
        """worker 재시작 시: 죽은 채 running 으로 남은 작업을 pending 으로 되돌림.

        체크포인트가 input 옆에 있으므로 재실행 시 멈춘 지점부터 이어서 돈다.
        """
        reset = []
        for job in self.list():
            if job["status"] == RUNNING:
                job["status"] = PENDING
                self.save(job)
                reset.append(job["id"])
        return reset

    def finish(self, job_id: str, status: str, counts: dict | None = None,
               error: str = "", stop_reason: str = "") -> None:
        job = self.get(job_id)
        if not job:
            return
        job["status"] = status
        job["finished_at"] = _ts()
        if counts is not None:
            job["counts"] = counts
        if error:
            job["error"] = str(error)[:500]
        if stop_reason:
            job["stop_reason"] = str(stop_reason)[:200]
        self.save(job)

    def set_input_total(self, job_id: str, total: int) -> None:
        """예전 작업(input_total 없이 등록됨)에 입력 회사 수를 채운다."""
        job = self.get(job_id)
        if job and not job.get("input_total"):
            job["input_total"] = int(total)
            self.save(job)

    def update_progress(self, job_id: str, current: int, total: int, name: str) -> None:
        job = self.get(job_id)
        if not job:
            return
        job["progress"] = {"current": current, "total": total, "name": name}
        self.save(job)

    def append_log(self, job_id: str, line: str) -> None:
        try:
            with open(self.log_path(job_id), "a", encoding="utf-8") as f:
                f.write(line.rstrip("\n") + "\n")
        except OSError:
            pass

    def tail_log(self, job_id: str, lines: int = 30) -> str:
        try:
            with open(self.log_path(job_id), encoding="utf-8") as f:
                return "".join(f.readlines()[-lines:])
        except OSError:
            return ""

    # ── 취소 ──
    def request_cancel(self, job_id: str, email: str) -> bool:
        """본인 작업만 취소. pending 은 즉시 취소, running 은 플래그로 안전 정지."""
        job = self.get(job_id)
        if not job or job.get("owner_email") != email:
            return False
        if job["status"] == PENDING:
            job["status"] = CANCELED
            job["finished_at"] = _ts()
            self.save(job)
            return True
        if job["status"] == RUNNING:
            with open(self._cancel_path(job_id), "w") as f:
                f.write(_ts())
            return True
        return False

    def cancel_requested(self, job_id: str) -> bool:
        return os.path.exists(self._cancel_path(job_id))

    # ── 일시정지 ──
    def _pause_path(self, job_id: str) -> str:
        return os.path.join(self.job_dir(job_id), "pause")

    def request_pause(self, job_id: str, email: str) -> bool:
        """본인 작업만 일시정지. 대기 중이면 바로, 실행 중이면 지금 회사까지 마치고 멈춘다."""
        job = self.get(job_id)
        if not job or job.get("owner_email") != email:
            return False
        if job["status"] == PENDING:
            job["status"] = PAUSED
            job["finished_at"] = _ts()
            self.save(job)
            return True
        if job["status"] == RUNNING:
            with open(self._pause_path(job_id), "w") as f:
                f.write(_ts())
            return True
        return False

    def pause_requested(self, job_id: str) -> bool:
        return os.path.exists(self._pause_path(job_id))

    def stop_requested(self, job_id: str) -> bool:
        """worker 가 매 회사 전에 확인: 취소 또는 일시정지 요청."""
        return self.cancel_requested(job_id) or self.pause_requested(job_id)

    def _clear_flags(self, job_id: str) -> None:
        for p in (self._cancel_path(job_id), self._pause_path(job_id)):
            try:
                os.remove(p)
            except OSError:
                pass

    def restart(self, job_id: str, email: str, options: dict | None = None) -> bool:
        """처음부터 다시 시작: 저장된 진행분(체크포인트)과 결과 파일을 지우고 대기열에 넣는다.

        options 를 주면 등록 옵션(대조 기준 등)을 바꿔서 다시 돈다. 입력 엑셀은 그대로 쓴다.
        실행 중·대기 중인 작업은 먼저 멈춘 뒤에 할 수 있다. 본인 작업만 가능.
        """
        job = self.get(job_id)
        if not job or job.get("owner_email") != email:
            return False
        if job["status"] not in FINISHED:
            return False
        self._clear_flags(job_id)
        base, _ = os.path.splitext(self.input_path(job_id))
        for p in (base + ".progress.json", self.result_path(job_id)):
            try:
                os.remove(p)
            except OSError:
                pass
        if options is not None:
            job["options"] = options
        job["status"] = PENDING
        job["started_at"] = None
        job["finished_at"] = None
        job["error"] = ""
        job["counts"] = {}
        job["progress"] = {"current": 0, "total": 0, "name": ""}
        job.pop("stop_reason", None)
        job.pop("auto_resume", None)
        job.setdefault("restarted", []).append(_ts())
        self.save(job)
        self.append_log(job_id, f"{now_seoul().strftime('%m-%d %H:%M:%S')}  [info] 처음부터 다시 시작 "
                                f"(이전 진행분 삭제)")
        return True

    def requeue(self, job_id: str, email: str) -> bool:
        """끝난(취소·정지·오류) 작업을 멈춘 지점부터 이어서 돌리도록 대기열에 다시 넣는다.

        체크포인트(input 옆 .progress.json)가 그대로 있으므로 worker 가 resume 으로
        이미 처리한 건을 건너뛰고 이어서 수집한다. 본인 작업만 가능.
        """
        job = self.get(job_id)
        if not job or job.get("owner_email") != email:
            return False
        if job["status"] not in (STOPPED, CANCELED, PAUSED, ERROR):
            return False
        self._clear_flags(job_id)
        job["status"] = PENDING
        job["finished_at"] = None
        job["error"] = ""
        job.pop("stop_reason", None)
        if job.get("auto_resume"):
            job["auto_resume"]["no_progress"] = 0     # 사람이 직접 재개 → 자동 재시작 다시 허용
        job.setdefault("requeued", []).append(_ts())
        self.save(job)
        return True

    def auto_requeue_stopped(self) -> list[str]:
        """안전 정지된 작업을 AUTO_RESUME_MIN 분 뒤 대기열에 다시 넣는다 (밤새 멈춰 있지 않도록).

        취소·오류는 건드리지 않는다. 연달아 재시작해도 진전이 없으면 그만둔다.
        """
        out = []
        for job in self.list():
            ok, wait = auto_resume_state(job)
            if not ok or wait > 0:
                continue
            ar = dict(job.get("auto_resume") or {})
            done = _processed(job)
            if ar.get("count") and done <= int(ar.get("last_processed") or 0):
                ar["no_progress"] = int(ar.get("no_progress") or 0) + 1
            else:
                ar["no_progress"] = 0
            if ar["no_progress"] >= AUTO_RESUME_MAX_NO_PROGRESS:
                job["auto_resume"] = ar          # 화면이 '자동 재시작 중단'을 알 수 있게 기록만
                self.save(job)
                continue
            ar["count"] = int(ar.get("count") or 0) + 1
            ar["last_processed"] = done
            ar["last_at"] = _ts()
            job["auto_resume"] = ar
            job["status"] = PENDING
            job["finished_at"] = None
            job.pop("stop_reason", None)
            self.save(job)
            out.append(job["id"])
        return out

    # ── 다운로드 기록 ──
    def record_download(self, job_id: str, email: str) -> None:
        job = self.get(job_id)
        if not job:
            return
        job.setdefault("downloads", []).append({"email": email, "at": _ts()})
        self.save(job)

    # ── 보관 기간 정리 ──
    def cleanup(self, retention_days: int = 30) -> list[str]:
        """끝난 지 retention_days 지난 작업 디렉터리를 삭제."""
        removed = []
        cutoff = time.time() - retention_days * 86400
        for job in self.list():
            if job["status"] not in FINISHED:
                continue
            try:
                mtime = os.path.getmtime(self._meta_path(job["id"]))
            except OSError:
                continue
            if mtime < cutoff:
                shutil.rmtree(self.job_dir(job["id"]), ignore_errors=True)
                removed.append(job["id"])
        return removed
