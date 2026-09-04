"""services/match — gap_months(월말·소급) · target_year 강제 · 소급 문장 강제 · 인용 실패 재생성→폐기를 가짜 LLM 으로 DB 없이 검사한다 (D-10 · D-11)."""

from datetime import date

from esg_watchdog.llm.client import LLMClient
from esg_watchdog.llm.providers.fake_provider import FakeProvider
from esg_watchdog.prompts.relation_judge import (
    PROMPT_VERSION,
    CommitmentInput,
    EventInput,
    RelationJudgement,
    build_user_prompt,
    fact_sentence,
)
from esg_watchdog.services.match import candidates as cand
from esg_watchdog.services.match import judge as svc

TODAY = date(2026, 9, 4)


def commitment(**overrides) -> CommitmentInput:
    base = {
        "id": 1005, "company_id": 2, "category": "S",
        "commitment_text": "중대재해 재발 방지를 위해 전 사업장 설비 안전장치를 정기 점검한다.",
        "normalized_text": "전 사업장 안전장치 정기 점검", "commitment_type": "정성", "metric": None, "target_value": None, "target_year": None,
        "filed_at": date(2025, 8, 14),
    }
    base.update(overrides)
    return CommitmentInput(**base)


def event(**overrides) -> EventInput:
    base = {
        "id": 2005, "company_id": 2, "category": "S", "title": "시화공장 화재로 공장 전체 가동 중단",
        "summary": "시화공장에서 화재가 발생해 공장 전체 가동이 중단됐다고 보도되었습니다.",
        "evidence_quote": "화재로 공장 전체 가동이 중단됐다", "event_date": date(2026, 2, 3), "reported_at": date(2026, 2, 3),
        "is_retrospective": False, "confirmed": True, "confirmed_basis": "회사 인정",
    }
    base.update(overrides)
    return EventInput(**base)


def judgement(**overrides) -> dict:
    base = {
        "relation": "위반",
        "rationale": "공약은 전 사업장 안전장치 정기 점검을 명시했습니다. 이후 같은 공장에서 화재가 발생해 가동이 중단됐다고 보도되었습니다. 두 사실은 어긋납니다.",
        "commitment_quote": "전 사업장 설비 안전장치를 정기 점검한다",
        "event_quote": "공장 전체 가동이 중단됐다",
        "llm_confidence": 70,
    }
    base.update(overrides)
    return base


def make_client(tmp_path, responses):
    fake = FakeProvider(responses)
    return LLMClient(provider=fake, cache_dir=tmp_path, sleep=lambda _seconds: None), fake


# --------------------------------------------------------------------------- gap_months (D-10)
def test_gap_months_ignores_days_and_uses_year_and_month_difference():
    assert cand.gap_months(date(2025, 6, 30), date(2025, 7, 1)) == 1  # 월말 → 다음달 초도 1개월
    assert cand.gap_months(date(2025, 6, 1), date(2025, 6, 30)) == 0  # 같은 달은 0 → 창 밖
    assert cand.gap_months(date(2025, 6, 30), date(2027, 6, 30)) == 24
    assert cand.gap_months(date(2025, 6, 30), date(2027, 7, 1)) == 25
    assert cand.gap_months(date(2025, 8, 14), date(2025, 7, 20)) == -1  # 공시 전 사건은 음수 → 창 밖
    assert cand.in_window(1) and cand.in_window(24)
    assert not cand.in_window(0) and not cand.in_window(25) and not cand.in_window(-1)


def test_retrospective_event_uses_reported_at_as_reference():
    # fixture 2001: event_date 2024-03 · reported_at 2026-07-30 · filed_at 2025-06-30 → 13개월 (reported_at 기준)
    assert cand.reference_date(date(2024, 3, 1), date(2026, 7, 30), True) == date(2026, 7, 30)
    assert cand.reference_date(date(2024, 3, 1), date(2026, 7, 30), False) == date(2024, 3, 1)
    assert cand.reference_date(date(2024, 3, 1), None, True) == date(2024, 3, 1)
    retro = event(id=2001, category="G", event_date=date(2024, 3, 1), reported_at=date(2026, 7, 30), is_retrospective=True)
    filed = commitment(id=1002, category="G", filed_at=date(2025, 6, 30))
    candidate = cand.to_candidate(filed, retro)
    assert candidate is not None and candidate.gap_months == 13
    # 소급이 아니면 event_date 기준 → 공시 전(음수) → 후보 아님
    assert cand.to_candidate(filed, event(id=2001, category="G", event_date=date(2024, 3, 1), reported_at=date(2026, 7, 30))) is None


def test_pair_candidates_requires_same_company_and_category_and_skips_existing_pairs():
    commitments = [commitment(id=1), commitment(id=2, category="E"), commitment(id=3, company_id=9)]
    events = [event(id=10), event(id=11, category="E"), event(id=12, event_date=date(2028, 1, 1), reported_at=None)]
    result = cand.pair_candidates(commitments, events, existing_pairs={(2, 11)})
    assert [(c.commitment.id, c.event.id, c.gap_months) for c in result] == [(1, 10, 6)]


def test_fact_sentence_and_prompt_do_not_ask_llm_to_compute_dates():
    assert fact_sentence(6, False) == "이 사건은 공약 공시 6개월 후에 발생했습니다."
    retro = fact_sentence(13, True, event_before_filing=True)
    assert retro.startswith("이 사건은 공약 공시 13개월 후에 보도로 확인되었습니다.")
    assert "공시 이전에 발생했으나 공시 이후 보도로 확인된 소급 건" in retro
    assert "소급 건" in fact_sentence(3, True)

    prompt = build_user_prompt(commitment(target_year=2029), event(), 6, today=TODAY, failed_quotes=["event_quote: 없는 문장"])
    assert "이 사건은 공약 공시 6개월 후에 발생했습니다." in prompt
    assert "목표연도 2029년은 아직 도래하지 않았습니다" in prompt and "'위반' 판정 금지" in prompt
    assert "- event_quote: 없는 문장" in prompt and "원문에 없습니다" in prompt
    assert "목표연도가 없습니다" in build_user_prompt(commitment(), event(), 6, today=TODAY)
    assert PROMPT_VERSION == "f04-v1"
    schema = RelationJudgement.model_json_schema()
    assert schema["properties"]["relation"]["enum"] == ["위반", "후퇴", "이행지연", "이행긍정", "무관"]
    assert schema["required"] == list(schema["properties"])


# --------------------------------------------------------------------------- (a) target_year 강제 · (b) 소급 문장 강제
def test_violation_with_pending_target_year_is_forced_to_delay_with_note():
    candidate = cand.to_candidate(commitment(target_year=2029), event())
    forced, notes = svc.enforce(RelationJudgement(**judgement()), candidate, TODAY)
    assert forced.relation == "이행지연"
    assert forced.rationale.endswith(svc.PENDING_NOTE)
    assert len(notes) == 1 and "위반 → 이행지연" in notes[0]
    # 목표연도가 도래했거나 없으면 그대로
    same, notes = svc.enforce(RelationJudgement(**judgement()), cand.to_candidate(commitment(target_year=2026), event()), TODAY)
    assert same.relation == "위반" and notes == []
    assert svc.target_year_pending(None, TODAY) is False and svc.target_year_pending(2027, TODAY) is True
    # '후퇴' 는 목표연도 미도래여도 바꾸지 않는다
    kept, _ = svc.enforce(RelationJudgement(**judgement(relation="후퇴")), candidate, TODAY)
    assert kept.relation == "후퇴"


def test_retrospective_rationale_gets_retro_mark_only_when_missing():
    retro_event = event(id=2001, category="G", event_date=date(2024, 3, 1), reported_at=date(2026, 7, 30), is_retrospective=True)
    candidate = cand.to_candidate(commitment(id=1002, category="G", filed_at=date(2025, 6, 30)), retro_event)
    forced, notes = svc.enforce(RelationJudgement(**judgement()), candidate, TODAY)
    assert svc.RETRO_MARK in forced.rationale and forced.rationale.endswith(svc.RETRO_SENTENCE)
    assert any("소급 확인" in note for note in notes)
    already = RelationJudgement(**judgement(rationale="공시 이후 보도로 소급 확인된 건입니다. 신고 의무와 어긋납니다. 확정 처분입니다."))
    unchanged, notes = svc.enforce(already, candidate, TODAY)
    assert unchanged.rationale == already.rationale and notes == []


# --------------------------------------------------------------------------- (c) 인용 실패 → 재생성 1회 → 폐기
def test_quote_failure_regenerates_once_with_hint_then_accepts(tmp_path):
    candidate = cand.to_candidate(commitment(), event())
    client, fake = make_client(tmp_path, [judgement(event_quote="화재로 인명 피해 발생"), judgement()])
    outcome = svc.judge_candidate(client, "m", candidate, today=TODAY, log=lambda _line: None)
    assert len(fake.calls) == 2 and outcome.regenerated and outcome.attempts == 2
    assert "event_quote: 화재로 인명 피해 발생" in fake.calls[1]["user"]
    assert outcome.judgement is not None and outcome.discarded is None
    values = svc.match_values(candidate, outcome.judgement)
    assert values["status"] == "accepted" and values["scores"] is None and values["gap_months"] == 6
    assert values["prompt_version"] == PROMPT_VERSION
    assert values["evidence_quotes"] == {"commitment_quote": "전 사업장 설비 안전장치를 정기 점검한다", "event_quote": "공장 전체 가동이 중단됐다"}


def test_second_quote_failure_is_discarded(tmp_path):
    candidate = cand.to_candidate(commitment(), event())
    client, fake = make_client(tmp_path, [judgement(commitment_quote="없는 공약 문장"), judgement(commitment_quote="또 없는 문장")])
    outcome = svc.judge_candidate(client, "m", candidate, today=TODAY, log=lambda _line: None)
    assert len(fake.calls) == 2
    assert outcome.judgement is None
    assert outcome.discarded is not None and "인용 실패" in outcome.discarded and "또 없는 문장" in outcome.discarded


def test_event_quote_falls_back_to_summary_when_evidence_quote_is_empty(tmp_path):
    candidate = cand.to_candidate(commitment(), event(evidence_quote=""))
    assert svc.event_source(candidate) == candidate.event.summary
    client, fake = make_client(tmp_path, [judgement(event_quote="공장 전체 가동이 중단됐다고 보도되었습니다")])
    outcome = svc.judge_candidate(client, "m", candidate, today=TODAY, log=lambda _line: None)
    assert len(fake.calls) == 1 and outcome.judgement is not None


def test_schema_failure_regenerates_once_then_discards(tmp_path):
    candidate = cand.to_candidate(commitment(), event())
    client, fake = make_client(tmp_path, [{"relation": "모름"}, judgement(relation="무관")])
    outcome = svc.judge_candidate(client, "m", candidate, today=TODAY, log=lambda _line: None)
    assert len(fake.calls) == 2 and "형식에 맞지 않았습니다" in fake.calls[1]["user"]
    assert outcome.judgement is not None and outcome.judgement.relation == "무관"  # '무관' 도 저장 대상

    broken, fake = make_client(tmp_path / "b", [{"relation": "모름"}, {"relation": "x"}])
    outcome = svc.judge_candidate(broken, "m", candidate, today=TODAY, log=lambda _line: None)
    assert outcome.judgement is None and outcome.discarded.startswith(f"{candidate.label}: (스키마 실패)")


def test_confidence_is_clamped_to_0_100():
    candidate = cand.to_candidate(commitment(), event())
    assert svc.match_values(candidate, RelationJudgement(**judgement(llm_confidence=140)))["llm_confidence"] == 100
    assert svc.match_values(candidate, RelationJudgement(**judgement(llm_confidence=-3)))["llm_confidence"] == 0
