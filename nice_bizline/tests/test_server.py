"""서버 모드(작업 큐 + worker) 테스트 - 사이트 접속 없이 MockCollector 사용."""
import os
import sys
import time
from pathlib import Path

import openpyxl
import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from nice_bizline.app.core.collector import MockCollector  # noqa: E402
from nice_bizline.app.server import jobs as J  # noqa: E402
from nice_bizline.app.server.worker import run_job  # noqa: E402

CFG = Path(__file__).resolve().parents[1] / "config.yaml"


def _cfg():
    return yaml.safe_load(CFG.read_text(encoding="utf-8"))


def _xlsx_bytes(rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    import io
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.fixture
def store(tmp_path):
    return J.JobStore(str(tmp_path))


class TestJobStore:
    def test_create_list_claim(self, store):
        a = store.create(name="a.xlsx", input_bytes=b"x", owner_email="a@b.kr")
        time.sleep(1.1)   # id 가 초 단위 timestamp → 순서 보장
        b = store.create(name="b.xlsx", input_bytes=b"y", owner_email="c@d.kr")
        ids = [j["id"] for j in store.list()]
        assert ids == [a["id"], b["id"]]
        first = store.claim_next()
        assert first["id"] == a["id"] and first["status"] == J.RUNNING
        assert store.get(b["id"])["status"] == J.PENDING

    def test_cancel_only_owner(self, store):
        j = store.create(name="a.xlsx", input_bytes=b"x", owner_email="me@b.kr")
        assert store.request_cancel(j["id"], "other@b.kr") is False
        assert store.get(j["id"])["status"] == J.PENDING
        assert store.request_cancel(j["id"], "me@b.kr") is True
        assert store.get(j["id"])["status"] == J.CANCELED

    def test_cancel_running_sets_flag(self, store):
        j = store.create(name="a.xlsx", input_bytes=b"x", owner_email="me@b.kr")
        store.claim_next()
        assert store.request_cancel(j["id"], "me@b.kr") is True
        assert store.cancel_requested(j["id"]) is True

    def test_reset_stale_running(self, store):
        j = store.create(name="a.xlsx", input_bytes=b"x", owner_email="me@b.kr")
        store.claim_next()
        assert store.reset_stale_running() == [j["id"]]
        assert store.get(j["id"])["status"] == J.PENDING

    def test_cleanup_removes_old_finished(self, store):
        j = store.create(name="a.xlsx", input_bytes=b"x", owner_email="me@b.kr")
        store.finish(j["id"], J.DONE)
        meta = os.path.join(store.job_dir(j["id"]), "job.json")
        old = time.time() - 40 * 86400
        os.utime(meta, (old, old))
        assert store.cleanup(retention_days=30) == [j["id"]]
        assert store.get(j["id"]) is None

    def test_requeue_canceled_job_resumes_from_checkpoint(self, store, monkeypatch):
        """취소된 작업을 '이어서 재개'하면 체크포인트 이후 건만 처리한다."""
        monkeypatch.setenv("MOCK_MODE", "1")
        xlsx = _xlsx_bytes([["회사명"], ["삼성전자"], ["현대자동차"], ["없는회사ZZZ"]])
        j = store.create(name="list.xlsx", input_bytes=xlsx, owner_email="me@b.kr")
        store.claim_next()
        # 1차 실행: 시작 전 취소 → 아무것도 처리 안 하고 canceled (체크포인트는 finalize 가 저장)
        store.request_cancel(j["id"], "me@b.kr")
        assert run_job(store, j, _cfg(), collector=MockCollector(_cfg())) == J.CANCELED
        # 다른 사람은 재개 불가, 본인은 가능 → pending 으로 복귀, cancel 플래그 제거
        assert store.requeue(j["id"], "other@b.kr") is False
        assert store.requeue(j["id"], "me@b.kr") is True
        assert store.get(j["id"])["status"] == J.PENDING
        assert store.cancel_requested(j["id"]) is False
        # 2차 실행: 끝까지 돌아 done
        store.claim_next()
        assert run_job(store, store.get(j["id"]), _cfg(), collector=MockCollector(_cfg())) == J.DONE
        assert store.get(j["id"])["counts"]["success"] == 2

    def test_record_download(self, store):
        j = store.create(name="a.xlsx", input_bytes=b"x", owner_email="me@b.kr")
        store.record_download(j["id"], "me@b.kr")
        assert store.get(j["id"])["downloads"][0]["email"] == "me@b.kr"


class TestWorkerRunJob:
    def test_runs_job_with_mock_and_writes_result(self, store, monkeypatch):
        monkeypatch.setenv("MOCK_MODE", "1")
        xlsx = _xlsx_bytes([["회사명"], ["삼성전자"], ["현대자동차"], ["없는회사ZZZ"]])
        j = store.create(name="list.xlsx", input_bytes=xlsx, owner_email="me@b.kr",
                         options={"narrow_fields": None, "result_filter": None})
        store.claim_next()
        status = run_job(store, j, _cfg(), collector=MockCollector(_cfg()))
        assert status == J.DONE
        meta = store.get(j["id"])
        assert meta["counts"]["success"] == 2
        assert meta["counts"]["not_found"] == 1
        assert os.path.exists(store.result_path(j["id"]))
        assert "작업 종료" in store.tail_log(j["id"])

    def test_result_filter_option_applied(self, store, monkeypatch):
        monkeypatch.setenv("MOCK_MODE", "1")
        xlsx = _xlsx_bytes([["회사명"], ["삼성전자"], ["현대자동차"]])
        j = store.create(name="list.xlsx", input_bytes=xlsx, owner_email="me@b.kr",
                         options={"result_filter": {"min_employees": 100000, "mode": "AND"}})
        store.claim_next()
        run_job(store, j, _cfg(), collector=MockCollector(_cfg()))
        meta = store.get(j["id"])
        assert meta["counts"]["success"] == 1          # 현대차(75091명) 제외
        assert meta["counts"]["필터제외"] == 1

    def test_cancel_before_start_is_respected(self, store, monkeypatch):
        """실행 직전 취소 플래그 → 첫 회사 전에 안전 정지, 상태 canceled."""
        monkeypatch.setenv("MOCK_MODE", "1")
        xlsx = _xlsx_bytes([["회사명"], ["삼성전자"]])
        j = store.create(name="list.xlsx", input_bytes=xlsx, owner_email="me@b.kr")
        store.claim_next()
        store.request_cancel(j["id"], "me@b.kr")
        status = run_job(store, j, _cfg(), collector=MockCollector(_cfg()))
        assert status == J.CANCELED
        assert store.get(j["id"])["counts"]["success"] == 0
