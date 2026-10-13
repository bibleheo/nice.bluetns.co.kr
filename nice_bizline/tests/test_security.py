"""보안 점검 대응 테스트: 계정 값 가리기, 로그인 입력 실패 문구, 파일명 마크다운, 이용 기록 경고."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from nice_bizline.app.core import collector as C  # noqa: E402
from nice_bizline.app.core.redact import MASK, redact  # noqa: E402
from nice_bizline.app.server import audit  # noqa: E402
from nice_bizline.app.server import jobs as J  # noqa: E402

SECRET_PW = "S3cret!Pw#2026"
SECRET_ID = "nice-user-01"


class TestRedact:
    def test_masks_env_values(self, monkeypatch):
        monkeypatch.setenv("NICE_PW", SECRET_PW)
        monkeypatch.setenv("NICE_ID", SECRET_ID)
        out = redact(f'Timeout - fill("{SECRET_ID}") - fill("{SECRET_PW}")')
        assert SECRET_PW not in out and SECRET_ID not in out
        assert out.count(MASK) == 2

    def test_extra_values_and_non_strings(self, monkeypatch):
        monkeypatch.delenv("NICE_PW", raising=False)
        monkeypatch.delenv("NICE_ID", raising=False)
        assert redact(ValueError("x abc123 y"), ["abc123"]) == f"x {MASK} y"
        assert redact(None) == ""

    def test_short_and_mock_values_not_masked(self, monkeypatch):
        monkeypatch.setenv("NICE_PW", "mock")
        monkeypatch.setenv("NICE_ID", "ab")
        assert redact("mock ab") == "mock ab"


class _FailingPage:
    """fill 이 Playwright 처럼 입력값을 담은 오류를 낸다."""

    def goto(self, url):
        pass

    def wait_for_selector(self, selector, timeout=0, state=None):
        if selector == "#marker":
            raise TimeoutError("not logged in")

    def fill(self, selector, value):
        raise TimeoutError(f'Page.fill: Timeout\nCall log:\n  - fill("{value}")')


class TestLoginErrorHidesValues:
    def test_fill_failure_message_has_no_credentials(self):
        col = C.NiceBizlineCollector.__new__(C.NiceBizlineCollector)
        col._cfg = {"site": {"login_url": "about:blank"},
                    "selectors": {"login": {"logged_in_marker": "#marker", "id_input": "#id",
                                            "pw_input": "#pw", "submit_btn": "#go"}}}
        col._page = _FailingPage()
        with pytest.raises(C.CollectorError) as ei:
            col.login(SECRET_ID, SECRET_PW)
        err = ei.value
        assert SECRET_PW not in str(err) and SECRET_ID not in str(err)
        assert err.__cause__ is None and err.__suppress_context__   # 원래 예외(값 포함)를 잇지 않음


class TestWorkerLogsRedacted:
    def test_unexpected_error_is_masked_in_log_and_job(self, tmp_path, monkeypatch):
        import openpyxl
        import yaml
        from nice_bizline.app.server.worker import run_job

        monkeypatch.setenv("NICE_ID", SECRET_ID)
        monkeypatch.setenv("NICE_PW", SECRET_PW)
        monkeypatch.delenv("MOCK_MODE", raising=False)
        wb = openpyxl.Workbook()
        wb.active.append(["회사명"])
        wb.active.append(["삼성전자"])
        p = tmp_path / "in.xlsx"
        wb.save(p)
        store = J.JobStore(str(tmp_path / "data"))
        job = store.create(name="a.xlsx", input_bytes=p.read_bytes(), owner_email="me@bluetns.co.kr")
        store.claim_next()

        class Boom:
            def login(self, uid, pw):
                raise RuntimeError(f'Page.fill: Timeout - fill("{pw}") id={uid}')

            def close(self):
                pass

        cfg = yaml.safe_load((Path(__file__).resolve().parents[1] / "config.yaml").read_text("utf-8"))
        run_job(store, job, cfg, collector=Boom())
        log = Path(store.log_path(job["id"])).read_text("utf-8")
        meta = store.get(job["id"])
        for text in (log, meta.get("error", "")):
            assert SECRET_PW not in text and SECRET_ID not in text


class TestMarkdownEscape:
    @pytest.fixture(autouse=True)
    def _ui(self):
        pytest.importorskip("streamlit")
        from nice_bizline.app.web import server_ui
        self.md = server_ui._md

    def test_link_syntax_and_autolinks_are_broken(self):
        out = self.md("[급여 확인](http://evil.example) www.evil.example a@b.kr")
        assert "](" not in out.replace("\\]\\(", "")
        assert "http:" not in out and "www." not in out
        assert "​@" in out

    def test_newlines_collapsed(self):
        assert "\n" not in self.md("a\n\n# 제목")


class TestAudit:
    def test_flags_foreign_domain_other_downloader_and_burst(self, tmp_path):
        store = J.JobStore(str(tmp_path))
        j = store.create(name="a.xlsx", input_bytes=b"x", owner_email="kim@bluetns.co.kr", input_total=5)
        meta = store.get(j["id"])
        meta["downloads"] = ([{"email": "kim@bluetns.co.kr", "at": "2026-10-13 09:00:00"}]
                             + [{"email": "evil@gmail.com", "at": f"2026-10-13 10:{m:02d}:00"}
                                for m in range(12)])
        store.save(meta)
        rows, warnings = audit.summarize(store, "bluetns.co.kr")
        text = "\n".join(warnings)
        assert "회사 도메인이 아닌 이메일: evil@gmail.com" in text
        assert "올린 사람이 아닌 다운로드" in text
        assert "짧은 시간 대량 다운로드: evil@gmail.com" in text
        assert {r["email"] for r in rows} == {"kim@bluetns.co.kr", "evil@gmail.com"}

    def test_clean_store_has_no_warnings(self, tmp_path):
        store = J.JobStore(str(tmp_path))
        store.create(name="a.xlsx", input_bytes=b"x", owner_email="kim@bluetns.co.kr")
        _, warnings = audit.summarize(store, "bluetns.co.kr")
        assert warnings == []
