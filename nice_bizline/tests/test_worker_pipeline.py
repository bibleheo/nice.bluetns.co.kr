"""워커 파이프라인 통합 테스트: MockCollector 기반 엔드투엔드."""
import yaml

from nice_bizline.app.core.collector import MockCollector, LoginRequired
from nice_bizline.app.core.worker import RunOptions, Worker, WorkerCallbacks


def _load_cfg():
    import os
    cfg_path = os.path.join(os.path.dirname(__file__), "..", "config.yaml")
    with open(cfg_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _run(companies, collector=None):
    cfg = _load_cfg()
    done = {}
    logs = []
    opts = RunOptions(user_id="u", password="p", companies=companies)
    cb = WorkerCallbacks(
        on_log=lambda lvl, msg: logs.append((lvl, msg)),
        on_progress=lambda c, t, n: None,
        on_done=done.update,
    )
    w = Worker(collector or MockCollector(cfg), cfg, opts, cb)
    w.start()
    w.join(timeout=10)
    assert not w.is_alive(), "워커가 시간 내 종료되지 않음"
    return w, done, logs


def test_success_path():
    w, summary, _ = _run([{"회사명": "삼성전자"}, {"회사명": "현대자동차"}])
    assert summary["total"] == 2
    assert summary["success"] == 2
    assert summary["not_found"] == 0


def test_not_found():
    w, summary, _ = _run([{"회사명": "존재하지않는회사XYZ"}])
    assert summary["not_found"] == 1
    assert any(r["조회상태"] == "미발견" for r in w.state.records)
    assert len(w.state.unfound) == 1


def test_mixed_results():
    w, summary, _ = _run([
        {"회사명": "삼성전자"},
        {"회사명": "없는회사1"},
        {"회사명": "현대자동차"},
        {"회사명": "없는회사2"},
    ])
    assert summary["success"] == 2
    assert summary["not_found"] == 2
    assert summary["error"] == 0


def test_login_failure_records_errors():
    """로그인이 실패하면 모든 건이 오류 처리되지 않고 finalize까지 진행."""
    cfg = _load_cfg()

    class BrokenCollector(MockCollector):
        def login(self, user_id, password):
            from nice_bizline.app.core.collector import CollectorError
            raise CollectorError("자격증명 거부")

    w, summary, logs = _run([{"회사명": "삼성전자"}], collector=BrokenCollector(cfg))
    assert summary["total"] == 1
    assert any("로그인 실패" in m for _, m in logs)


def test_login_required_triggers_relogin_retry():
    """첫 호출에서 LoginRequired → 재로그인 후 1회 재시도 후 성공."""
    cfg = _load_cfg()

    class FlakyCollector(MockCollector):
        def __init__(self, cfg):
            super().__init__(cfg)
            self._search_count = 0

        def search(self, name):
            self._search_count += 1
            if self._search_count == 1:
                raise LoginRequired()
            return super().search(name)

    w, summary, logs = _run([{"회사명": "삼성전자"}], collector=FlakyCollector(cfg))
    assert summary["success"] == 1
    assert any("세션 만료/강제 로그아웃 감지" in m for _, m in logs)


class ListCollector:
    """검색 결과를 지정해 넣는 테스트용 수집기 (노이즈 필터/동명 처리 검증)."""

    def __init__(self, results: dict):
        self._results = results  # 이름 조각 -> 후보 dict 리스트
        self._logged_in = False

    def login(self, user_id, password):
        self._logged_in = True

    def search(self, name):
        for key, rows in self._results.items():
            if key in name or name in key:
                return [dict(r) for r in rows]
        return []

    def fetch_detail(self, candidate, finance_years=1):
        return {
            "회사명": candidate.get("회사명"),
            "사업자번호": candidate.get("사업자번호"),
            "대표자": candidate.get("대표자명"),
            "주소": candidate.get("주소"),
            "매출액": "100", "영업이익": "10", "당기순이익": "5", "신용등급": "A",
        }

    def is_login_page(self):
        return not self._logged_in

    def close(self):
        pass


def test_noise_filter_excludes_non_company():
    """사업자번호 없는 펀드/ETF 행은 후보에서 제외되고 로그로 안내한다."""
    col = ListCollector({"가나건설": [
        {"회사명": "가나건설(주)", "사업자번호": "111-11-11111", "대표자명": "김가나"},
        {"회사명": "가나건설레버리지ETF", "사업자번호": "-"},   # 펀드 노이즈
    ]})
    w, summary, logs = _run([{"회사명": "가나건설"}], collector=col)
    assert summary["success"] == 1
    assert len(w.state.records) == 1
    assert any("제외" in m for lvl, m in logs)


def test_returns_all_duplicates_when_no_biz():
    """사업자번호 없이 동명 회사가 여러 개면 전부 수집한다."""
    col = ListCollector({"동명건설": [
        {"회사명": "동명건설(주)", "사업자번호": "111-11-11111", "대표자명": "김철수", "주소": "서울"},
        {"회사명": "동명건설(주)", "사업자번호": "222-22-22222", "대표자명": "이영희", "주소": "부산"},
        {"회사명": "동명건설우량채펀드", "사업자번호": "-"},   # 노이즈
    ]})
    w, summary, logs = _run([{"회사명": "동명건설"}], collector=col)
    assert summary["success"] == 2                    # 동명 2건 전부 수집
    assert len(w.state.records) == 2
    bizes = {r.get("사업자번호") for r in w.state.records}
    assert bizes == {"111-11-11111", "222-22-22222"}
    assert any("동명 회사 2건" in m for lvl, m in logs)


def test_biz_number_picks_single_among_duplicates():
    """사업자번호를 주면 동명 중 해당 1건만 확정한다."""
    col = ListCollector({"동명건설": [
        {"회사명": "동명건설(주)", "사업자번호": "111-11-11111", "대표자명": "김철수"},
        {"회사명": "동명건설(주)", "사업자번호": "222-22-22222", "대표자명": "이영희"},
    ]})
    w, summary, _ = _run(
        [{"회사명": "동명건설", "사업자번호": "222-22-22222"}], collector=col)
    assert summary["success"] == 1
    assert w.state.records[0]["사업자번호"] == "222-22-22222"


def test_no_exact_name_match_is_ambiguous():
    """상호가 정확히 일치하는 후보가 없으면 확인필요로 분류한다."""
    col = ListCollector({"우리": [
        {"회사명": "우리은행(주)", "사업자번호": "111-11-11111"},
        {"회사명": "우리카드(주)", "사업자번호": "222-22-22222"},
    ]})
    w, summary, _ = _run([{"회사명": "우리"}], collector=col)
    assert summary["ambiguous"] == 1
    assert summary["success"] == 0
    assert len(w.state.ambiguous) == 1


def test_result_filter_and_mode():
    """결과 필터: AND 모드에서 종업원수·매출 조건을 모두 충족해야 수집."""
    cfg = _load_cfg()
    done = {}
    # 삼성전자: 종업원 125819, 매출 258900000백만 → 통과
    # 현대자동차: 종업원 75091, 매출 162663700 → 통과
    opts = RunOptions(user_id="u", password="p",
                      companies=[{"회사명": "삼성전자"}, {"회사명": "현대자동차"}],
                      result_filter={"min_employees": 100000, "min_sales": 1000,
                                     "mode": "AND"})
    cb = WorkerCallbacks(on_log=lambda *a: None, on_progress=lambda *a: None,
                         on_done=done.update)
    w = Worker(MockCollector(cfg), cfg, opts, cb)
    w.start(); w.join(timeout=10)
    # 현대차는 종업원 75091 < 100000 → AND 미충족 제외
    assert done["success"] == 1
    assert done.get("필터제외") == 1
    assert len(w.state.records) == 1


def test_result_filter_or_mode():
    """OR 모드: 하나만 충족해도 수집."""
    cfg = _load_cfg()
    done = {}
    opts = RunOptions(user_id="u", password="p",
                      companies=[{"회사명": "현대자동차"}],
                      result_filter={"min_employees": 100000, "min_sales": 1000,
                                     "mode": "OR"})
    cb = WorkerCallbacks(on_log=lambda *a: None, on_progress=lambda *a: None,
                         on_done=done.update)
    w = Worker(MockCollector(cfg), cfg, opts, cb)
    w.start(); w.join(timeout=10)
    assert done["success"] == 1   # 매출 조건 충족 → OR 통과


def test_connection_error_relogin_and_retry():
    """검색 중 일반 예외(인터넷 단절 등) 발생 시 재로그인 후 같은 회사 재시도."""
    cfg = _load_cfg()

    class FlakyNetCollector(MockCollector):
        def __init__(self, cfg):
            super().__init__(cfg)
            self.search_calls = 0
            self.login_calls = 0

        def login(self, user_id, password):
            self.login_calls += 1
            super().login(user_id, password)

        def search(self, name):
            self.search_calls += 1
            if self.search_calls == 1:
                raise RuntimeError("net::ERR_INTERNET_DISCONNECTED")
            return super().search(name)

    col = FlakyNetCollector(cfg)
    w, summary, logs = _run([{"회사명": "삼성전자"}], collector=col)
    assert summary["success"] == 1          # 재시도로 결국 성공
    assert col.login_calls >= 2             # 최초 로그인 + 복구 재로그인
    assert any("재로그인 후 재시도" in m for _, m in logs)


def test_consecutive_errors_safe_stop_and_resumable():
    """연속 오류 시 안전 정지하고, 해당 회사들은 재개 대상으로 남긴다."""
    cfg = _load_cfg()

    class DeadCollector(MockCollector):
        def search(self, name):
            raise RuntimeError("연결 끊김")

    companies = [{"회사명": f"회사{i}"} for i in range(10)]
    w, summary, logs = _run(companies, collector=DeadCollector(cfg))
    assert summary["stopped"] is True
    assert any("안전 정지" in m for _, m in logs)
    # 연속 오류로 정지된 회사들은 processed_keys 에서 제거되어 재개 시 재시도
    assert len(w.state.processed_keys) < len(companies)


def test_duplicate_input_logs_and_skips():
    """같은 실행 안에서 동일 회사명|사업자번호가 반복되면 경고 로그 후 1회만 처리."""
    w, summary, logs = _run([
        {"회사명": "삼성전자"},
        {"회사명": "삼성전자"},
        {"회사명": "현대자동차"},
    ])
    assert summary["total"] == 3
    # 삼성전자(1회) + 현대자동차(1회) = 2건만 수집, 중복 삼성전자는 스킵
    assert summary["success"] == 2
    assert len(w.state.records) == 2  # 중복 삼성전자는 record에 추가되지 않음
    assert any("중복 입력" in m for lvl, m in logs if lvl == "warn")


def test_stop_safely_finalizes():
    """stop() 호출 후에도 finalize가 호출되어 결과를 반환."""
    cfg = _load_cfg()
    done = {}
    opts = RunOptions(user_id="u", password="p", companies=[{"회사명": "삼성전자"}] * 20)
    cb = WorkerCallbacks(
        on_log=lambda *_: None,
        on_progress=lambda *_: None,
        on_done=done.update,
    )
    w = Worker(MockCollector(cfg), cfg, opts, cb)
    w.start()
    w.stop()  # 즉시 중단
    w.join(timeout=5)
    assert not w.is_alive()
    assert "total" in done
