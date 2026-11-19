"""브라우저 복구·자동 재시작·로그 날짜·대조 기준 테스트."""
import datetime as dt
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from nice_bizline.app.core import pipeline as P  # noqa: E402
from nice_bizline.app.core.collector import MockCollector  # noqa: E402
from nice_bizline.app.core.matcher import select_matches  # noqa: E402
from nice_bizline.app.core.timeutil import now_seoul  # noqa: E402
from nice_bizline.app.server import jobs as J  # noqa: E402
from nice_bizline.app.server.worker import _match_fields  # noqa: E402

CFG = yaml.safe_load((Path(__file__).resolve().parents[1] / "config.yaml").read_text("utf-8"))


def _run(collector, companies, cfg=None, **kw):
    opts = P.PipelineOptions(user_id="u", password="p", companies=companies, finance_years=1, **kw)
    state = P.PipelineState()
    logs = [ev["message"] for ev in P.run_pipeline(collector, cfg or CFG, opts, state,
                                                   stop_check=lambda: False)
            if ev.get("type") == "log"]
    return state, logs


class _CrashOnce(MockCollector):
    """처음 검색에서 화면이 죽고, 브라우저를 새로 띄우기 전에는 로그인도 계속 실패한다."""

    def __init__(self):
        super().__init__()
        self.crashed = False
        self.dead = False
        self.restarts = 0

    def search(self, name):
        if not self.crashed:
            self.crashed = self.dead = True
            raise RuntimeError("Page.goto: Page crashed\nCall log:\n  - navigating")
        return super().search(name)

    def login(self, uid, pw):
        if self.dead:
            raise RuntimeError("Page.goto: Page crashed\nCall log:\n  - navigating")
        super().login(uid, pw)

    def restart(self, keep_session=True):
        self.restarts += 1
        self.dead = False


class TestBrowserRecovery:
    def test_crashed_page_recovers_by_restarting_browser(self, monkeypatch):
        monkeypatch.setattr(P, "_RELOGIN_DELAY_SCALE", 0)
        col = _CrashOnce()
        state, logs = _run(col, [{"회사명": "삼성전자"}])
        assert col.restarts == 1
        assert state.summary["success"] == 1 and not state.summary["stopped"]
        text = "\n".join(logs)
        assert "브라우저 새로 시작" in text
        assert "Call log" not in text                      # 오류는 첫 줄만 남긴다

    def test_periodic_recycle(self):
        cfg = dict(CFG, browser=dict(CFG["browser"], recycle_every=2))
        col = MockCollector()
        names = ["삼성전자", "현대자동차", "삼성전자a", "현대자동차b", "삼성전자c"]
        state, logs = _run(col, [{"회사명": n} for n in names], cfg=cfg)
        assert getattr(col, "restarts", 0) == 2            # 2건, 4건 처리 뒤
        assert state.summary["total"] == 5


class TestAutoResume:
    @pytest.fixture
    def store(self, tmp_path):
        return J.JobStore(str(tmp_path))

    def _stop(self, store, job, minutes_ago, processed):
        t = (now_seoul().replace(tzinfo=None) - dt.timedelta(minutes=minutes_ago))
        j = store.get(job["id"])
        j["status"] = J.STOPPED
        j["finished_at"] = t.strftime("%Y-%m-%d %H:%M:%S")
        j["counts"] = {"processed": processed}
        store.save(j)

    def test_requeues_only_after_wait(self, store):
        job = store.create(name="a.xlsx", input_bytes=b"x", owner_email="me@b.kr")
        self._stop(store, job, minutes_ago=3, processed=10)
        assert store.auto_requeue_stopped() == []
        ok, wait = J.auto_resume_state(store.get(job["id"]))
        assert ok and 0 < wait <= J.AUTO_RESUME_MIN * 60
        self._stop(store, job, minutes_ago=J.AUTO_RESUME_MIN + 1, processed=10)
        assert store.auto_requeue_stopped() == [job["id"]]
        assert store.get(job["id"])["status"] == J.PENDING

    def test_gives_up_without_progress_and_manual_resume_resets(self, store):
        job = store.create(name="a.xlsx", input_bytes=b"x", owner_email="me@b.kr")
        for _ in range(J.AUTO_RESUME_MAX_NO_PROGRESS):       # 처음 + 진전 없는 재시작들
            self._stop(store, job, minutes_ago=60, processed=10)
            assert store.auto_requeue_stopped() == [job["id"]]
        self._stop(store, job, minutes_ago=60, processed=10)
        assert store.auto_requeue_stopped() == []            # 진전 없음 3번 → 그만둠
        assert J.auto_resume_state(store.get(job["id"]))[0] is False
        assert store.requeue(job["id"], "me@b.kr")           # 사람이 재개하면 다시 허용
        self._stop(store, job, minutes_ago=60, processed=10)
        assert J.auto_resume_state(store.get(job["id"]))[0] is True

    def test_progress_resets_counter(self, store):
        job = store.create(name="a.xlsx", input_bytes=b"x", owner_email="me@b.kr")
        for n in (10, 10, 10, 50, 50, 50):
            self._stop(store, job, minutes_ago=60, processed=n)
            assert store.auto_requeue_stopped() == [job["id"]]

    def test_canceled_and_error_are_left_alone(self, store):
        a = store.create(name="a.xlsx", input_bytes=b"x", owner_email="me@b.kr")
        store.finish(a["id"], J.CANCELED)
        assert store.auto_requeue_stopped() == []


class TestLogDates:
    @pytest.fixture(autouse=True)
    def _ui(self):
        pytest.importorskip("streamlit")
        from nice_bizline.app.web import server_ui
        self.h = server_ui._humanize_log

    def test_dated_lines_survive_a_long_stop(self):
        raw = "\n".join([
            "11-17 19:16:00  [error] 재로그인이 계속 실패해 안전 정지합니다.",
            "11-19 09:41:00  [info] 작업 시작 - 16409건, 소유자 me",
            "11-19 09:41:20  [info] 로그인 성공",
        ])
        out = self.h(raw, start=dt.datetime(2026, 11, 17, 16, 17)).splitlines()
        assert out[0].startswith("- :gray[11/17 19:16]")
        assert out[1].startswith("- :gray[11/19 09:41]")      # 하루 넘게 멈췄어도 날짜가 맞다
        assert out[2].startswith("- :gray[09:41]")

    def test_old_undated_lines_still_parse(self):
        out = self.h("23:59:00  [info] 로그인 성공\n00:10:00  [info] 세션 연장 (+10분)",
                     start=dt.datetime(2026, 10, 9, 23, 0))
        assert "10/09 23:59" in out and "10/10 00:10" in out


SAMSUNG = {"회사명": "삼성전자(주)", "사업자번호": "124-81-00998", "대표자명": "한종희"}
SAME_NAME = {"회사명": "삼성전자(주)", "사업자번호": "999-99-99999", "대표자명": "홍길동"}


class TestMatchCriteria:
    def test_name_plus_biz_picks_the_one_with_that_number(self):
        r = select_matches({"회사명": "삼성전자", "사업자번호": "1248100998"},
                           [SAME_NAME, SAMSUNG], {}, ["사업자번호"])
        assert r.status == "biz" and r.picks == [SAMSUNG]

    def test_name_plus_biz_never_falls_back_to_name_only(self):
        r = select_matches({"회사명": "삼성전자", "사업자번호": "1111111111"},
                           [SAME_NAME, SAMSUNG], {}, ["사업자번호"])
        assert r.status == "nobiz" and r.picks == []

    def test_name_plus_ceo_ignores_biz_column(self):
        r = select_matches({"회사명": "삼성전자", "사업자번호": "1111111111", "대표자명": "홍길동"},
                           [SAME_NAME, SAMSUNG], {}, ["대표자명"])
        assert r.status == "single" and r.picks == [SAME_NAME]

    def test_name_ceo_biz_requires_both(self):
        q = {"회사명": "삼성전자", "사업자번호": "1248100998", "대표자명": "한종희"}
        assert select_matches(q, [SAMSUNG], {}, ["대표자명", "사업자번호"]).status == "biz"
        q["대표자명"] = "다른사람"
        assert select_matches(q, [SAMSUNG], {}, ["대표자명", "사업자번호"]).status == "mismatch"

    def test_name_only_collects_all_same_names(self):
        r = select_matches({"회사명": "삼성전자", "사업자번호": "1248100998"},
                           [SAME_NAME, SAMSUNG], {}, [])
        assert r.status == "multiple" and len(r.picks) == 2

    def test_row_without_biz_value_falls_back_to_other_criteria(self):
        r = select_matches({"회사명": "삼성전자", "대표자명": "한종희"},
                           [SAME_NAME, SAMSUNG], {}, ["대표자명", "사업자번호"])
        assert r.status == "single" and r.picks == [SAMSUNG]

    def test_legacy_none_keeps_old_behaviour(self):
        r = select_matches({"회사명": "삼성전자", "사업자번호": "1111111111"}, [SAMSUNG], {}, None)
        assert r.status == "single"                          # 예전: 번호가 없으면 이름으로 재판단

    def test_old_jobs_keep_automatic_biz(self):
        assert _match_fields({"narrow_fields": ["주소"]}) == ["주소", "사업자번호"]
        assert _match_fields({"narrow_fields": ["주소"], "match_version": 2}) == ["주소"]
        assert _match_fields({"narrow_fields": None}) is None


class TestBizSearchFallback:
    def test_found_by_biz_number_when_name_search_misses(self):
        """엑셀 상호가 NICE 상호와 달라 이름 검색에 안 나와도 사업자번호로 찾는다."""
        state, logs = _run(MockCollector(), [{"회사명": "전혀다른상호", "사업자번호": "124-81-00998"}],
                           narrow_fields=["사업자번호"])
        assert state.summary["success"] == 1
        assert any("사업자번호 검색으로 찾음" in m for m in logs)
        assert state.records[0]["사업자번호"].replace("-", "") == "1248100998"

    def test_unknown_biz_number_is_not_found(self):
        state, _ = _run(MockCollector(), [{"회사명": "삼성전자", "사업자번호": "111-11-11111"}],
                        narrow_fields=["사업자번호"])
        assert state.summary["success"] == 0 and state.summary["not_found"] == 1
        assert "사업자번호" in state.unfound[0]["사유"]


class TestPauseAndRestart:
    @pytest.fixture
    def store(self, tmp_path):
        return J.JobStore(str(tmp_path))

    def _xlsx(self, tmp_path, names):
        import openpyxl
        wb = openpyxl.Workbook()
        wb.active.append(["회사명"])
        for n in names:
            wb.active.append([n])
        p = tmp_path / "in.xlsx"
        wb.save(p)
        return p.read_bytes()

    def test_pause_pending_is_immediate_and_only_owner(self, store):
        j = store.create(name="a.xlsx", input_bytes=b"x", owner_email="me@b.kr")
        assert store.request_pause(j["id"], "other@b.kr") is False
        assert store.request_pause(j["id"], "me@b.kr") is True
        assert store.get(j["id"])["status"] == J.PAUSED
        assert store.claim_next() is None                     # 일시정지 작업은 실행되지 않는다

    def test_paused_job_is_not_auto_resumed(self, store):
        j = store.create(name="a.xlsx", input_bytes=b"x", owner_email="me@b.kr")
        store.request_pause(j["id"], "me@b.kr")
        meta = store.get(j["id"])
        meta["finished_at"] = "2020-01-01 00:00:00"
        store.save(meta)
        assert store.auto_requeue_stopped() == []

    def test_pause_running_then_resume_continues(self, store, tmp_path, monkeypatch):
        from nice_bizline.app.server.worker import run_job
        monkeypatch.setenv("MOCK_MODE", "1")
        j = store.create(name="a.xlsx", input_bytes=self._xlsx(tmp_path, ["삼성전자", "현대자동차"]),
                         owner_email="me@b.kr", input_total=2)
        store.claim_next()
        assert store.request_pause(j["id"], "me@b.kr")        # 실행 중: 플래그만
        assert store.get(j["id"])["status"] == J.RUNNING
        assert run_job(store, j, CFG, collector=MockCollector()) == J.PAUSED
        assert store.requeue(j["id"], "me@b.kr")
        assert not store.pause_requested(j["id"])
        store.claim_next()
        assert run_job(store, store.get(j["id"]), CFG, collector=MockCollector()) == J.DONE
        assert store.get(j["id"])["counts"]["success"] == 2

    def test_cancel_wins_over_pause(self, store, tmp_path, monkeypatch):
        from nice_bizline.app.server.worker import run_job
        monkeypatch.setenv("MOCK_MODE", "1")
        j = store.create(name="a.xlsx", input_bytes=self._xlsx(tmp_path, ["삼성전자"]),
                         owner_email="me@b.kr")
        store.claim_next()
        store.request_pause(j["id"], "me@b.kr")
        store.request_cancel(j["id"], "me@b.kr")
        assert run_job(store, j, CFG, collector=MockCollector()) == J.CANCELED

    def test_restart_wipes_progress_and_applies_new_options(self, store, tmp_path, monkeypatch):
        import os
        from nice_bizline.app.server.worker import run_job
        monkeypatch.setenv("MOCK_MODE", "1")
        j = store.create(name="a.xlsx", input_bytes=self._xlsx(tmp_path, ["삼성전자", "현대자동차"]),
                         owner_email="me@b.kr", options={"narrow_fields": ["주소"]})
        store.claim_next()
        assert run_job(store, j, CFG, collector=MockCollector()) == J.DONE
        ckpt = os.path.splitext(store.input_path(j["id"]))[0] + ".progress.json"
        assert os.path.exists(store.result_path(j["id"]))
        assert store.restart(j["id"], "other@b.kr") is False
        assert store.restart(j["id"], "me@b.kr",
                             options={"narrow_fields": ["사업자번호"], "match_version": 2})
        meta = store.get(j["id"])
        assert meta["status"] == J.PENDING and meta["counts"] == {}
        assert meta["options"]["narrow_fields"] == ["사업자번호"]
        assert not os.path.exists(ckpt) and not os.path.exists(store.result_path(j["id"]))
        store.claim_next()
        assert run_job(store, store.get(j["id"]), CFG, collector=MockCollector()) == J.DONE
        log = Path(store.log_path(j["id"])).read_text("utf-8")
        assert "처음부터 다시 시작" in log and "체크포인트 로드" not in log.split("처음부터 다시 시작")[1]

    def test_cannot_restart_while_running(self, store):
        j = store.create(name="a.xlsx", input_bytes=b"x", owner_email="me@b.kr")
        store.claim_next()
        assert store.restart(j["id"], "me@b.kr") is False
