"""services/match — gap_months(월말·소급) · target_year 강제 · 소급 문장 강제 · 인용 실패 재생성→폐기를 가짜 LLM 으로 DB 없이 검사한다 (D-10 · D-11)."""

from datetime import date

import pytest

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


# --------------------------------------------------------------------------- 후보 축소 (D-11): sub_tags 교집합 · 공약당 상한 · dry-run
def test_tags_overlap_excludes_disjoint_pairs_and_keeps_pairs_with_an_empty_side():
    # 실측의 무의미한 쌍: 정보보호 공약 × 산업재해 사건 — 같은 S 라도 교집합이 없으면 후보 아님
    info = commitment(id=1001, company_id=1, sub_tags=("정보보호·프라이버시",), filed_at=date(2025, 6, 30))
    accident = event(id=2005, company_id=1, sub_tags=("산업안전보건",), event_date=date(2026, 2, 3))
    assert cand.category_candidate(info, accident) is not None  # ① 은 통과
    assert cand.to_candidate(info, accident) is None  # ② 에서 제외
    # 핵심 쌍은 살아남는다: 정보보호 공약 × 정보유출 사건
    leak = event(id=2002, company_id=1, sub_tags=("정보보호·프라이버시",), event_date=date(2025, 9, 18))
    assert cand.to_candidate(info, leak) is not None
    # 한 개만 겹쳐도 후보 (KT G: 준법·윤리 · 공시·투명성 × 준법·윤리 · 정보보호·프라이버시)
    assert cand.tags_overlap(("준법·윤리", "공시·투명성"), ("준법·윤리", "정보보호·프라이버시"))
    # 한쪽이 빈 배열이면 category 일치만으로 후보 — 태그 누락으로 사건을 잃지 않는다
    assert cand.to_candidate(info, event(id=2009, company_id=1, sub_tags=(), event_date=date(2026, 2, 3))) is not None
    assert cand.to_candidate(commitment(id=1009, company_id=1, sub_tags=(), filed_at=date(2025, 6, 30)), accident) is not None
    assert cand.tags_overlap((), ()) and cand.tags_overlap(("a",), ()) and cand.tags_overlap((), ("b",))
    assert not cand.tags_overlap(("a",), ("b",))


def test_pair_candidates_applies_tag_rule_and_still_skips_existing_matches():
    commitments = [commitment(id=1, sub_tags=("산업안전보건",)), commitment(id=2, sub_tags=("정보보호·프라이버시",)), commitment(id=3)]
    events = [
        event(id=10, sub_tags=("산업안전보건",)),  # 1 과 겹침 · 2 와 안 겹침 · 3(빈 배열) 과는 통과
        event(id=11, sub_tags=()),  # 빈 배열 → 전부와 후보
        event(id=12, sub_tags=("정보보호·프라이버시",)),  # 2 와만 겹침 (+ 3)
    ]
    result = cand.pair_candidates(commitments, events, existing_pairs={(1, 11), (3, 12)})
    assert [(c.commitment.id, c.event.id) for c in result] == [(1, 10), (2, 11), (2, 12), (3, 10), (3, 11)]
    # 기존 matches 에 있는 쌍은 규칙을 통과해도 제외 — '무관' 도 저장돼 있어 재판정하지 않는다
    assert (1, 11) not in {(c.commitment.id, c.event.id) for c in result}
    assert (3, 12) not in {(c.commitment.id, c.event.id) for c in result}


def _ranked_events() -> list[EventInput]:
    """공약 하나에 대한 후보 5건. 최신순만 쓰면 A·E 가 남고 확정·다보도 사건이 잘린다."""
    return [
        event(id=1, confirmed=False, source_count=9, event_date=date(2026, 8, 1), reported_at=None),  # A: 최신·다보도지만 미확정
        event(id=2, confirmed=True, source_count=1, event_date=date(2025, 10, 1), reported_at=None),  # B: 확정, 가장 오래됨
        event(id=3, confirmed=True, source_count=3, event_date=date(2026, 1, 1), reported_at=None),  # C: 확정 · 3보도
        event(id=4, confirmed=True, source_count=3, event_date=date(2026, 3, 1), reported_at=None),  # D: 확정 · 3보도 · C 보다 최신
        event(id=5, confirmed=False, source_count=1, event_date=date(2026, 7, 1), reported_at=None),  # E: 최신이지만 미확정·1보도
    ]


def test_per_commitment_cap_keeps_confirmed_then_source_count_then_latest():
    candidates = cand.pair_candidates([commitment(id=100)], _ranked_events(), existing_pairs=set())
    assert len(candidates) == 5
    kept, truncated = cand.cap_per_commitment(candidates, 3)
    assert [c.event.id for c in kept] == [4, 3, 2] and truncated == 2  # D(확정·3·최신) → C(확정·3) → B(확정·1). A·E 절단
    kept, truncated = cand.cap_per_commitment(candidates, 4)
    assert [c.event.id for c in kept] == [4, 3, 2, 1] and truncated == 1  # 미확정 중에는 source_count 9 가 먼저
    kept, truncated = cand.cap_per_commitment(candidates, 5)
    assert [c.event.id for c in kept] == [4, 3, 2, 1, 5] and truncated == 0  # 상한 안이면 절단 없음
    kept, truncated = cand.cap_per_commitment(candidates, None)
    assert len(kept) == 5 and truncated == 0
    with pytest.raises(ValueError):
        cand.cap_per_commitment(candidates, 0)


def test_rank_uses_reference_date_so_retrospective_events_rank_by_reported_at():
    old_but_reported_late = event(id=1, confirmed=True, source_count=1, event_date=date(2024, 3, 1), reported_at=date(2026, 7, 30), is_retrospective=True)
    recent = event(id=2, confirmed=True, source_count=1, event_date=date(2026, 1, 1), reported_at=date(2026, 1, 1))
    candidates = cand.pair_candidates([commitment(id=100, filed_at=date(2025, 6, 30))], [old_but_reported_late, recent], existing_pairs=set())
    kept, truncated = cand.cap_per_commitment(candidates, 1)
    assert [c.event.id for c in kept] == [1] and truncated == 1  # 기준일(보도일 2026-07-30)이 더 최신


def test_total_limit_uses_the_same_priority_across_commitments():
    events = _ranked_events()
    candidates = cand.pair_candidates([commitment(id=100), commitment(id=200)], events, existing_pairs=set())
    kept, truncated, limited = cand.select_candidates(candidates, per_commitment=3, limit=4)
    assert truncated == 4 and limited == 2 and len(kept) == 4
    # 두 공약 모두 D·C 가 남고(확정·3보도), B 는 전체 상한에 잘린다. 순서는 commitment.id → rank
    assert [(c.commitment.id, c.event.id) for c in kept] == [(100, 4), (100, 3), (200, 4), (200, 3)]
    kept, truncated, limited = cand.select_candidates(candidates, per_commitment=None, limit=None)
    assert len(kept) == 10 and truncated == 0 and limited == 0
    with pytest.raises(ValueError):
        cand.apply_limit(candidates, 0)


def test_funnel_by_category_counts_three_stages_per_category():
    s_commitment = commitment(id=100, category="S", sub_tags=("산업안전보건",))
    g_commitment = commitment(id=200, category="G", sub_tags=("준법·윤리",))
    s_events = [event(id=e.id, category="S", sub_tags=("산업안전보건",), confirmed=e.confirmed, source_count=e.source_count, event_date=e.event_date, reported_at=None) for e in _ranked_events()]
    g_events = [event(id=90, category="G", sub_tags=("준법·윤리",)), event(id=91, category="G", sub_tags=("공시·투명성",))]
    tagged = cand.pair_candidates([s_commitment, g_commitment], s_events + g_events, existing_pairs=set())
    kept, _truncated, _limited = cand.select_candidates(tagged, per_commitment=3, limit=None)
    funnels = cand.funnel_by_category({"S": 5, "G": 2}, tagged, kept, per_commitment=3)
    assert funnels["S"] == cand.Funnel(by_category=5, by_tags=5, truncated=2, limited=0, kept=3)
    assert funnels["G"] == cand.Funnel(by_category=2, by_tags=1, truncated=0, limited=0, kept=1)  # 91 은 교집합 없음
    total = cand.CandidateSet(candidates=kept, per_category=funnels).total
    assert (total.by_category, total.by_tags, total.truncated, total.limited, total.kept) == (7, 6, 2, 0, 4)
    assert svc.funnel_line(total) == "후보 4건(카테고리 일치 7 → sub_tags 교집합 6 → 상한 절단 4) → LLM 호출 4회"


def test_candidate_sql_uses_array_overlap_and_cardinality_and_keeps_matches_exclusion():
    from sqlalchemy.dialects import postgresql

    tagged_sql = str(cand.candidate_stmt(1).compile(dialect=postgresql.dialect()))
    assert "commitments.sub_tags && events.sub_tags" in tagged_sql  # 교집합은 SQL 배열 연산자
    assert "cardinality(commitments.sub_tags) =" in tagged_sql and "cardinality(events.sub_tags) =" in tagged_sql  # 한쪽이 비면 통과
    assert "NOT (EXISTS (SELECT * \nFROM matches" in tagged_sql  # 기존 matches 제외는 그대로
    assert "EXTRACT(year FROM" in tagged_sql and "EXTRACT(month FROM" in tagged_sql  # gap 창도 SQL 에서
    assert "events.confirmed DESC, events.source_count DESC" in tagged_sql
    # ① 카테고리 일치는 count 만 — 교집합 조건이 없고 행을 가져오지 않는다
    count_sql = str(cand.count_by_category_stmt(1).compile(dialect=postgresql.dialect()))
    assert "count(*)" in count_sql and "GROUP BY commitments.category" in count_sql
    assert "&&" not in count_sql and "cardinality" not in count_sql and "NOT (EXISTS" in count_sql


def test_sql_gap_matches_python_gap_on_month_boundaries():
    # gap_sql 은 (연차×12 + 월차) 를 그대로 옮긴 식 — 파이썬 gap_months 와 같은 값이어야 한다
    from sqlalchemy import literal
    from sqlalchemy.dialects import postgresql

    expr = cand.gap_sql(literal(date(2025, 6, 30)), literal(date(2025, 7, 1)))
    sql = str(expr.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert sql.replace(" ", "") == "(EXTRACT(yearFROM'2025-07-01')-EXTRACT(yearFROM'2025-06-30'))*12+(EXTRACT(monthFROM'2025-07-01')-EXTRACT(monthFROM'2025-06-30'))"
    assert cand.gap_months(date(2025, 6, 30), date(2025, 7, 1)) == 1


def _fixture_like_set(per_commitment: int | None, limit: int | None) -> cand.CandidateSet:
    """DB 대신 쓰는 후보 집합: fixture KT S 와 비슷한 모양(공약 2 × 사건 2) + 절단 1건."""
    commitments = [commitment(id=1001, company_id=1, sub_tags=("정보보호·프라이버시",), filed_at=date(2025, 6, 30))]
    events = [
        event(id=2002, company_id=1, sub_tags=("정보보호·프라이버시",), event_date=date(2025, 9, 18), source_count=2),
        event(id=2004, company_id=1, sub_tags=("정보보호·프라이버시",), event_date=date(2025, 12, 31), source_count=1),
    ]
    tagged = cand.pair_candidates(commitments, events, existing_pairs=set())
    kept, _t, _l = cand.select_candidates(tagged, per_commitment=per_commitment, limit=limit)
    return cand.CandidateSet(candidates=kept, per_category=cand.funnel_by_category({"S": 5}, tagged, kept, per_commitment=per_commitment))


def test_dry_run_prints_three_stage_table_without_creating_llm_client_or_pipeline_run(monkeypatch, capsys):
    from esg_watchdog import cli

    class NoLLM:
        def __init__(self, *args, **kwargs):
            raise AssertionError("--dry-run 은 LLM 클라이언트를 만들지 않아야 한다")

    calls: dict = {}
    monkeypatch.setattr(svc, "LLMClient", NoLLM)
    monkeypatch.setattr(svc, "start_run", lambda stage: calls.setdefault("start_run", stage))
    monkeypatch.setattr(svc, "_load_company", lambda code: (1, f"KT({code})"))
    monkeypatch.setattr(svc, "_load_active_companies", lambda: [(1, "KT(030200)"), (2, "SPC삼립(005610)")])
    monkeypatch.setattr(svc, "load_candidates", lambda company_id, *, per_commitment, limit: _fixture_like_set(per_commitment, limit))

    assert cli.main(["match", "--company", "030200", "--dry-run", "--per-commitment", "1"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "LLM 호출 없음" in out and "start_run" not in calls
    assert "카테고리 일치" in out and "sub_tags 교집합" in out and "상한 절단" in out
    assert "KT(030200)" in out and "합계" in out
    # 표 값: ① 5(SQL count 대용) → ② 2 → ③ 1 (truncated 1)
    table_row = next(line for line in out.splitlines() if line.startswith("KT(030200)"))
    assert table_row.split() == ["KT(030200)", "S", "5", "2", "1", "1", "0"]
    assert "commitment 1001 × event 2002" in out and "commitment 1001 × event 2004" not in out  # source_count 2 가 남는다

    # --company 생략 → 활성 기업 전부
    assert cli.main(["match", "--dry-run"]) == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "KT(030200)" in out and "SPC삼립(005610)" in out

    # --dry-run 이 아니면 --company 필수
    assert cli.main(["match"]) == cli.EXIT_ERROR
    assert "--company 는 필수" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["match", "--company", "030200", "--per-commitment", "0"])
    args = cli.build_parser().parse_args(["match", "--company", "030200"])
    assert args.per_commitment == cand.DEFAULT_PER_COMMITMENT == 5 and args.limit is None and args.dry_run is False


def test_match_events_prints_funnel_line_and_records_funnel_stats(monkeypatch, tmp_path):
    lines: list[str] = []
    saved: list[dict] = []
    finished: dict = {}
    monkeypatch.setattr(svc, "_load_company", lambda code: (1, f"KT({code})"))
    monkeypatch.setattr(svc, "load_candidates", lambda company_id, *, per_commitment, limit: _fixture_like_set(per_commitment, limit))
    monkeypatch.setattr(svc, "start_run", lambda stage: 77)
    monkeypatch.setattr(svc, "finish_run", lambda run_id, status, stats, note=None: finished.update(run_id=run_id, status=status, stats=stats, note=note))
    monkeypatch.setattr(svc, "_upsert_match", saved.append)
    # _fixture_like_set 의 공약·사건 원문은 commitment()/event() 기본값이라 judgement() 기본 인용이 통과한다
    client, fake = make_client(tmp_path, [judgement()])

    result = svc.match_events(stock_code="030200", client=client, model="m", today=TODAY, log=lines.append, per_commitment=1, limit=None)
    assert lines[0] == "[match] KT(030200): 후보 1건(카테고리 일치 5 → sub_tags 교집합 2 → 상한 절단 1) → LLM 호출 1회"
    assert lines[1] == "[match] 절단: 공약당 상한 1 에 1건 · 전체 상한 - 에 0건"
    assert len(fake.calls) == 1 and len(saved) == 1 and saved[0]["commitment_id"] == 1001 and saved[0]["event_id"] == 2002
    stats = result.stats
    assert (stats["candidates"], stats["by_category"], stats["by_tags"], stats["truncated"], stats["limited"]) == (1, 5, 2, 1, 0)
    assert stats["per_commitment"] == 1 and stats["limit"] is None and stats["upserted"] == 1 and stats["llm_calls"] == 1
    assert finished["run_id"] == 77 and finished["status"] == "success" and finished["stats"]["truncated"] == 1
