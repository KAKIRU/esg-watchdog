"""llm/quotes.py — 인용 검사(normalize · quote_in · find_span)를 원문 없이 검사한다 (D-04 · D-05)."""

from esg_watchdog.llm.quotes import find_span, normalize, quote_in


def test_normalize_collapses_whitespace_and_unifies_quotes_and_middle_dots():
    assert normalize("  “탄소중립”\n\t2050 ") == '"탄소중립" 2050'
    assert normalize("온실가스ㆍ에너지 • 폐기물・자원") == "온실가스·에너지 · 폐기물·자원"
    assert normalize("‘안전’") == "'안전'"
    assert normalize("") == ""
    assert normalize(" \n ") == ""


def test_normalize_drops_zero_width_and_treats_nbsp_as_space():
    assert normalize("A\u200bB\u00a0C\ufeff") == "AB C"


def test_quote_in_ignores_whitespace_and_quote_style():
    source = "당사는 2030년까지\n온실가스 배출량을 “2020년 대비” 20%   감축한다."
    assert quote_in('2030년까지 온실가스 배출량을 "2020년 대비" 20% 감축한다', source)
    assert quote_in("온실가스 배출량을", source)
    assert not quote_in("2035년까지", source)
    assert not quote_in("", source)
    assert not quote_in("   ", source)


def test_find_span_returns_offsets_in_original_text():
    source = "머리말.\n\n당사는  “탄소중립”을 선언한다."
    span = find_span('당사는 "탄소중립"을', source)
    assert span is not None
    start, end = span
    assert source[start:end] == "당사는  “탄소중립”을"
    assert find_span("없는 문장", source) is None
    assert find_span("", source) is None


def test_find_span_covers_dropped_and_wide_spaces():
    source = "A\u200bB\u00a0C"
    assert find_span("AB C", source) == (0, 5)
    assert find_span("B C", source) == (2, 5)


def test_find_span_first_occurrence_and_full_match():
    source = "안전 점검. 안전 점검을 강화한다."
    assert find_span("안전 점검", source) == (0, 5)
    assert find_span("안전 점검을 강화한다.", source) == (7, 19)
