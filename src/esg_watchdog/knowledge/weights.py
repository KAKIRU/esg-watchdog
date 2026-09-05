"""기본값. D-13·D-14·D-16 은 B 가 검토 후 조정한다(가중치가 바뀌면 esg-watchdog score 로 전체 재계산, D-15).

- INDUSTRY_EVENT_WEIGHT: 산업(industry_key, D-38 로 telecom · food 확정) × event_type → 0~40. SASB 참조 자리(값은 기본값).
  두 dict 의 키 집합은 knowledge/taxonomy.py 의 EVENT_TYPES 12개와 정확히 같아야 한다(D-39, 테스트로 고정).
- RELATION_COEF(D-14) · CONFIRMED_COEF(D-08): 정답지 점수 분포 확인 후 B 조정.
- GRADE_THRESHOLDS · MIN_PUBLISH_MATERIALITY(D-16): 2026-09-02 실측(실데이터 216 매칭)에서 중대성이 10~50대에 몰려 80 이상이 2건뿐이라
  80/70 → 60/45/30 으로 내리고, 30 미만은 발행하지 않는다(대조군 오뚜기의 약한 '후퇴' 19건이 경보로 나가지 않게).
- SEVERITY_* · CONFIDENCE_*: services/score/scoring.py 의 산식 상수. 산식 자체는 scoring.py 에 있다.
"""

INDUSTRY_EVENT_WEIGHT: dict[str, dict[str, int]] = {
    "telecom": {
        "정보유출": 40,
        "제재": 34,
        "규제위반": 32,
        "지배구조·준법": 32,
        "소송·수사": 30,
        "재무영향": 26,
        "제품안전·품질": 22,
        "산업재해": 20,
        "공급망·협력사": 16,
        "환경오염": 14,
        "이행조치": 0,
        "기타": 15,
    },
    "food": {
        "제품안전·품질": 40,
        "산업재해": 38,
        "제재": 34,
        "규제위반": 32,
        "소송·수사": 30,
        "지배구조·준법": 28,
        "환경오염": 26,
        "공급망·협력사": 26,
        "재무영향": 25,
        "정보유출": 14,
        "이행조치": 0,
        "기타": 15,
    },
}
# 산업(industry_key)이 표에 없거나 event_type 이 그 산업 표에 없을 때
DEFAULT_INDUSTRY_WEIGHT = 20

# D-14 초기값. 이행긍정·무관은 0.0 → materiality 0
RELATION_COEF: dict[str, float] = {"위반": 1.0, "후퇴": 0.7, "이행지연": 0.5, "이행긍정": 0.0, "무관": 0.0}

# D-08: 미확정(의혹·수사 단계) 사건은 중대성을 0.6 배
CONFIRMED_COEF: dict[bool, float] = {True: 1.0, False: 0.6}

# D-16 — materiality 가 threshold 이상이면 그 등급(위에서부터 첫 매치). 실측 분포(10~50대 집중, 80 이상 2건)에 맞춰 80/70 → 60/45/30
GRADE_THRESHOLDS: list[tuple[str, int]] = [("심각", 60), ("경고", 45), ("주의", 30)]
# D-16 발행 하한 — accepted 매칭이라도 materiality 가 이 값 미만이면 경보 대상에서 제외한다(폐기가 아니라 미발행, matches 는 그대로)
MIN_PUBLISH_MATERIALITY = 30

# SEVERITY (0~100 clamp): fine_amount 원 단위 → 없음 0 / <1억 10 / 1~10억 20 / 10~100억 30 / ≥100억 40
FINE_TIERS: list[tuple[int, int]] = [(10_000_000_000, 40), (1_000_000_000, 30), (100_000_000, 20), (1, 10)]
# casualties → 0 / 1~2 → 20 / ≥3 → 30
CASUALTY_TIERS: list[tuple[int, int]] = [(3, 30), (1, 20)]
LAWSUIT_POINTS = 10
REPEAT_POINTS = 15
REGULATOR_POINTS = 10

# CONFIDENCE (0~100 clamp): 0.5×llm_confidence + source_count(1:10 / 2:20 / ≥3:30) + (confirmed ? 20 : 0) − (thin_source ? 10 : 0)
LLM_CONFIDENCE_WEIGHT = 0.5
SOURCE_COUNT_TIERS: list[tuple[int, int]] = [(3, 30), (2, 20), (1, 10)]
CONFIRMED_POINTS = 20
THIN_SOURCE_PENALTY = 10

# materiality = clamp(round((industry_weight + SEVERITY_WEIGHT×severity) × relation_coef × confirmed_coef), 0, 100)
SEVERITY_WEIGHT = 0.6
