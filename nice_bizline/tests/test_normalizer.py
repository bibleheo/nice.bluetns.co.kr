from nice_bizline.app.core.normalizer import (
    amount_to_millions,
    normalize_amount,
    normalize_biz_number,
    normalize_date,
    normalize_record,
)


class TestAmountToMillions:
    def test_eok_won(self):
        assert amount_to_millions("20.4", "억원") == 2040
        assert amount_to_millions("11.4", "억원") == 1140

    def test_man_won(self):
        assert amount_to_millions("1,558.3", "만원") == 16

    def test_dash_and_none(self):
        assert amount_to_millions("-", "억원") is None
        assert amount_to_millions(None, "억원") is None
        assert amount_to_millions("", "만원") is None

    def test_negative(self):
        assert amount_to_millions("-2,239.2", "만원") == -22

    def test_other_units(self):
        assert amount_to_millions("5", "조원") == 5_000_000
        assert amount_to_millions("300", "백만원") == 300
        assert amount_to_millions("1000000", "원") == 1

    def test_unknown_unit_rounds_value(self):
        assert amount_to_millions("123.6", "") == 124


class TestNormalizeAmount:
    def test_none_and_empty(self):
        assert normalize_amount(None) is None
        assert normalize_amount("") is None

    def test_int_passthrough(self):
        assert normalize_amount(1234) == 1234
        assert normalize_amount(0) == 0

    def test_float_truncates(self):
        assert normalize_amount(1234.78) == 1234

    def test_strips_commas_and_unit(self):
        assert normalize_amount("1,234,567") == 1234567
        assert normalize_amount("1,234백만원") == 1234
        assert normalize_amount("258,900,000원") == 258900000

    def test_unparseable(self):
        assert normalize_amount("미공개") is None
        assert normalize_amount("-") is None


class TestNormalizeDate:
    def test_none(self):
        assert normalize_date(None) is None
        assert normalize_date("") is None

    def test_iso(self):
        assert normalize_date("2026-05-28") == "2026-05-28"

    def test_dotted(self):
        assert normalize_date("2026.05.28") == "2026-05-28"

    def test_slashed(self):
        assert normalize_date("2026/05/28") == "2026-05-28"

    def test_korean(self):
        assert normalize_date("2026년 05월 28일") == "2026-05-28"

    def test_compact(self):
        assert normalize_date("20260528") == "2026-05-28"

    def test_unknown_format_preserved(self):
        assert normalize_date("28 May 2026") == "28 May 2026"


class TestNormalizeBizNumber:
    def test_none(self):
        assert normalize_biz_number(None) is None
        assert normalize_biz_number("") is None

    def test_digits_only(self):
        assert normalize_biz_number("1248100998") == "124-81-00998"

    def test_already_formatted(self):
        assert normalize_biz_number("124-81-00998") == "124-81-00998"

    def test_other_separators(self):
        assert normalize_biz_number("124 81 00998") == "124-81-00998"

    def test_invalid_length_preserved(self):
        assert normalize_biz_number("12345") == "12345"


class TestNormalizeRecord:
    def test_full_record(self):
        raw = {
            "회사명": "테스트(주)",
            "사업자번호": "1234567890",
            "설립일": "2020.01.15",
            "매출액": "1,234",
            "영업이익": "100",
            "당기순이익": "50",
            "종업원수": "1,234명",
        }
        out = normalize_record(raw)
        assert out["사업자번호"] == "123-45-67890"
        assert out["설립일"] == "2020-01-15"
        assert out["매출액"] == 1234
        assert out["종업원수"] == 1234
        assert out["회사명"] == "테스트(주)"  # unchanged
