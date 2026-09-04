"""services/detect/events.py — 가짜 LLM 으로 중복 병합 · quote 실패 재생성 · is_subject false 제외 · 택소노미 강등을 DB 없이 검사한다 (D-05 · D-07 · D-08)."""

from datetime import date, timedelta

import pytest

from esg_watchdog.llm.client import LLMClient
from esg_watchdog.llm.providers.fake_provider import FakeProvider
from esg_watchdog.prompts.event_detect import (
    PROMPT_VERSION,
    ArticleInput,
    EventBatch,
    build_user_prompt,
)
from esg_watchdog.services.detect import events as svc

COMPANY = svc.CompanyTarget(id=2, stock_code="005610", name="SPC삼립", aliases=["SPC삼립", "삼립"])

A1 = ArticleInput(id=105, title="SPC삼립 시화공장 화재로 공장 전체 가동 중단", description="근로자 3명이 연기를 흡입했다", published_at=date(2026, 2, 3), press="bizjoongang.co.kr")
A2 = ArticleInput(id=109, title="삼립 시화공장 화재… 가동 중단 이틀째", description="화재로 공장 전체 가동이 중단됐다", published_at=date(2026, 2, 4), press="laborplus.co.kr")
A3 = ArticleInput(id=110, title="SPC삼립, 신제품 출시로 매출 증대 기대", description="봄 신제품 3종을 내놨다", published_at=date(2026, 2, 5), press="fnnews.com")
A4 = ArticleInput(id=111, title="SPC그룹 계열 던킨 매장 위생 논란", description="던킨 매장에서 위생 문제가 보도됐다", published_at=date(2026, 2, 6), press="x.kr")


def judgement(article_id: int, **overrides) -> dict:
    base = {
        "article_id": article_id,
        "is_esg_event": True,
        "is_subject": True,
        "via_subsidiary": False,
        "category": "S",
        "sub_tags": ["산업안전보건"],
        "event_type": "산업재해",
        "title": "시화공장 화재로 공장 전체 가동 중단",
        "summary": "시화공장에서 화재가 발생해 가동이 중단됐다고 보도되었습니다.",
        "event_date": "2026-02-03",
        "date_precision": "day",
        "severity_signals": {"fine_amount": None, "casualties": 3, "lawsuit": False, "is_repeat": True, "regulator": "고용노동부"},
        "confirmed": True,
        "confirmed_basis": "회사 인정",
        "evidence_quote": "화재로 공장 전체 가동 중단",
        "is_retrospective": False,
    }
    base.update(overrides)
    return base


def not_event(article_id: int, **overrides) -> dict:
    return judgement(
        article_id,
        is_esg_event=False,
        category=None,
        sub_tags=[],
        event_type=None,
        title=None,
        summary=None,
        event_date=None,
        date_precision=None,
        severity_signals=None,
        confirmed=False,
        confirmed_basis=None,
        evidence_quote=None,
        **overrides,
    )


def make_client(tmp_path, responses):
    fake = FakeProvider(responses)
    return LLMClient(provider=fake, cache_dir=tmp_path, sleep=lambda _seconds: None), fake


# --------------------------------------------------------------------------- 중복 병합 (D-07)
def candidate(article_id: int, **overrides) -> svc.EventCandidate:
    base = {
        "article_id": article_id, "published_at": date(2026, 2, 3), "category": "S", "sub_tags": ["산업안전보건"], "event_type": "산업재해",
        "title": "시화공장 화재로 공장 전체 가동 중단", "summary": "s", "event_date": date(2026, 2, 3), "date_precision": "day",
        "severity_signals": dict(svc.EMPTY_SIGNALS), "via_subsidiary": False, "confirmed": False, "confirmed_basis": "없음",
        "is_retrospective": False, "evidence_quote": "q",
    }
    base.update(overrides)
    return svc.EventCandidate(**base)


def signals(**overrides) -> dict:
    return {**svc.EMPTY_SIGNALS, **overrides}


def test_totally_different_titles_merge_when_category_type_and_month_match(tmp_path):
    """언론사마다 제목이 달라도 (category, event_type, 연-월) 이 같으면 한 사건 — 제목은 키에 안 들어간다."""
    client, _ = make_client(
        tmp_path,
        [{"judgements": [
            judgement(105),
            judgement(109, title="삼립 시화공장 불… 가동 이틀째 멈춰", event_date="2026-02-04", evidence_quote="화재로 공장 전체 가동이 중단됐다"),
            not_event(110),
        ]}],
    )
    outcome = svc.detect_batch(client, "m", COMPANY, [A1, A2, A3], log=lambda _line: None)
    assert len(outcome.candidates) == 2 and outcome.not_event == 1 and outcome.judged == 3

    new_events, merges = svc.merge_candidates(outcome.candidates, [], COMPANY.id)
    assert merges == []
    assert len(new_events) == 1
    values = svc.event_values(COMPANY.id, new_events[0], [])
    assert values["sources"] == [105, 109]
    assert values["source_count"] == 2 and values["thin_source"] is False
    assert values["reported_at"] == date(2026, 2, 3)
    assert values["title"] == "시화공장 화재로 공장 전체 가동 중단"  # 가장 이른 기사의 제목이 대표
    assert values["event_date"] == date(2026, 2, 3) and values["date_precision"] == "day"  # 둘 다 day 면 대표 행의 값
    assert values["prompt_version"] == PROMPT_VERSION
    assert values["event_type"] == "산업재해" and values["confirmed"] is True and values["confirmed_basis"] == "회사 인정"


def test_dedup_key_ignores_title_and_uses_company_category_type_month():
    assert svc.dedup_key(2, "S", "산업재해", date(2026, 2, 3)) == svc.dedup_key(2, "S", "산업재해", date(2026, 2, 28))
    assert svc.dedup_key(2, "S", "산업재해", date(2026, 2, 3)) != svc.dedup_key(2, "S", "산업재해", date(2026, 3, 1))
    assert svc.dedup_key(2, "S", "산업재해", date(2026, 2, 3)) != svc.dedup_key(2, "S", "정보유출", date(2026, 2, 3))
    assert svc.dedup_key(2, "S", "산업재해", date(2026, 2, 3)) != svc.dedup_key(2, "G", "산업재해", date(2026, 2, 3))
    assert svc.dedup_key(1, "S", "산업재해", date(2026, 2, 3)) != svc.dedup_key(2, "S", "산업재해", date(2026, 2, 3))
    # 같은 키의 다른 event_type · 다른 달은 배치 안에서도 따로 남는다
    new_events, _ = svc.merge_candidates(
        [candidate(105), candidate(109, event_type="정보유출"), candidate(110, event_date=date(2026, 3, 2), published_at=date(2026, 3, 2))], [], COMPANY.id
    )
    assert [group.article_ids for group in new_events] == [[105], [109], [110]]


def test_different_fine_amount_or_casualties_keeps_events_apart():
    """분리 유지 예외: fine_amount 가 양쪽 다 있고 다르면 · casualties 가 양쪽 다 0 보다 크고 다르면 별개 사건."""
    assert svc.signals_conflict(signals(fine_amount=100_000_000), signals(fine_amount=200_000_000)) is True
    assert svc.signals_conflict(signals(fine_amount=None), signals(fine_amount=200_000_000)) is False
    assert svc.signals_conflict(signals(casualties=2), signals(casualties=3)) is True
    assert svc.signals_conflict(signals(casualties=0), signals(casualties=3)) is False
    assert svc.signals_conflict(signals(casualties=3), signals(casualties=3)) is False

    # 배치 안: 과징금이 다른 두 제재는 같은 달이라도 둘로 남는다 · 과징금 없는 세 번째 기사는 첫 사건에 붙는다
    fined = [
        candidate(105, category="G", event_type="제재", severity_signals=signals(fine_amount=100_000_000)),
        candidate(109, category="G", event_type="제재", severity_signals=signals(fine_amount=200_000_000), published_at=date(2026, 2, 4)),
        candidate(110, category="G", event_type="제재", severity_signals=signals(), published_at=date(2026, 2, 5)),
    ]
    new_events, merges = svc.merge_candidates(fined, [], COMPANY.id)
    assert merges == []
    assert [group.article_ids for group in new_events] == [[105, 110], [109]]
    assert new_events[0].fields.severity_signals["fine_amount"] == 100_000_000

    # 기존 DB 사건과도 같은 예외: 사상자 수가 다른 같은 달 사고는 새 사건이 된다
    existing = svc.ExistingEvent(
        id=2006, title="(임시) 컨베이어 작업 중 직원 2명 손가락 절단", category="S", event_type="산업재해", event_date=date(2026, 2, 10),
        sources=[106], reported_at=date(2026, 2, 10), prompt_version="fixture-v0", severity_signals=signals(casualties=2),
    )
    new_events, merges = svc.merge_candidates([candidate(111, severity_signals=signals(casualties=3))], [existing], COMPANY.id)
    assert merges == [] and [group.article_ids for group in new_events] == [[111]]
    new_events, merges = svc.merge_candidates([candidate(111, severity_signals=signals(casualties=0))], [existing], COMPANY.id)
    assert new_events == [] and merges[0].article_ids == [111]


def test_merge_updates_source_count_confirmed_date_precision_and_signals_by_rule():
    """병합 규칙: sources 합집합 · source_count · reported_at 최솟값 · confirmed OR(true 쪽 근거) · is_retrospective OR ·
    date_precision day 우선(그 행의 event_date) · severity_signals 필드별 큰 값 · thin_source 재계산. 텍스트 3필드는 안 건드린다."""
    seed = svc.ExistingEvent(
        id=2005, title="(임시) 시화공장 화재로 공장 전체 가동 중단", category="S", event_type="산업재해", event_date=date(2026, 2, 1),
        date_precision="month", sources=[105], reported_at=date(2026, 2, 3), prompt_version="fixture-v0",
        severity_signals=signals(regulator="고용노동부"), confirmed=False, confirmed_basis="없음", is_retrospective=False,
    )
    # casualties 0 → 3 은 "값이 있는 쪽" 채택. (양쪽 다 0 보다 크고 다르면 분리 예외라 여기까지 오지 않는다)
    later = candidate(
        109, published_at=date(2026, 2, 1), title="삼립 공장 화재 부상자 발생", event_date=date(2026, 2, 4), date_precision="day",
        severity_signals=signals(fine_amount=50_000_000, casualties=3, lawsuit=True, regulator="경찰"),
        confirmed=True, confirmed_basis="규제기관 처분", is_retrospective=True,
    )
    filings = [svc.FilingRow(id=9, filed_at=date(2026, 2, 10), title="화재 발생"), svc.FilingRow(id=10, filed_at=date(2026, 3, 30), title="화재 발생"), svc.FilingRow(id=11, filed_at=date(2026, 2, 2), title="주주총회 소집")]

    new_events, merges = svc.merge_candidates([later], [seed], COMPANY.id)
    assert new_events == [] and len(merges) == 1
    assert seed.is_seed
    values = svc.merged_values(merges[0], filings)
    assert values["sources"] == [105, 109] and values["source_count"] == 2 and values["thin_source"] is False
    assert values["reported_at"] == date(2026, 2, 1)
    assert values["confirmed"] is True and values["confirmed_basis"] == "규제기관 처분"
    assert values["is_retrospective"] is True
    assert values["date_precision"] == "day" and values["event_date"] == date(2026, 2, 4)  # day 인 행의 event_date 채택
    assert values["severity_signals"] == {"fine_amount": 50_000_000, "casualties": 3, "lawsuit": True, "is_repeat": False, "regulator": "고용노동부"}
    assert values["filing_ids"] == [9]
    assert set(values) == set(svc.MERGED_COLUMNS)
    assert "title" not in values and "summary" not in values and "evidence_quote" not in values

    # 기존 행이 이미 day 면 event_date 를 바꾸지 않고, confirmed 근거가 있으면 지킨다. month 후보는 아무것도 못 바꾼다
    confirmed_day = svc.ExistingEvent(
        id=1, title="t", category="S", event_type="산업재해", event_date=date(2026, 2, 3), sources=[105], reported_at=date(2026, 2, 3),
        prompt_version="f03-v1", confirmed=True, confirmed_basis="회사 인정",
    )
    month_candidate = candidate(109, event_date=date(2026, 2, 1), date_precision="month", confirmed=True, confirmed_basis="판결", published_at=date(2026, 2, 9))
    _, merges = svc.merge_candidates([month_candidate], [confirmed_day], COMPANY.id)
    values = svc.merged_values(merges[0], [])
    assert values["event_date"] == date(2026, 2, 3) and values["date_precision"] == "day"
    assert values["confirmed"] is True and values["confirmed_basis"] == "회사 인정"
    assert values["reported_at"] == date(2026, 2, 3)

    # 배치 안 병합도 같은 규칙: month 대표 + day 후보 → day 와 그 후보의 날짜
    new_events, _ = svc.merge_candidates(
        [candidate(105, event_date=date(2026, 2, 1), date_precision="month"), candidate(109, event_date=date(2026, 2, 20), published_at=date(2026, 2, 21), confirmed=True, confirmed_basis="회사 공시")],
        [],
        COMPANY.id,
    )
    values = svc.event_values(COMPANY.id, new_events[0], [])
    assert values["event_date"] == date(2026, 2, 20) and values["date_precision"] == "day"
    assert values["confirmed"] is True and values["confirmed_basis"] == "회사 공시" and values["source_count"] == 2


def test_merge_signals_field_rules():
    assert svc.merge_signals(signals(), signals(fine_amount=10, casualties=2, lawsuit=True, regulator="A")) == signals(fine_amount=10, casualties=2, lawsuit=True, regulator="A")
    assert svc.merge_signals(signals(fine_amount=20, casualties=3, regulator="A"), signals(fine_amount=10, casualties=2, regulator="B")) == signals(fine_amount=20, casualties=3, regulator="A")
    assert svc.merge_signals(signals(is_repeat=True), signals(is_repeat=False))["is_repeat"] is True
    assert svc.merge_signals({}, {})["casualties"] == 0  # 빈 값도 EMPTY_SIGNALS 모양으로


def test_already_linked_article_is_not_added_twice():
    existing = svc.ExistingEvent(id=1, title="시화공장 화재", category="S", event_type="산업재해", event_date=date(2026, 2, 3), sources=[105], reported_at=date(2026, 2, 3), prompt_version="f03-v1")
    _, merges = svc.merge_candidates([candidate(105)], [existing], COMPANY.id)
    assert len(merges) == 1 and merges[0].article_ids == []
    assert svc.merged_values(merges[0], [])["sources"] == [105]


# --------------------------------------------------------------------------- quote 실패 → 재생성 1회 → 폐기
def test_quote_failure_regenerates_failed_articles_only_then_discards(tmp_path):
    first = {"judgements": [judgement(105, evidence_quote="화재로 인명 피해 발생"), judgement(109, evidence_quote="화재로 공장 전체 가동이 중단됐다"), not_event(110)]}
    second = {"judgements": [judgement(105, evidence_quote="근로자 3명이 연기를 흡입했다")]}
    client, fake = make_client(tmp_path, [first, second])

    outcome = svc.detect_batch(client, "m", COMPANY, [A1, A2, A3], log=lambda _line: None)

    assert len(fake.calls) == 2 and outcome.regenerated and outcome.attempts == 2
    retry_prompt = fake.calls[1]["user"]
    assert "id=105" in retry_prompt and "id=109" not in retry_prompt and "id=110" not in retry_prompt
    assert "evidence_quote 가 제목·설명에 없" in retry_prompt
    assert sorted(c.article_id for c in outcome.candidates) == [105, 109]
    assert outcome.discarded == [] and outcome.judged == 3


def test_second_failure_is_discarded(tmp_path):
    first = {"judgements": [judgement(105, evidence_quote="없는 문장")]}
    second = {"judgements": [judgement(105, evidence_quote="또 없는 문장")]}
    client, fake = make_client(tmp_path, [first, second])
    outcome = svc.detect_batch(client, "m", COMPANY, [A1], log=lambda _line: None)
    assert len(fake.calls) == 2
    assert outcome.candidates == []
    assert len(outcome.discarded) == 1 and "article 105" in outcome.discarded[0] and "또 없는 문장" in outcome.discarded[0]


def test_missing_required_field_counts_as_failure(tmp_path):
    first = {"judgements": [judgement(105, event_date=None)]}
    second = {"judgements": [judgement(105)]}
    client, fake = make_client(tmp_path, [first, second])
    outcome = svc.detect_batch(client, "m", COMPANY, [A1], log=lambda _line: None)
    assert "필수 필드 누락: event_date" in fake.calls[1]["user"]
    assert [c.article_id for c in outcome.candidates] == [105]


def test_schema_failure_regenerates_whole_batch_once(tmp_path):
    client, fake = make_client(tmp_path, [{"judgements": "x"}, {"judgements": [judgement(105), not_event(110)]}])
    outcome = svc.detect_batch(client, "m", COMPANY, [A1, A3], log=lambda _line: None)
    assert len(fake.calls) == 2 and "형식에 맞지 않았습니다" in fake.calls[1]["user"]
    assert outcome.judged == 2 and len(outcome.candidates) == 1 and outcome.not_event == 1


# --------------------------------------------------------------------------- is_subject false 제외
def test_not_subject_and_not_event_are_excluded(tmp_path):
    client, _ = make_client(tmp_path, [{"judgements": [judgement(111, is_subject=False, via_subsidiary=True, evidence_quote="위생 논란"), not_event(110)]}])
    outcome = svc.detect_batch(client, "m", COMPANY, [A4, A3], log=lambda _line: None)
    assert outcome.candidates == []
    assert outcome.not_subject == 1 and outcome.not_event == 1 and outcome.discarded == []


# --------------------------------------------------------------------------- 택소노미 밖 값 (DB CHECK 없음)
def test_out_of_taxonomy_event_type_is_demoted_to_기타_and_noted(tmp_path):
    client, _ = make_client(
        tmp_path,
        [{"judgements": [judgement(105, event_type="화재사고", sub_tags=["산업안전보건", "화재"], confirmed=True, confirmed_basis="언론 보도")]}],
    )
    outcome = svc.detect_batch(client, "m", COMPANY, [A1], log=lambda _line: None)
    candidate = outcome.candidates[0]
    assert candidate.event_type == "기타"
    assert candidate.sub_tags == ["산업안전보건"]
    assert candidate.confirmed_basis == "없음" and candidate.confirmed is False
    joined = "\n".join(outcome.taxonomy_notes)
    assert "event_type '화재사고' → 기타" in joined
    assert "sub_tags 제외 ['화재']" in joined
    assert "confirmed_basis '언론 보도' → 없음" in joined
    assert "근거가 없어 false" in joined


def test_sanitize_taxonomy_keeps_valid_values_silently():
    event_type, sub_tags, confirmed, basis, notes = svc.sanitize_taxonomy(1, "제재", ["준법·윤리", "준법·윤리"], True, "규제기관 처분")
    assert (event_type, sub_tags, confirmed, basis, notes) == ("제재", ["준법·윤리"], True, "규제기관 처분", [])
    event_type, sub_tags, confirmed, basis, notes = svc.sanitize_taxonomy(1, None, [], False, None)
    assert (event_type, sub_tags, confirmed, basis) == ("기타", [], False, "없음")
    assert len(notes) == 1


# --------------------------------------------------------------------------- 기타 순수 함수
def test_match_filings_window_and_keywords():
    filings = [
        svc.FilingRow(id=1, filed_at=date(2026, 2, 17), title="소송 등의 제기"),
        svc.FilingRow(id=2, filed_at=date(2026, 2, 18), title="소송 등의 제기"),
        svc.FilingRow(id=3, filed_at=date(2026, 2, 3), title="주요사항보고서(유상증자결정)"),
    ]
    assert svc.match_filings(filings, date(2026, 2, 3)) == [1]


def test_batches_and_prompt_shape():
    assert svc.batches_for(0) == 0 and svc.batches_for(15) == 1 and svc.batches_for(16) == 2
    prompt = build_user_prompt("SPC삼립", ["SPC삼립", "삼립"], [A1], failed={105: "이유"})
    assert "회사: SPC삼립 (별칭: SPC삼립, 삼립)" in prompt
    assert "id=105" in prompt and "보도일=2026-02-03" in prompt and A1.description in prompt
    assert "- id=105: 이유" in prompt
    schema = EventBatch.model_json_schema()
    judgement_schema = schema["$defs"]["ArticleJudgement"]
    assert judgement_schema["required"] == list(judgement_schema["properties"])
    assert "enum" not in judgement_schema["properties"]["event_type"]  # 택소노미 검증은 적재 직전 코드가 한다


# --------------------------------------------------------------------------- 사전 필터 (D-01)
def test_prefilter_keeps_only_titles_with_keyword_and_preserves_order():
    keywords = ["화재", "위생", "시화공장"]
    kept, excluded = svc.prefilter_articles([A1, A3, A2, A4], keywords)
    assert [a.id for a in kept] == [105, 109, 111]  # A3(신제품 출시)는 제목에 키워드가 없다
    assert [a.id for a in excluded] == [110]
    # 설명에만 키워드가 있어도 제목 기준이라 걸러진다
    only_description = ArticleInput(id=120, title="SPC삼립 1분기 실적 발표", description="시화공장 화재 여파", published_at=date(2026, 5, 1), press="x")
    assert svc.prefilter_articles([only_description], keywords) == ([], [only_description])
    # 대소문자 무시 · 빈 키워드 무시
    assert svc.title_has_keyword("KT 서버 감염 사실 미신고", ["서버 감염"]) is True
    assert svc.title_has_keyword("kt 랜섬웨어 피해", ["랜섬웨어", ""]) is True
    assert svc.title_has_keyword("KT 신제품 출시", []) is False


def test_prefiltered_articles_are_not_batched_and_keep_pending_status(tmp_path):
    """걸러진 기사는 배치에 안 들어가고, _store_batch 의 status 갱신도 배치에 든 기사에만 미친다."""
    from sqlalchemy.dialects import postgresql

    from esg_watchdog.services.collect.news import keywords_for

    keywords = keywords_for(COMPANY.stock_code)
    kept, excluded = svc.prefilter_articles([A1, A2, A3, A4], keywords)
    assert [a.id for a in kept] == [105, 109, 111]  # 화재(안전보건) · 논란(공통)
    assert [a.id for a in excluded] == [110]

    client, fake = make_client(tmp_path, [{"judgements": [judgement(105), judgement(109, evidence_quote="화재로 공장 전체 가동이 중단됐다"), judgement(111, is_subject=False, evidence_quote="위생 논란")]}])
    outcome = svc.detect_batch(client, "m", COMPANY, kept, log=lambda _line: None)
    assert "id=110" not in fake.calls[0]["user"] and "id=105" in fake.calls[0]["user"]

    class RecordingSession:
        def __init__(self):
            self.statements = []
            self.added = []

        def add(self, obj):
            self.added.append(obj)

        def flush(self):
            for index, obj in enumerate(self.added, start=1):
                if getattr(obj, "id", None) is None:
                    obj.id = 9000 + index

        def execute(self, statement):
            self.statements.append(statement)

    session = RecordingSession()
    stats = svc._new_stats(COMPANY, len(kept), pending=4, prefiltered=len(excluded))
    svc._store_batch(session, COMPANY, kept, outcome, [], [], stats)

    updates = [
        str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
        for stmt in session.statements
        if "UPDATE articles" in str(stmt)
    ]
    assert len(updates) == 1
    assert "105" in updates[0] and "109" in updates[0] and "111" in updates[0]
    assert "110" not in updates[0]  # 걸러진 기사는 pending 그대로
    assert stats["processed"] == 3 and stats["pending"] == 4 and stats["prefiltered"] == 1


# --------------------------------------------------------------------------- --since / --until · 처리 구간
def test_window_bounds_default_is_12_months_and_until_is_inclusive():
    from datetime import datetime

    from esg_watchdog.services.runs import KST

    now = datetime(2026, 9, 4, 12, 0, tzinfo=KST)
    lower, upper = svc.window_bounds(None, None, now)
    assert lower == now - timedelta(days=svc.WINDOW_DAYS) and upper is None

    lower, upper = svc.window_bounds(date(2025, 9, 4), date(2025, 9, 30), now)
    assert lower == datetime(2025, 9, 4, 0, 0, tzinfo=KST)
    assert upper == datetime(2025, 10, 1, 0, 0, tzinfo=KST)  # 9/30 을 포함하는 상한(미만)
    # 경계 판정: 9/30 23:59 은 안에, 10/1 00:00 은 밖에, 9/3 은 밖에
    assert lower <= datetime(2025, 9, 30, 23, 59, tzinfo=KST) < upper
    assert not (lower <= datetime(2025, 10, 1, 0, 0, tzinfo=KST) < upper)
    assert not (lower <= datetime(2025, 9, 3, 23, 59, tzinfo=KST) < upper)

    with pytest.raises(ValueError):
        svc.window_bounds(date(2025, 10, 1), date(2025, 9, 30), now)


def test_articles_stmt_filters_published_at_by_both_bounds():
    """--since/--until 은 SQL 의 published_at 조건으로 들어간다 — 기간 밖 기사는 애초에 읽지 않는다."""
    from datetime import datetime

    from sqlalchemy.dialects import postgresql

    from esg_watchdog.services.runs import KST

    lower, upper = svc.window_bounds(date(2025, 9, 4), date(2025, 9, 30), datetime(2026, 9, 4, tzinfo=KST))
    sql = str(svc._articles_stmt(2, lower, upper).compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "articles.published_at >= '2025-09-04 00:00:00+09:00'" in sql
    assert "articles.published_at < '2025-10-01 00:00:00+09:00'" in sql
    assert "articles.status = 'pending'" in sql and "article_companies.company_id = 2" in sql

    # 기본(상한 없음)은 하한만
    sql = str(svc._articles_stmt(2, lower, None).compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert ">= '2025-09-04 00:00:00+09:00'" in sql and "published_at <" not in sql


def test_coverage_is_min_max_published_at_of_batched_articles():
    assert svc.coverage_of([A2, A4, A1]) == (date(2026, 2, 3), date(2026, 2, 6))
    assert svc.coverage_text(svc.coverage_of([A2, A4, A1])) == "2026-02-03 ~ 2026-02-06"
    assert svc.coverage_of([]) is None and svc.coverage_text(None) == "없음"
    stats = svc._new_stats(COMPANY, 3, pending=3, prefiltered=0, window={"since": "2025-09-04", "until": None}, coverage=(date(2026, 2, 3), date(2026, 2, 6)))
    assert stats["coverage"] == {"from": "2026-02-03", "to": "2026-02-06"} and stats["window"]["since"] == "2025-09-04"


def test_cli_parses_since_and_until_as_dates():
    from esg_watchdog.cli import build_parser

    args = build_parser().parse_args(["detect", "--company", "030200", "--since", "2025-09-04", "--until", "2025-09-30"])
    assert args.since == date(2025, 9, 4) and args.until == date(2025, 9, 30)
    args = build_parser().parse_args(["detect", "--company", "030200"])
    assert args.since is None and args.until is None
    with pytest.raises(SystemExit):
        build_parser().parse_args(["detect", "--company", "030200", "--since", "2025/09/04"])
