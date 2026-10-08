from nice_bizline.app.core.matcher import pick, select_matches, _norm_company

WEIGHTS = {
    "weight_biz_no": 100,
    "weight_ceo": 30,
    "weight_address": 20,
    "accept_threshold": 25,
}


def test_no_candidates_returns_none():
    r = pick({"회사명": "X"}, [], WEIGHTS)
    assert r.status == "none"
    assert r.candidate is None


def test_single_candidate_unique():
    r = pick({"회사명": "X"}, [{"회사명": "X사"}], WEIGHTS)
    assert r.status == "unique"
    assert r.candidate == {"회사명": "X사"}


def test_biz_no_match_auto_confirmed():
    """사업자번호 일치 시 다른 후보가 더 많아도 즉시 확정."""
    r = pick(
        {"회사명": "X", "사업자번호": "123-45-67890"},
        [
            {"회사명": "A사", "사업자번호": "9999999999"},
            {"회사명": "B사", "사업자번호": "1234567890"},
        ],
        WEIGHTS,
    )
    assert r.status == "auto"
    assert r.candidate["회사명"] == "B사"


def test_ceo_and_address_score_passes_threshold():
    r = pick(
        {"회사명": "X", "대표자명": "홍길동", "주소": "서울 강남구 역삼동"},
        [
            {"회사명": "A사", "대표자명": "홍길동", "주소": "서울 강남구"},  # 30+20=50
            {"회사명": "B사", "대표자명": "김철수", "주소": "부산 해운대구"},  # 0
        ],
        WEIGHTS,
    )
    assert r.status == "auto"
    assert r.candidate["회사명"] == "A사"


def test_below_threshold_returns_ambiguous():
    """일치 신호가 없으면 임계치 미달로 확인필요 분류."""
    r = pick(
        {"회사명": "X"},
        [
            {"회사명": "A사", "주소": "서울"},
            {"회사명": "B사", "주소": "부산"},
        ],
        WEIGHTS,
    )
    assert r.status == "ambiguous"
    # 최상위 후보 + 나머지가 others에 채워짐
    assert r.candidate is not None
    assert len(r.others) >= 1


def test_norm_company_strips_corp_forms_but_keeps_syllables():
    assert _norm_company("삼성전자(주)") == _norm_company("삼성전자")
    assert _norm_company("주식회사 가나") == _norm_company("가나")
    # 단독 음절 '주'는 법인격 표기가 아니므로 유지
    assert _norm_company("주성엔지니어링") == "주성엔지니어링"


def test_norm_company_strips_all_corp_forms():
    """상법 5종 + 민법·특별법인 표기(정식/약자) 전부 제거."""
    pairs = [
        ("㈜가나", "가나"), ("(유)가나", "가나"), ("유한회사 가나", "가나"),
        ("유한책임회사 가나", "가나"), ("(유한)가나", "가나"),
        ("합자회사 가나", "가나"), ("(합자)가나", "가나"),
        ("합명회사 가나", "가나"), ("(합명)가나", "가나"),
        ("재단법인 가나", "가나"), ("(재)가나", "가나"),
        ("사단법인 가나", "가나"), ("(사)가나", "가나"),
        ("학교법인 가나", "가나"), ("(학)가나", "가나"),
        ("의료법인 가나", "가나"), ("(의)가나", "가나"),
        ("사회복지법인 가나", "가나"), ("(사복)가나", "가나"), ("(복)가나", "가나"),
        ("영농조합법인 가나", "가나"), ("협동조합 가나", "가나"), ("(협)가나", "가나"),
        ("종교법인 가나", "가나"), ("(종)가나", "가나"), ("(특)가나", "가나"),
    ]
    for src, expected in pairs:
        assert _norm_company(src) == expected, src


def test_select_matches_drops_non_company():
    r = select_matches({"회사명": "가나"}, [
        {"회사명": "가나(주)", "사업자번호": "111-11-11111"},
        {"회사명": "가나펀드", "사업자번호": "-"},
    ], WEIGHTS)
    assert r.dropped == 1
    assert r.status == "single"
    assert len(r.picks) == 1


def test_select_matches_multiple_same_name():
    r = select_matches({"회사명": "동명"}, [
        {"회사명": "동명(주)", "사업자번호": "111-11-11111"},
        {"회사명": "동명(주)", "사업자번호": "222-22-22222"},
    ], WEIGHTS)
    assert r.status == "multiple"
    assert len(r.picks) == 2


def test_select_matches_biz_number_confirms():
    r = select_matches({"회사명": "동명", "사업자번호": "222-22-22222"}, [
        {"회사명": "동명(주)", "사업자번호": "111-11-11111"},
        {"회사명": "동명(주)", "사업자번호": "222-22-22222"},
    ], WEIGHTS)
    assert r.status == "biz"
    assert r.picks[0]["사업자번호"] == "222-22-22222"


def test_select_matches_narrow_by_ceo():
    """입력에 대표자명이 있으면 동명 후보를 대표자로 좁힌다."""
    r = select_matches({"회사명": "세명", "대표자명": "이경환"}, [
        {"회사명": "세명(주)", "사업자번호": "111-11-11111", "대표자명": "김철수"},
        {"회사명": "세명(주)", "사업자번호": "222-22-22222", "대표자명": "이경환"},
        {"회사명": "세명(주)", "사업자번호": "333-33-33333", "대표자명": "박영수"},
    ], WEIGHTS)
    assert r.status == "single"
    assert r.picks[0]["사업자번호"] == "222-22-22222"


def test_select_matches_narrow_by_address():
    """입력에 주소가 있으면 지역(앞부분)으로 좁힌다."""
    r = select_matches({"회사명": "세명", "주소": "대구 북구"}, [
        {"회사명": "세명(주)", "사업자번호": "111-11-11111", "주소": "서울 강남구"},
        {"회사명": "세명(주)", "사업자번호": "222-22-22222", "주소": "(41513) 대구 북구 검단로"},
    ], WEIGHTS)
    assert r.status == "single"
    assert r.picks[0]["사업자번호"] == "222-22-22222"


def test_select_matches_strict_no_match_becomes_ambiguous():
    """엄격 모드: 필터 값이 아무 후보와도 안 맞으면 수집하지 않고 확인필요."""
    r = select_matches({"회사명": "세명", "대표자명": "없는사람"}, [
        {"회사명": "세명(주)", "사업자번호": "111-11-11111", "대표자명": "김철수"},
        {"회사명": "세명(주)", "사업자번호": "222-22-22222", "대표자명": "이경환"},
    ], WEIGHTS)
    assert r.status == "ambiguous"
    assert r.picks == []


def test_region_match_city_level():
    from nice_bizline.app.core.matcher import region_match
    # 시/군까지만 비교 (도로명 이하 무시)
    assert region_match("경기도 안양시 동안구 엘에스로 122", "경기 안양시 동안구")
    assert region_match("경상북도 경주시 유림로 53-12", "경북 경주시")
    assert region_match("(46273) 부산광역시 연제구 중앙대로 1001", "부산 연제구")
    assert region_match("서울특별시 관악구 양녕로1길 32", "서울 강남구")   # 광역시는 시/도 일치
    assert not region_match("경기도 안양시", "경기 수원시")               # 다른 시
    assert not region_match("경상남도 김해시", "경북 경주시")


def test_select_matches_excludes_personal_and_closed_type():
    """기업유형이 '개인'/'폐업'인 후보는 수집하지 않는다."""
    r = select_matches({"회사명": "가나"}, [
        {"회사명": "가나(주)", "사업자번호": "111-11-11111", "기업유형": "일반"},
        {"회사명": "가나(주)", "사업자번호": "222-22-22222", "기업유형": "개인"},
        {"회사명": "가나(주)", "사업자번호": "333-33-33333", "기업유형": "폐업"},
    ], WEIGHTS)
    assert r.status == "single"
    assert r.picks[0]["사업자번호"] == "111-11-11111"
    assert r.dropped == 2


def test_select_matches_none_when_all_noise():
    r = select_matches({"회사명": "X"},
                       [{"회사명": "X펀드", "사업자번호": "-"}], WEIGHTS)
    assert r.status == "none"


def test_ambiguous_when_top_ties_second():
    """동점이면 임계치를 넘어도 ambiguous로 분류 (안전판)."""
    r = pick(
        {"회사명": "X", "대표자명": "홍길동"},
        [
            {"회사명": "A사", "대표자명": "홍길동"},
            {"회사명": "B사", "대표자명": "홍길동"},
        ],
        WEIGHTS,
    )
    assert r.status == "ambiguous"
