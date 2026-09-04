"""F-05 점수 산식 — 순수 함수만, DB import 없음 (D-08 · D-13 · D-14 · D-16 · D-39).

- severity(signals): fine_amount 계층 + casualties 계층 + lawsuit + is_repeat + regulator → clamp 0~100.
- industry_weight(industry_key, event_type): knowledge/weights.INDUSTRY_EVENT_WEIGHT, 없으면 DEFAULT_INDUSTRY_WEIGHT.
- materiality(industry_weight, severity, relation_coef, confirmed_coef) = clamp(round((industry_weight + 0.6×severity) × relation_coef × confirmed_coef), 0, 100).
  이행긍정·무관은 relation_coef 0.0 → materiality 0.
- confidence(llm_confidence, source_count, confirmed, thin_source) = clamp(0.5×llm + source계층 + confirmed 20 − thin 10, 0, 100).
- score_match(...) → matches.scores JSONB 그대로: {"materiality", "confidence", "components": {industry_weight, relation_coef, severity, confirmed_coef}}.
- 상수(가중치·계수·계층)는 전부 knowledge/weights.py — 여기서는 산식만 둔다. 반올림은 사사오입(0.5 → 올림).
"""

import math
from collections.abc import Mapping

from esg_watchdog.knowledge.weights import (
    CASUALTY_TIERS,
    CONFIRMED_COEF,
    CONFIRMED_POINTS,
    DEFAULT_INDUSTRY_WEIGHT,
    FINE_TIERS,
    INDUSTRY_EVENT_WEIGHT,
    LAWSUIT_POINTS,
    LLM_CONFIDENCE_WEIGHT,
    REGULATOR_POINTS,
    RELATION_COEF,
    REPEAT_POINTS,
    SEVERITY_WEIGHT,
    SOURCE_COUNT_TIERS,
    THIN_SOURCE_PENALTY,
)

COMPONENT_KEYS = ("industry_weight", "relation_coef", "severity", "confirmed_coef")


def clamp(value: float, low: int = 0, high: int = 100) -> int:
    return int(max(low, min(high, value)))


def round_half_up(value: float) -> int:
    return math.floor(value + 0.5)


def _tier(value: int, tiers: list[tuple[int, int]]) -> int:
    """tiers 는 (하한, 점수) 내림차순. value ≥ 하한 인 첫 점수, 없으면 0."""
    for floor, points in tiers:
        if value >= floor:
            return points
    return 0


def _as_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def severity(signals: Mapping | None) -> int:
    """events.severity_signals(fine_amount · casualties · lawsuit · is_repeat · regulator) → 0~100."""
    signals = signals or {}
    total = _tier(_as_int(signals.get("fine_amount")), FINE_TIERS)
    total += _tier(_as_int(signals.get("casualties")), CASUALTY_TIERS)
    if signals.get("lawsuit"):
        total += LAWSUIT_POINTS
    if signals.get("is_repeat"):
        total += REPEAT_POINTS
    if signals.get("regulator"):
        total += REGULATOR_POINTS
    return clamp(total)


def industry_weight(industry_key: str | None, event_type: str | None) -> int:
    table = INDUSTRY_EVENT_WEIGHT.get(industry_key or "")
    if table is None:
        return DEFAULT_INDUSTRY_WEIGHT
    return table.get(event_type or "", DEFAULT_INDUSTRY_WEIGHT)


def relation_coef(relation: str) -> float:
    return RELATION_COEF.get(relation, 0.0)


def confirmed_coef(confirmed: bool) -> float:
    return CONFIRMED_COEF[bool(confirmed)]


def materiality(industry_weight: int, severity: int, relation_coef: float, confirmed_coef: float) -> int:
    return clamp(round_half_up((industry_weight + SEVERITY_WEIGHT * severity) * relation_coef * confirmed_coef))


def confidence(llm_confidence: int, source_count: int, confirmed: bool, thin_source: bool) -> int:
    total = LLM_CONFIDENCE_WEIGHT * _as_int(llm_confidence)
    total += _tier(_as_int(source_count), SOURCE_COUNT_TIERS)
    if confirmed:
        total += CONFIRMED_POINTS
    if thin_source:
        total -= THIN_SOURCE_PENALTY
    return clamp(round_half_up(total))


def components(industry_weight: int, relation_coef: float, severity: int, confirmed_coef: float) -> dict:
    """화면은 이 dict 를 순회해 막대를 그린다 — 키 4개·순서 고정 (D-36)."""
    return {
        "industry_weight": industry_weight,
        "relation_coef": relation_coef,
        "severity": severity,
        "confirmed_coef": confirmed_coef,
    }


def score_match(
    *,
    industry_key: str | None,
    event_type: str | None,
    severity_signals: Mapping | None,
    relation: str,
    confirmed: bool,
    llm_confidence: int,
    source_count: int,
    thin_source: bool,
) -> dict:
    """matches.scores 값 하나. 이행긍정·무관은 materiality 0 · components.relation_coef 0.0."""
    weight = industry_weight(industry_key, event_type)
    sev = severity(severity_signals)
    rel = relation_coef(relation)
    conf = confirmed_coef(confirmed)
    return {
        "materiality": materiality(weight, sev, rel, conf),
        "confidence": confidence(llm_confidence, source_count, confirmed, thin_source),
        "components": components(weight, rel, sev, conf),
    }
