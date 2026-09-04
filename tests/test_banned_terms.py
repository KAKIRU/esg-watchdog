"""knowledge/banned_terms.py — scan(allow 마스킹 → pattern/substring) · check_fields(applies_to 만) · never_ban 을 검사한다 (D-17)."""

import pytest

from esg_watchdog.knowledge.banned_terms import (
    check_fields,
    load_banned_terms,
    regeneration_hint,
    scan,
)

# allow 표현 · 어미 · 앞글자 덕에 걸리지 않아야 하는 문장
CLEAN = [
    "수사기관", "조사기간", "감사기준", "사기업", "판매수익", "구매수량", "판매도 증가", "도매수요", "기관 매수세", "최고점",
    "공장 바닥", "사업 포트폴리오", "재생에너지 100%", "고용 보장", "불법 펨토셀", "범죄예방", "영업이익률", "총수익률", "불확실성",
    "안전을 보장한다고 밝혔으나", "공약 위반으로 판단됩니다",
]
# 적발해야 하는 문장 → 기대 term
BANNED = [
    ("지금 매수하시기 바랍니다", "매수"),
    ("목표주가 5만원", "목표주가"),
    ("저평가 구간", "저평가"),
    ("투자의견 매수", "투자의견"),
    ("포트폴리오 조정이 필요", "포트폴리오"),
    ("100% 확실합니다", "100%"),
    ("수익을 보장합니다", "보장"),
    ("명백한 위법입니다", "명백히"),
    ("사기를 저질렀습니다", "사기"),
    ("유망한 종목", "유망"),
]


@pytest.mark.parametrize("text", CLEAN)
def test_allowed_expressions_do_not_hit(text: str):
    assert scan(text) == [], text


@pytest.mark.parametrize(("text", "term"), BANNED)
def test_banned_expressions_hit(text: str, term: str):
    assert term in scan(text), text


def test_combined_hits_are_listed_once_per_term_in_yaml_order():
    assert scan("투자의견 매수, 지금 매수하세요") == ["매수", "투자의견"]
    assert scan("100% 확실합니다") == ["확실히", "100%"]  # yaml 순서


def test_never_ban_words_never_hit():
    never = load_banned_terms()["never_ban"]
    assert len(never) == 8
    for word in never:
        assert scan(word) == [], word
    assert scan(" ".join(never)) == []
    assert scan("경보 등급은 리스크와 중대성에 따라 위반·후퇴·이행지연·이행긍정으로 나뉘며 불확실합니다") == []


def test_case_insensitive_and_empty():
    assert scan("") == [] and scan("   ") == []
    # 영문 term 은 없지만 정책 case_sensitive false — 한글은 영향 없음. 마스킹은 소문자화 뒤 한다
    assert scan("SPC삼립 사업 포트폴리오") == []


def test_allow_masking_happens_before_matching():
    # allow 를 먼저 마스킹하지 않으면 "매수세" 안의 "매수" 가 걸린다 — 순서 고정
    assert scan("기관 매수세가 유입됐다") == []
    # allow 표현 밖에 같은 term 이 또 있으면 그것은 걸린다
    assert scan("기관 매수세, 개인도 매수") == ["매수"]
    assert scan("총수익률과 수익률 전망") == ["수익률"]


def test_yaml_allow_additions_for_f06_paragraph_one():
    terms = {entry["term"]: entry for entry in load_banned_terms()["terms"]}
    assert terms["수익률"]["allow"] == ["영업이익률", "순이익률", "총수익률"]
    assert "보장한다고 밝혔" in terms["보장"]["allow"]
    assert scan("2030년까지 안전을 보장한다고 밝혔습니다") == []


def test_check_fields_only_scans_applies_to_and_ignores_excluded():
    record = {
        "headline": "지금 매수하시기 바랍니다",
        "explanation": "보도되었습니다.\n\n목표주가 5만원.",
        "limitation": "확정되지 않았습니다.",
        "fallback": None,
        # excluded — 금지어가 있어도 결과에 안 나온다
        "evidence_quote": "투자의견 매수",
        "commitment_quote": "수익을 보장합니다",
        "event_quote": "저평가 구간",
        "rationale": "명백한 위법입니다",
        "title": "유망한 종목",
        "summary": "사기를 저질렀습니다",
    }
    assert check_fields(record) == {"headline": ["매수"], "explanation": ["목표주가"]}
    excluded_only = {key: record[key] for key in load_banned_terms()["policy"]["excluded"]}
    assert check_fields(excluded_only) == {}
    assert check_fields({"headline": None, "explanation": 3}) == {}
    assert check_fields({}) == {}


def test_regeneration_hint_formats_policy_text():
    hint = regeneration_hint(["매수", "목표주가"])
    assert hint.startswith("다음 표현을 쓰지 말고 다시 쓰세요: 매수, 목표주가.")
    assert "{hit_terms}" not in hint
