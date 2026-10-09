"""디자인 시안 v1 반영분 테스트: 입력 건수·처리 건수·정지 이유 저장, 열 인식, 화면 계산 함수."""
import sys
from pathlib import Path

import openpyxl
import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from nice_bizline.app.core.collector import MockCollector  # noqa: E402
from nice_bizline.app.excelio.reader import inspect_columns  # noqa: E402
from nice_bizline.app.server import jobs as J  # noqa: E402
from nice_bizline.app.server.worker import run_job, stop_reason_text  # noqa: E402

CFG = Path(__file__).resolve().parents[1] / "config.yaml"


def _cfg():
    return yaml.safe_load(CFG.read_text(encoding="utf-8"))


def _xlsx(path, rows):
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    wb.save(path)
    return path


def _xlsx_bytes(tmp_path, rows):
    return Path(_xlsx(tmp_path / "in.xlsx", rows)).read_bytes()


@pytest.fixture
def store(tmp_path):
    return J.JobStore(str(tmp_path / "data"))


class TestJobStoreFields:
    def test_create_records_input_total(self, store):
        j = store.create(name="a.xlsx", input_bytes=b"x", owner_email="me@b.kr", input_total=16000)
        assert store.get(j["id"])["input_total"] == 16000

    def test_set_input_total_only_fills_missing(self, store):
        j = store.create(name="a.xlsx", input_bytes=b"x", owner_email="me@b.kr")
        store.set_input_total(j["id"], 120)
        assert store.get(j["id"])["input_total"] == 120
        store.set_input_total(j["id"], 999)                 # 이미 있으면 덮지 않음
        assert store.get(j["id"])["input_total"] == 120

    def test_stop_reason_saved_and_cleared_on_requeue(self, store):
        j = store.create(name="a.xlsx", input_bytes=b"x", owner_email="me@b.kr")
        store.claim_next()
        store.finish(j["id"], J.STOPPED, stop_reason="사이트 연결 오류가 5번 이어져")
        assert store.get(j["id"])["stop_reason"] == "사이트 연결 오류가 5번 이어져"
        assert store.requeue(j["id"], "me@b.kr")
        assert "stop_reason" not in store.get(j["id"])


class TestWorkerCounts:
    def test_done_job_has_processed_and_input_total(self, store, tmp_path, monkeypatch):
        monkeypatch.setenv("MOCK_MODE", "1")
        data = _xlsx_bytes(tmp_path, [["회사명"], ["삼성전자"], ["현대자동차"], ["없는회사ZZZ"]])
        j = store.create(name="list.xlsx", input_bytes=data, owner_email="me@b.kr")   # 예전 방식(건수 없음)
        store.claim_next()
        assert run_job(store, j, _cfg(), collector=MockCollector(_cfg())) == J.DONE
        meta = store.get(j["id"])
        assert meta["input_total"] == 3                    # worker 가 보정
        assert meta["counts"]["processed"] == 3
        assert meta["counts"]["total"] == 3                # total 은 입력 건수

    def test_canceled_job_keeps_stop_point(self, store, tmp_path, monkeypatch):
        """멈춘 작업은 진행값을 total/total 로 덮지 않는다 ('0 / 2 · 처리분 저장됨')."""
        monkeypatch.setenv("MOCK_MODE", "1")
        data = _xlsx_bytes(tmp_path, [["회사명"], ["삼성전자"], ["현대자동차"]])
        j = store.create(name="list.xlsx", input_bytes=data, owner_email="me@b.kr", input_total=2)
        store.claim_next()
        store.request_cancel(j["id"], "me@b.kr")
        assert run_job(store, j, _cfg(), collector=MockCollector(_cfg())) == J.CANCELED
        meta = store.get(j["id"])
        assert meta["progress"]["current"] == 0
        assert meta["progress"]["total"] == 2
        assert meta["counts"]["processed"] == 0


class TestStopReason:
    def test_consecutive_errors(self):
        msg = "연속 5건 오류 - 연결 문제로 보입니다. 안전 정지합니다."
        assert stop_reason_text(msg) == "사이트 연결 오류가 5번 이어져"

    def test_relogin_failure(self):
        assert stop_reason_text("재로그인이 계속 실패해 안전 정지합니다.") == "다시 로그인이 계속 실패해"

    def test_unknown(self):
        assert stop_reason_text("") == ""


class TestInspectColumns:
    def test_alias_and_unused_columns(self, tmp_path):
        p = _xlsx(tmp_path / "c.xlsx", [["고객사", "전화번호", "주소", "비고"], ["A", "02", "서울", "x"]])
        cols = inspect_columns(str(p))
        assert [c["original"] for c in cols] == ["고객사", "전화번호", "주소", "비고"]
        assert cols[0]["field"] == "회사명" and cols[0]["renamed"] and not cols[0]["guessed"]
        assert cols[2]["field"] == "주소" and not cols[2]["renamed"]
        assert cols[3]["field"] is None

    def test_first_column_guessed_as_company(self, tmp_path):
        p = _xlsx(tmp_path / "g.xlsx", [["거래처목록", "주소"], ["A", "서울"]])
        cols = inspect_columns(str(p))
        assert cols[0]["field"] == "회사명" and cols[0]["guessed"]


class TestScreenHelpers:
    @pytest.fixture(autouse=True)
    def _ui(self):
        pytest.importorskip("streamlit")
        from nice_bizline.app.web import server_ui
        self.ui = server_ui

    def test_human_durations(self):
        h = self.ui._human
        assert h(30) == "약 1분"
        assert h(35 * 60) == "약 35분"
        assert h(3 * 3600) == "약 3시간"
        assert h(14370 * 25) == "약 4일"

    def test_counts_for_stopped_and_old_jobs(self):
        new = {"status": J.STOPPED, "input_total": 16000,
               "counts": {"total": 16000, "processed": 1630}, "progress": {"current": 1630, "total": 16000}}
        assert self.ui._total(new) == 16000 and self.ui._done(new) == 1630
        old_pending = {"status": J.PENDING, "progress": {"current": 0, "total": 0}, "counts": {}}
        assert self.ui._total(old_pending) == 0
        old_done = {"status": J.DONE, "counts": {"total": 420}, "progress": {"current": 420, "total": 420}}
        assert self.ui._total(old_done) == 420 and self.ui._done(old_done) == 420

    def test_queue_order_matches_claim_next(self):
        jobs = [
            {"id": "1", "status": J.RUNNING, "input_total": 120, "progress": {"current": 20}, "counts": {}},
            {"id": "2", "status": J.PENDING, "input_total": 10, "progress": {}, "counts": {}},
            {"id": "3", "status": J.PENDING, "input_total": 10, "progress": {}, "counts": {}},
        ]
        assert [j["id"] for j in self.ui._ahead(jobs, jobs[2])] == ["1", "2"]
        assert self.ui._wait_before(jobs, jobs[2]) == (100 + 10) * self.ui.SEC_PER_COMPANY

    def test_humanize_log(self):
        raw = "\n".join([
            "01:33:08  [info] 작업 시작 - 16000건, 소유자 me",
            "01:33:10  [info] 로그인 성공",
            "08:41:55  [info] 세션 연장 (+10분)",
            "08:51:55  [info] 세션 연장 (+10분)",
            "08:52:00  [info] [삼성전자] 수집 완료",
            "08:52:33  [info] 체크포인트 저장 (1630건)",
            "08:54:10  [error] 연속 5건 오류 - 연결 문제로 보입니다. 안전 정지합니다.",
        ])
        out = self.ui._humanize_log(raw)
        assert "NICE BizLINE 로그인" in out
        assert out.count("접속 시간 연장") == 1            # 연속 반복은 하나로
        assert "1,630건까지 저장" in out
        assert "연결 오류 5번 연속 → 안전 정지" in out
        assert "수집 완료" not in out
        assert "`" not in out                              # 코드 글씨 대신 회색 일반 글씨
        assert ":gray[" in out

    def test_humanize_log_dates_across_midnight(self):
        import datetime as dt
        raw = "\n".join([
            "23:50:00  [info] 로그인 성공",
            "23:59:00  [info] 체크포인트 저장 (10건)",
            "00:09:00  [info] 체크포인트 저장 (20건)",
            "00:19:00  [info] 체크포인트 저장 (30건)",
        ])
        out = self.ui._humanize_log(raw, start=dt.datetime(2026, 10, 9, 23, 49)).splitlines()
        assert out[0].startswith("- :gray[10/09 23:50]")   # 첫 줄은 날짜 포함
        assert out[1].startswith("- :gray[23:59]")          # 같은 날은 시각만
        assert out[2].startswith("- :gray[10/10 00:09]")    # 날짜가 바뀌면 날짜 포함
        assert out[3].startswith("- :gray[00:19]")

    def test_particle(self):
        assert self.ui._with_ro("주소") == "주소로"
        assert self.ui._with_ro("대표자명") == "대표자명으로"

    def test_no_double_tilde_in_markdown_copy(self):
        """'~' 두 개가 한 줄에 있으면 Streamlit 이 취소선으로 그린다 (기존 표시 오류의 원인)."""
        src = Path(self.ui.__file__).read_text(encoding="utf-8")
        for line in src.splitlines():
            if line.lstrip().startswith("#"):
                continue
            assert line.count("~") < 2, line


class TestPortalHeader:
    """포털 공통 '포털로 이동' 버튼 규격."""

    @pytest.fixture(autouse=True)
    def _ui(self):
        pytest.importorskip("streamlit")
        from nice_bizline.app.web import server_ui
        self.ui = server_ui

    def test_button_links_to_portal_same_tab(self):
        html = self.ui.header_html()
        assert f'href="{self.ui.PORTAL_URL}"' in html
        assert "_blank" not in html                          # 새 탭 금지
        assert 'aria-label="포털로 이동"' in html            # 심볼만 보일 때도 이름 유지
        assert ">포털로 이동</span>" in html                  # 문구 고정
        # 버튼이 앱 이름보다 앞(왼쪽)
        assert html.index("nice-portal-btn") < html.index("nice-app-name")

    def test_symbol_bundled_without_metadata(self):
        import base64
        assert self.ui.PORTAL_SYMBOL_PATH.exists()            # 앱 안의 파일을 씀
        img = self.ui._portal_symbol_img()
        assert img.startswith('<img src="data:image/svg+xml;base64,')
        svg = base64.b64decode(img.split("base64,")[1].split('"')[0]).decode()
        assert "<metadata>" not in svg and "c2pa" not in svg
        assert "portal.bluetns.co.kr" not in img              # 포털 주소에서 불러오지 않음

    def test_spec_sizes_in_css(self):
        css = self.ui._HEADER_CSS
        assert "height:36px" in css and "height:44px" in css
        assert "border-radius:10px" in css and "1px solid #e2e5ea" in css
        assert "width:22px;height:22px" in css
