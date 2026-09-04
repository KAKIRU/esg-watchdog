"""services/publish/alerts.py — 가짜 LLM 으로 금지어 → 재생성 → 폐기, 길이 재생성 → 채택+note, confirmed false → '주의' 캡, 미확정 limitation 강제를 DB 없이 검사한다 (D-08 · D-16 · D-17)."""

from datetime import date, datetime

from esg_watchdog.knowledge.banned_terms import load_banned_terms
from esg_watchdog.llm.client import LLMClient
from esg_watchdog.llm.providers.fake_provider import FakeProvider
from esg_watchdog.prompts.alert_explain import (
    PROMPT_VERSION,
    AlertCommitment,
    AlertContext,
    AlertEvent,
    AlertExplanation,
    build_user_prompt,
)
from esg_watchdog.services.publish import alerts as svc
from esg_watchdog.services.runs import KST

PARAGRAPH = "SPC삼립은 2025년 8월 14일 발간한 지속가능경영보고서 26쪽에서 중대재해 재발 방지를 위해 전 사업장 설비 안전장치를 정기 점검한다고 밝혔습니다. 해당 문장은 산업안전보건 항목에 실려 있습니다."
GOOD_EXPLANATION = f"{PARAGRAPH}\n\n이후 2026년 2월 3일 시화공장에서 화재가 발생해 공장 전체 가동이 중단됐고 근로자 3명이 연기를 흡입했다고 비즈중앙 등이 보도되었습니다. 고용노동부가 조사에 착수했다고도 보도되었습니다.\n\n공약이 명시한 전 사업장 정기 점검과, 같은 공장에서 화재로 가동이 중단된 사실은 서로 어긋납니다. 입력된 심각도 신호는 사상자 3명과 반복 사고 표시이며, 재무적 중대성 점수는 71점입니다. 과징금 부과 여부는 확인되지 않았습니다.\n\n이 글은 공개된 보고서 문장과 보도 내용만을 근거로 합니다. 회사의 반론과 조사 결과, 이후 개선 조치는 반영되어 있지 않으므로 화면의 원문 링크에서 보고서와 기사를 직접 확인하시기 바랍니다."


def context(**overrides) -> AlertContext:
    base = {
        "company_name": "SPC삼립",
        "commitment": AlertCommitment(
            commitment_text="중대재해 재발 방지를 위해 전 사업장 설비 안전장치를 정기 점검한다.",
            document_title="SPC삼립 지속가능경영보고서 2025", fiscal_year=2024, page=26, filed_at=date(2025, 8, 14),
        ),
        "event": AlertEvent(
            title="시화공장 화재로 공장 전체 가동 중단", summary="시화공장에서 화재가 발생해 공장 전체 가동이 중단됐다고 보도되었습니다.",
            evidence_quote="화재로 공장 전체 가동이 중단됐다", event_date=date(2026, 2, 3), date_precision="day", confirmed=True,
            confirmed_basis="회사 인정", severity_signals={"fine_amount": None, "casualties": 3, "lawsuit": False, "is_repeat": True, "regulator": "고용노동부"},
            presses=["비즈중앙", "참여와혁신"],
        ),
        "relation": "위반",
        "gap_months": 6,
        "is_retrospective": False,
        "scores": {"materiality": 71, "confidence": 65, "components": {}},
    }
    base.update(overrides)
    return AlertContext(**base)


def target(**overrides) -> svc.PublishTarget:
    base = {"match_id": 3005, "company_id": 2, "confirmed": True, "materiality": 71, "context": context()}
    base.update(overrides)
    return svc.PublishTarget(**base)


def explanation(**overrides) -> dict:
    base = {
        "headline": "안전점검 공약 이후 같은 공장에서 화재로 가동 중단",
        "explanation": GOOD_EXPLANATION,
        "limitation": "사고 원인 조사 결과가 확정되기 전의 보도를 근거로 합니다.",
        "fallback": "점검 주기와 사고 시점의 간격에 따라 다르게 읽힐 여지가 있습니다.",
    }
    base.update(overrides)
    return base


def make_client(tmp_path, responses):
    fake = FakeProvider(responses)
    return LLMClient(provider=fake, cache_dir=tmp_path, sleep=lambda _seconds: None), fake


# --------------------------------------------------------------------------- 등급 (D-16 · D-08 ②)
def test_grade_thresholds_and_unconfirmed_cap():
    assert svc.grade_for(80) == "심각" and svc.grade_for(100) == "심각"
    assert svc.grade_for(79) == "경고" and svc.grade_for(70) == "경고"
    assert svc.grade_for(69) == "주의" and svc.grade_for(0) == "주의" and svc.grade_for(None) == "주의"
    assert svc.cap_grade("심각", confirmed=True) == "심각"
    assert svc.cap_grade("심각", confirmed=False) == "주의"
    assert svc.cap_grade("경고", confirmed=False) == "주의"
    assert svc.cap_grade("주의", confirmed=False) == "주의"


def test_unconfirmed_event_is_capped_to_caution_and_limitation_gets_sentence(tmp_path):
    unconfirmed = target(confirmed=False, materiality=88)
    client, _ = make_client(tmp_path, [explanation(limitation="단일 매체 보도를 근거로 합니다.")])
    outcome = svc.explain_target(client, "m", unconfirmed, log=lambda _line: None)
    assert outcome.explanation is not None
    assert svc.UNCONFIRMED_SENTENCE in outcome.explanation.limitation
    assert any("D-08 ④" in note for note in outcome.notes)
    values = svc.alert_values(unconfirmed, outcome.explanation, datetime(2026, 9, 4, 9, 0, tzinfo=KST))
    assert values["grade"] == "주의"  # 88점이지만 미확정 → 캡
    assert values["status"] == "published" and values["prompt_version"] == PROMPT_VERSION
    assert values["match_id"] == 3005 and values["company_id"] == 2
    assert values["published_at"].tzinfo is KST
    # 이미 취지 문장이 있으면 덧붙이지 않는다
    assert svc.ensure_limitation("조사 단계로 확정되지 않았습니다.", False) == "조사 단계로 확정되지 않았습니다."
    assert svc.ensure_limitation("확정 처분입니다.", True) == "확정 처분입니다."


def test_confirmed_event_keeps_grade(tmp_path):
    client, _ = make_client(tmp_path, [explanation(fallback=None)])
    outcome = svc.explain_target(client, "m", target(materiality=88), log=lambda _line: None)
    values = svc.alert_values(target(materiality=88), outcome.explanation, datetime(2026, 9, 4, tzinfo=KST))
    assert values["grade"] == "심각" and values["fallback"] is None
    assert svc.alert_values(target(materiality=71), outcome.explanation, datetime(2026, 9, 4, tzinfo=KST))["grade"] == "경고"


# --------------------------------------------------------------------------- 금지어 → 재생성 → 폐기 (D-17)
def test_banned_term_regenerates_with_hint_then_accepts(tmp_path):
    client, fake = make_client(tmp_path, [explanation(headline="지금 매수하시기 바랍니다"), explanation()])
    outcome = svc.explain_target(client, "m", target(), log=lambda _line: None)
    assert len(fake.calls) == 2 and outcome.regenerated and outcome.attempts == 2
    hint = load_banned_terms()["policy"]["regeneration_hint"].format(hit_terms="매수")
    assert hint in fake.calls[1]["user"]
    assert outcome.explanation is not None and outcome.discarded is None
    assert outcome.explanation.headline == explanation()["headline"]


def test_banned_term_twice_is_discarded_with_hits(tmp_path):
    client, fake = make_client(
        tmp_path,
        [explanation(explanation=GOOD_EXPLANATION + "\n\n목표주가 5만원이 기대됩니다."), explanation(limitation="투자의견 매수입니다.")],
    )
    outcome = svc.explain_target(client, "m", target(), log=lambda _line: None)
    assert len(fake.calls) == 2
    assert outcome.explanation is None
    assert outcome.hits == {"limitation": ["매수", "투자의견"]}
    assert outcome.discarded.startswith("match 3005: 금지어 재발")
    assert "투자의견" in outcome.discarded


def test_excluded_fields_are_not_scanned_but_all_four_applies_to_are(tmp_path):
    # rationale · evidence_quote 는 스캔 대상이 아니다 — 프롬프트 입력에 금지어가 있어도 출력만 본다
    ctx = context(event=AlertEvent(title="유망한 종목 논란", summary="사기를 저질렀다는 의혹이 보도되었습니다.", evidence_quote="투자의견 매수", event_date=date(2026, 2, 3), date_precision="day", confirmed=False, confirmed_basis=None))
    client, fake = make_client(tmp_path, [explanation(fallback="저평가 구간이라는 해석도 있습니다."), explanation()])
    outcome = svc.explain_target(client, "m", target(context=ctx, confirmed=False), log=lambda _line: None)
    assert len(fake.calls) == 2  # fallback 의 '저평가' 로 재생성
    assert outcome.explanation is not None


# --------------------------------------------------------------------------- 길이 400~800자
def test_length_out_of_range_regenerates_once_then_accepts_with_note(tmp_path):
    short = explanation(explanation="너무 짧은 설명입니다.\n\n둘째.\n\n셋째.\n\n넷째.")
    client, fake = make_client(tmp_path, [short, short])
    outcome = svc.explain_target(client, "m", target(), log=lambda _line: None)
    assert len(fake.calls) == 2 and outcome.regenerated
    assert "범위를 벗어나" in fake.calls[1]["user"] and "더 길게" in fake.calls[1]["user"]
    assert outcome.explanation is not None and outcome.discarded is None  # 길이는 폐기 사유가 아니다
    assert any("범위 밖이지만 채택" in note for note in outcome.notes)

    long_text = "가" * 900
    client, fake = make_client(tmp_path / "long", [explanation(explanation=long_text), explanation()])
    outcome = svc.explain_target(client, "m", target(), log=lambda _line: None)
    assert "더 짧게" in fake.calls[1]["user"] and outcome.notes == []
    assert svc.explanation_length(GOOD_EXPLANATION) == len(" ".join(GOOD_EXPLANATION.split()))
    assert svc.length_problem(GOOD_EXPLANATION) is None and svc.length_problem("짧다") == 2


def test_banned_and_length_problems_share_one_regeneration(tmp_path):
    client, fake = make_client(tmp_path, [explanation(headline="지금 매수", explanation="짧다.\n\n둘.\n\n셋.\n\n넷."), explanation()])
    outcome = svc.explain_target(client, "m", target(), log=lambda _line: None)
    assert len(fake.calls) == 2
    retry = fake.calls[1]["user"]
    assert "매수" in retry and "범위를 벗어나" in retry
    assert outcome.explanation is not None and outcome.attempts == 2


def test_schema_failure_regenerates_once_then_discards(tmp_path):
    client, fake = make_client(tmp_path, [{"headline": 1}, explanation()])
    outcome = svc.explain_target(client, "m", target(), log=lambda _line: None)
    assert len(fake.calls) == 2 and "형식에 맞지 않았습니다" in fake.calls[1]["user"]
    assert outcome.explanation is not None
    broken, _ = make_client(tmp_path / "b", [{"headline": 1}, {"explanation": None}])
    outcome = svc.explain_target(broken, "m", target(), log=lambda _line: None)
    assert outcome.explanation is None and "(스키마 실패)" in outcome.discarded


# --------------------------------------------------------------------------- 프롬프트
def test_prompt_contains_inputs_and_rules():
    prompt = build_user_prompt(context(is_retrospective=True))
    assert "기업명: SPC삼립" in prompt and "26쪽" in prompt and "발간일 2025-08-14" in prompt
    assert "보도 언론사: 비즈중앙, 참여와혁신" in prompt and "사상자: 3명" in prompt and "재무적 중대성 71" in prompt
    assert "소급 건: 예" in prompt and "관계: 위반" in prompt and "공시 이후 6개월" in prompt
    month = build_user_prompt(context(event=AlertEvent(title="t", summary="s", evidence_quote="q", event_date=date(2026, 6, 1), date_precision="month", confirmed=False, confirmed_basis=None)))
    assert "2026년 06월" in month and "미확정(조사·의혹 단계)" in month
    assert PROMPT_VERSION == "f06-v1"
    from esg_watchdog.prompts.alert_explain import SYSTEM_PROMPT

    for category in ("매매권유", "가격전망", "밸류에이션", "투자자문", "확정단정", "사법단정"):
        assert category in SYSTEM_PROMPT
    assert "4단락" in SYSTEM_PROMPT and "400~800자" in SYSTEM_PROMPT and "기업명은 카드가 따로 표시" in SYSTEM_PROMPT
    schema = AlertExplanation.model_json_schema()
    assert schema["required"] == ["headline", "explanation", "limitation", "fallback"]
