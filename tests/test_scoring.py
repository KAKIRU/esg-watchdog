"""services/score/scoring.py · knowledge/weights.py — 산식·클램프·confirmed 0.6·이행긍정 0·EVENT_TYPES 와 가중치 키 일치를 고정한다 (D-08 · D-13 · D-14 · D-16 · D-39)."""

from esg_watchdog.knowledge import weights
from esg_watchdog.knowledge.taxonomy import EVENT_TYPES, GRADES, RELATIONS
from esg_watchdog.services.score import scoring

FINE_539 = {"fine_amount": 53_979_000_000, "casualties": 0, "lawsuit": True, "is_repeat": True, "regulator": "개인정보보호위원회"}


# --------------------------------------------------------------------------- 가중치 표 (D-39)
def test_industry_weight_keys_match_event_types_exactly():
    assert set(weights.INDUSTRY_EVENT_WEIGHT) == {"telecom", "food"}  # D-38
    for industry, table in weights.INDUSTRY_EVENT_WEIGHT.items():
        assert set(table) == set(EVENT_TYPES), industry
        assert len(table) == 12
        assert all(0 <= value <= 40 for value in table.values()), industry
        assert table["이행조치"] == 0
    assert set(weights.RELATION_COEF) == set(RELATIONS)
    assert weights.RELATION_COEF["이행긍정"] == 0.0 and weights.RELATION_COEF["무관"] == 0.0
    assert [grade for grade, _ in weights.GRADE_THRESHOLDS] == list(reversed(GRADES))
    # D-16 실측 조정: 60/45/30. 마지막 등급('주의')의 임계값이 곧 발행 하한 — 그 아래는 등급을 매길 일이 없다
    assert [threshold for _, threshold in weights.GRADE_THRESHOLDS] == [60, 45, 30]
    assert weights.GRADE_THRESHOLDS[-1][1] == weights.MIN_PUBLISH_MATERIALITY == 30
    assert "실측" in weights.__doc__ and "D-16" in weights.__doc__
    assert weights.__doc__.startswith("기본값. D-13·D-14·D-16 은 B 가 검토 후 조정한다")


def test_industry_weight_lookup_and_default():
    assert scoring.industry_weight("telecom", "정보유출") == 40
    assert scoring.industry_weight("food", "제품안전·품질") == 40
    assert scoring.industry_weight("food", "정보유출") == 14
    assert scoring.industry_weight("shipbuilding", "제재") == weights.DEFAULT_INDUSTRY_WEIGHT
    assert scoring.industry_weight(None, None) == weights.DEFAULT_INDUSTRY_WEIGHT
    assert scoring.industry_weight("telecom", "없는유형") == weights.DEFAULT_INDUSTRY_WEIGHT


# --------------------------------------------------------------------------- severity
def test_severity_tiers_and_clamp():
    empty = {"fine_amount": None, "casualties": 0, "lawsuit": False, "is_repeat": False, "regulator": None}
    assert scoring.severity(empty) == 0
    assert scoring.severity(None) == 0 and scoring.severity({}) == 0
    assert scoring.severity({**empty, "fine_amount": 0}) == 0
    assert scoring.severity({**empty, "fine_amount": 50_000_000}) == 10  # <1억
    assert scoring.severity({**empty, "fine_amount": 100_000_000}) == 20  # 1억
    assert scoring.severity({**empty, "fine_amount": 999_999_999}) == 20
    assert scoring.severity({**empty, "fine_amount": 1_000_000_000}) == 30  # 10억
    assert scoring.severity({**empty, "fine_amount": 10_000_000_000}) == 40  # 100억
    assert scoring.severity({**empty, "casualties": 1}) == 20
    assert scoring.severity({**empty, "casualties": 2}) == 20
    assert scoring.severity({**empty, "casualties": 3}) == 30
    assert scoring.severity({**empty, "lawsuit": True}) == 10
    assert scoring.severity({**empty, "is_repeat": True}) == 15
    assert scoring.severity({**empty, "regulator": "고용노동부"}) == 10
    assert scoring.severity(FINE_539) == 75  # 40 + 10 + 15 + 10
    # 전부 최대 → 105 → 100 클램프
    assert scoring.severity({"fine_amount": 10**11, "casualties": 5, "lawsuit": True, "is_repeat": True, "regulator": "x"}) == 100
    # 이상한 값은 0 으로
    assert scoring.severity({"fine_amount": "많음", "casualties": None}) == 0


# --------------------------------------------------------------------------- materiality
def test_materiality_formula_and_clamp():
    assert scoring.materiality(34, 75, 1.0, 1.0) == 79  # 34 + 45
    assert scoring.materiality(40, 100, 1.0, 1.0) == 100  # 100 → 100 클램프
    assert scoring.materiality(40, 100, 1.0, 0.6) == 60
    assert scoring.materiality(38, 45, 0.5, 1.0) == 33  # (38 + 27) × 0.5 = 32.5 → 33 (사사오입)
    assert scoring.materiality(25, 0, 0.7, 1.0) == 18  # 17.5 → 18
    assert scoring.materiality(0, 0, 1.0, 1.0) == 0
    assert scoring.round_half_up(2.5) == 3 and scoring.round_half_up(2.49) == 2
    assert scoring.clamp(-5) == 0 and scoring.clamp(105) == 100 and scoring.clamp(42.9) == 42


def test_unconfirmed_event_multiplies_by_0_6():
    confirmed = scoring.score_match(industry_key="telecom", event_type="제재", severity_signals=FINE_539, relation="위반", confirmed=True, llm_confidence=84, source_count=2, thin_source=False)
    unconfirmed = scoring.score_match(industry_key="telecom", event_type="제재", severity_signals=FINE_539, relation="위반", confirmed=False, llm_confidence=84, source_count=2, thin_source=False)
    assert confirmed["materiality"] == 79 and confirmed["components"]["confirmed_coef"] == 1.0
    assert unconfirmed["materiality"] == 47  # 79 × 0.6 = 47.4 → 47
    assert unconfirmed["components"]["confirmed_coef"] == 0.6
    assert confirmed["confidence"] == 82  # 42 + 20 + 20
    assert unconfirmed["confidence"] == 62  # confirmed 20 점도 빠진다


def test_positive_and_unrelated_relations_score_zero_materiality():
    for relation in ("이행긍정", "무관"):
        scores = scoring.score_match(industry_key="telecom", event_type="이행조치", severity_signals={"casualties": 3, "lawsuit": True}, relation=relation, confirmed=True, llm_confidence=72, source_count=1, thin_source=True)
        assert scores["materiality"] == 0, relation
        assert scores["components"]["relation_coef"] == 0.0
        assert scores["confidence"] == 56  # 36 + 10 + 20 − 10 — 신뢰도는 그대로 계산된다
    assert scoring.relation_coef("후퇴") == 0.7 and scoring.relation_coef("이행지연") == 0.5 and scoring.relation_coef("없음") == 0.0


# --------------------------------------------------------------------------- confidence · scores 형태
def test_confidence_formula_and_clamp():
    assert scoring.confidence(0, 0, False, False) == 0
    assert scoring.confidence(100, 3, True, False) == 100  # 50 + 30 + 20
    assert scoring.confidence(100, 5, True, False) == 100  # ≥3 은 30 고정
    assert scoring.confidence(60, 1, False, True) == 30  # 30 + 10 − 10
    assert scoring.confidence(60, 2, False, False) == 50
    assert scoring.confidence(5, 0, False, True) == 0  # 2.5 − 10 → 클램프 0
    assert scoring.confidence(61, 1, False, False) == 41  # 30.5 + 10 = 40.5 → 41


def test_score_match_shape_matches_contract():
    scores = scoring.score_match(industry_key="food", event_type="산업재해", severity_signals={"casualties": 3, "is_repeat": True, "regulator": "고용노동부"}, relation="위반", confirmed=True, llm_confidence=70, source_count=1, thin_source=False)
    assert set(scores) == {"materiality", "confidence", "components"}
    assert tuple(scores["components"]) == scoring.COMPONENT_KEYS == ("industry_weight", "relation_coef", "severity", "confirmed_coef")
    assert scores["components"] == {"industry_weight": 38, "relation_coef": 1.0, "severity": 55, "confirmed_coef": 1.0}
    assert scores["materiality"] == 71  # 38 + 33
    assert scores["confidence"] == 65  # 35 + 10 + 20
    assert all(isinstance(scores[key], int) for key in ("materiality", "confidence"))
