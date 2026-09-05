"""services/publish/alerts.py — 가짜 LLM 으로 금지어 → 재생성 → 폐기, 길이 재생성 → 채택+note, confirmed false → '주의' 캡, 미확정 limitation 강제,
등급 임계값 60/45/30 · 발행 하한 30(미달은 대상 제외 + stage_stats.below_threshold), 사유별 재생성 예산(금지어 → 스키마 실패 → 성공이 살아남고
같은 사유 두 번이면 폐기, stage_stats 사유별 분리), 같은 사건 억제(--per-event · 기존 alert 포함 · 억제된 match 는 accepted 유지),
근거 품질 게이트(출처 제목에 기업명·별칭이 하나도 없으면 제외 · --allow-unnamed-source 로 해제) 를
DB 없이 검사한다 (D-08 · D-16 · D-17)."""

from datetime import date, datetime

import pytest

from esg_watchdog.knowledge.banned_terms import load_banned_terms
from esg_watchdog.knowledge.weights import GRADE_THRESHOLDS, MIN_PUBLISH_MATERIALITY
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
def test_grade_thresholds_at_boundaries_29_30_45_60():
    assert GRADE_THRESHOLDS == [("심각", 60), ("경고", 45), ("주의", 30)] and MIN_PUBLISH_MATERIALITY == 30
    assert svc.grade_for(60) == "심각" and svc.grade_for(100) == "심각" and svc.grade_for(61) == "심각"
    assert svc.grade_for(59) == "경고" and svc.grade_for(45) == "경고"
    assert svc.grade_for(44) == "주의" and svc.grade_for(30) == "주의"
    # 하한 미달은 grade_for 만 보면 마지막 등급이지만 발행 대상이 아니다(is_publishable 로 걸러진다)
    assert svc.grade_for(29) == "주의" and svc.grade_for(0) == "주의" and svc.grade_for(None) == "주의"
    assert svc.is_publishable(30) and svc.is_publishable(30.0) and svc.is_publishable(100)
    assert not svc.is_publishable(29) and not svc.is_publishable(29.9) and not svc.is_publishable(0) and not svc.is_publishable(None)


def test_unconfirmed_cap_still_applies_with_new_thresholds():
    assert svc.cap_grade("심각", confirmed=True) == "심각"
    assert svc.cap_grade("심각", confirmed=False) == "주의"
    assert svc.cap_grade("경고", confirmed=False) == "주의"
    assert svc.cap_grade("주의", confirmed=False) == "주의"
    # PublishTarget.grade 는 alert_values 와 같은 계산(임계값 + 캡)
    assert target(materiality=60, confirmed=True).grade == "심각" and target(materiality=60, confirmed=False).grade == "주의"
    assert target(materiality=45, confirmed=True).grade == "경고" and target(materiality=45, confirmed=False).grade == "주의"
    assert target(materiality=30, confirmed=True).grade == "주의" and target(materiality=30, confirmed=False).grade == "주의"


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
    assert svc.alert_values(target(materiality=71), outcome.explanation, datetime(2026, 9, 4, tzinfo=KST))["grade"] == "심각"  # 옛 임계값이면 '경고'
    assert svc.alert_values(target(materiality=50), outcome.explanation, datetime(2026, 9, 4, tzinfo=KST))["grade"] == "경고"
    assert svc.alert_values(target(materiality=35), outcome.explanation, datetime(2026, 9, 4, tzinfo=KST))["grade"] == "주의"


# --------------------------------------------------------------------------- 발행 하한 · 첫 줄 출력 (D-16)
def test_below_threshold_targets_are_excluded_not_discarded_and_first_line_shows_grade_summary(monkeypatch, tmp_path):
    lines: list[str] = []
    saved: list[dict] = []
    finished: dict = {}
    # _load_targets 가 하한(≥30)을 이미 걸러 (대상, 제외 수) 를 돌려준 상태를 흉내낸다 — 제외 19건은 대조군 오뚜기의 약한 '후퇴'
    targets = [
        target(match_id=1, event_id=2001, materiality=60, confirmed=True, context=context(scores={"materiality": 60, "confidence": 65, "components": {}})),
        target(match_id=2, event_id=2002, materiality=45, confirmed=False, context=context(scores={"materiality": 45, "confidence": 65, "components": {}})),
        target(match_id=3, event_id=2003, materiality=30, confirmed=True, context=context(scores={"materiality": 30, "confidence": 65, "components": {}})),
    ]
    monkeypatch.setattr(svc, "_load_targets", lambda stock_code: svc.TargetSet("전체", targets, below_threshold=19))
    monkeypatch.setattr(svc, "start_run", lambda stage: 77)
    monkeypatch.setattr(svc, "finish_run", lambda run_id, status, stats, note=None: finished.update(run_id=run_id, status=status, stats=stats, note=note))
    monkeypatch.setattr(svc, "_insert_alert", lambda values: saved.append(values) or True)
    client, fake = make_client(tmp_path, [explanation(), explanation(), explanation()])

    result = svc.publish_alerts(client=client, model="m", now=datetime(2026, 9, 5, 9, 0, tzinfo=KST), log=lines.append)

    assert lines[0] == (
        "[publish] 전체: 대상 3건(하한 미달 제외 19건 · 기업 미언급 출처만 0건 · 같은 사건 억제 0건) → 등급 심각 1 · 경고 0 · 주의 2 "
        "(accepted · 위반/후퇴/이행지연 · materiality ≥ 30 · alert 없음 → LLM 호출 3회)"
    )
    assert len(fake.calls) == 3 and [values["grade"] for values in saved] == ["심각", "주의", "주의"]  # 45점 미확정 → 캡
    stats = result.stats
    assert stats["targets"] == 3 and stats["below_threshold"] == 19  # 미달은 폐기(discarded)가 아니라 제외
    assert stats["published"] == 3 and stats["discarded"] == 0 and stats["capped"] == 1 and stats["llm_calls"] == 3
    assert stats["grades"] == {"주의": 2, "경고": 0, "심각": 1}
    assert result.status == "success" and finished["run_id"] == 77 and finished["stats"]["below_threshold"] == 19
    # 미리보기 헬퍼 — 대상이 없으면 전부 0
    assert svc.grade_counts([]) == {"주의": 0, "경고": 0, "심각": 0}
    assert svc.targets_line("KT(030200)", [], 5, 2, 1).startswith(
        "[publish] KT(030200): 대상 0건(하한 미달 제외 5건 · 기업 미언급 출처만 1건 · 같은 사건 억제 2건) → 등급 심각 0 · 경고 0 · 주의 0"
    )


# --------------------------------------------------------------------------- 같은 사건 억제 (D-16)
def same_event_targets() -> list[svc.PublishTarget]:
    """event 2005 에 매칭 3건(공약만 다름) + event 2006 에 1건. 프롬프트가 달라야 캐시가 겹치지 않으므로 scores 를 다르게 둔다."""
    return [
        target(match_id=3011, event_id=2005, materiality=71, confidence=70, context=context(scores={"materiality": 71, "confidence": 70, "components": {}})),
        target(match_id=3012, event_id=2005, materiality=65, confidence=80, context=context(scores={"materiality": 65, "confidence": 80, "components": {}})),
        target(match_id=3013, event_id=2005, materiality=71, confidence=60, context=context(scores={"materiality": 71, "confidence": 60, "components": {}})),
        target(match_id=3014, event_id=2006, materiality=50, confidence=50, context=context(scores={"materiality": 50, "confidence": 50, "components": {}})),
    ]


def test_suppress_same_event_keeps_by_materiality_then_confidence_then_match_id():
    assert svc.DEFAULT_PER_EVENT == 1
    kept, suppressed = svc.suppress_same_event(same_event_targets(), {}, per_event=1)
    assert [item.match_id for item in kept] == [3011, 3014]  # 71/70 이 71/60 · 65/80 보다 앞
    assert [item.match_id for item in suppressed] == [3013, 3012]  # rank 순: 71/60 → 65/80
    kept, suppressed = svc.suppress_same_event(same_event_targets(), {}, per_event=2)
    assert [item.match_id for item in kept] == [3011, 3013, 3014] and [item.match_id for item in suppressed] == [3012]
    kept, _ = svc.suppress_same_event(same_event_targets(), {}, per_event=3)
    assert len(kept) == 4
    # materiality · confidence 가 같으면 match_id 오름차순
    tie = [target(match_id=9, event_id=1, materiality=50, confidence=50), target(match_id=8, event_id=1, materiality=50, confidence=50)]
    kept, suppressed = svc.suppress_same_event(tie, {}, per_event=1)
    assert kept[0].match_id == 8 and suppressed[0].match_id == 9


def test_suppress_same_event_counts_existing_alerts_in_db():
    # event 2005 에 이미 alert 1건 → per_event 1 이면 세 건 모두 억제, per_event 2 면 한 건만 추가
    kept, suppressed = svc.suppress_same_event(same_event_targets(), {2005: 1}, per_event=1)
    assert [item.match_id for item in kept] == [3014] and len(suppressed) == 3
    kept, suppressed = svc.suppress_same_event(same_event_targets(), {2005: 1}, per_event=2)
    assert [item.match_id for item in kept] == [3011, 3014] and len(suppressed) == 2
    # 기존 alert 가 상한을 이미 넘어도 터지지 않는다
    kept, suppressed = svc.suppress_same_event(same_event_targets(), {2005: 5, 2006: 1}, per_event=1)
    assert kept == [] and len(suppressed) == 4
    lines = svc.suppression_lines(kept, suppressed, per_event=1)
    assert lines[0].startswith("[publish] 같은 사건 억제(--per-event 1): event 2005 → 남김 기존 alert · 억제 3011(71) · 3013(71) · 3012(65)")


def publish_with(monkeypatch, tmp_path, *, targets, existing, per_event, responses, allow_unnamed_source=False):
    lines: list[str] = []
    saved: list[dict] = []
    finished: dict = {}
    monkeypatch.setattr(svc, "_load_targets", lambda stock_code: svc.TargetSet("SPC삼립(005610)", targets, existing_alerts=existing))
    monkeypatch.setattr(svc, "start_run", lambda stage: 79)
    monkeypatch.setattr(svc, "finish_run", lambda run_id, status, stats, note=None: finished.update(stats=stats, note=note))
    monkeypatch.setattr(svc, "_insert_alert", lambda values: saved.append(values) or True)
    client, fake = make_client(tmp_path, responses)
    result = svc.publish_alerts(
        stock_code="005610", client=client, model="m", now=datetime(2026, 9, 5, 9, 0, tzinfo=KST),
        per_event=per_event, allow_unnamed_source=allow_unnamed_source, log=lines.append,
    )
    return result, lines, saved, fake, finished


def test_publish_suppresses_same_event_and_reports_it(monkeypatch, tmp_path):
    result, lines, saved, fake, finished = publish_with(
        monkeypatch, tmp_path / "one", targets=same_event_targets(), existing={}, per_event=1, responses=[explanation(), explanation()]
    )
    assert lines[0].startswith(
        "[publish] SPC삼립(005610): 대상 2건(하한 미달 제외 0건 · 기업 미언급 출처만 0건 · 같은 사건 억제 2건) → 등급 심각 1 · 경고 1 · 주의 0"
    )
    assert lines[1] == "[publish] 같은 사건 억제(--per-event 1): event 2005 → 남김 3011(71) · 억제 3013(71) · 3012(65)"
    assert [values["match_id"] for values in saved] == [3011, 3014] and len(fake.calls) == 2  # 억제된 건은 LLM 도 부르지 않는다
    stats = result.stats
    assert stats["targets"] == 2 and stats["suppressed_same_event"] == 2 and stats["per_event"] == 1 and stats["published"] == 2
    assert finished["stats"]["suppressed_same_event"] == 2 and result.status == "success"

    # --per-event 2 → 같은 사건에 두 건
    result, lines, saved, fake, _ = publish_with(
        monkeypatch, tmp_path / "two", targets=same_event_targets(), existing={}, per_event=2, responses=[explanation()] * 3
    )
    assert [values["match_id"] for values in saved] == [3011, 3013, 3014] and result.stats["suppressed_same_event"] == 1
    assert "같은 사건 억제 1건" in lines[0]

    # 이미 발행된 alert 가 있으면 추가 발행 억제 (재실행 시 중복 증식 방지)
    result, lines, saved, fake, _ = publish_with(
        monkeypatch, tmp_path / "existing", targets=same_event_targets()[:3], existing={2005: 1}, per_event=1, responses=[]
    )
    assert saved == [] and len(fake.calls) == 0 and result.stats["suppressed_same_event"] == 3 and result.stats["targets"] == 0
    assert result.status == "success" and "대상 0건(하한 미달 제외 0건 · 기업 미언급 출처만 0건 · 같은 사건 억제 3건)" in lines[0]


def test_publish_rejects_per_event_below_one(monkeypatch, tmp_path):
    client, _ = make_client(tmp_path, [])
    with pytest.raises(ValueError, match="--per-event"):
        svc.publish_alerts(client=client, model="m", per_event=0, log=lambda _line: None)


def test_cli_publish_per_event_argument():
    from esg_watchdog import cli

    args = cli.build_parser().parse_args(["publish"])
    assert args.per_event == svc.DEFAULT_PER_EVENT == 1 and args.company is None
    assert cli.build_parser().parse_args(["publish", "--company", "005610", "--per-event", "2"]).per_event == 2
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["publish", "--per-event", "0"])


# --------------------------------------------------------------------------- 근거 품질 게이트 (D-08 · D-17, 실측 match 3219)
KT_TERMS = ["KT", "케이티"]


def test_company_terms_and_has_named_source_case_insensitive():
    assert svc.company_terms("KT", ["KT", "케이티", "", None]) == ["KT", "케이티"]
    assert svc.company_terms("SPC삼립", None) == ["SPC삼립"] and svc.company_terms(None, []) == []
    totals = ["1분기 담합 과징금 6891억원 총계", "설탕 담합 CJ제일제당·삼양사 제재"]
    assert not svc.has_named_source(totals, KT_TERMS)  # 제목에 KT 가 하나도 없다 — 본문 업종 나열로 스친 사건
    assert svc.has_named_source([*totals, "KT 과징금 385억원"], KT_TERMS)  # 하나라도 있으면 통과
    assert svc.has_named_source(["kt 침해사고 일지"], KT_TERMS) and svc.has_named_source(["케이티 조사 착수"], KT_TERMS)
    assert svc.has_named_source(["otoki 라면 가격"], svc.company_terms("오뚜기", ["오뚜기", "OTOKI"]))
    assert not svc.has_named_source([], KT_TERMS) and not svc.has_named_source([None, ""], KT_TERMS)  # 출처가 없으면 통과 못 한다
    assert not svc.has_named_source(["KT&G 담배"], []) # 기업명이 비면 아무것도 매칭하지 않는다


def gate_targets() -> list[svc.PublishTarget]:
    return [
        target(match_id=3219, event_id=2101, materiality=71, named_source=False, source_count=29, context=context(scores={"materiality": 71, "confidence": 70, "components": {}})),
        target(match_id=3003, event_id=2003, materiality=88, named_source=True, source_count=2, context=context(scores={"materiality": 88, "confidence": 84, "components": {}})),
    ]


def test_apply_source_gate_excludes_unnamed_unless_allowed():
    kept, gated = svc.apply_source_gate(gate_targets(), allow_unnamed_source=False)
    assert [item.match_id for item in kept] == [3003] and [item.match_id for item in gated] == [3219]
    assert svc.gate_lines(gated) == [
        "[publish] 기업 미언급 출처만(--allow-unnamed-source 로 해제): event 2101 → match 3219 (출처 29건, 제목에 SPC삼립 없음)"
    ]
    kept, gated = svc.apply_source_gate(gate_targets(), allow_unnamed_source=True)
    assert len(kept) == 2 and gated == [] and svc.gate_lines(gated) == []
    assert svc.PublishTarget(match_id=1, company_id=1, confirmed=True, materiality=50, context=context()).named_source is True  # 기본값


def test_publish_excludes_unnamed_source_event_not_discarded(monkeypatch, tmp_path):
    result, lines, saved, fake, finished = publish_with(
        monkeypatch, tmp_path / "gate", targets=gate_targets(), existing={}, per_event=1, responses=[explanation()]
    )
    assert lines[0].startswith("[publish] SPC삼립(005610): 대상 1건(하한 미달 제외 0건 · 기업 미언급 출처만 1건 · 같은 사건 억제 0건) → 등급 심각 1")
    assert lines[1].startswith("[publish] 기업 미언급 출처만(--allow-unnamed-source 로 해제): event 2101 → match 3219 (출처 29건")
    assert [values["match_id"] for values in saved] == [3003] and len(fake.calls) == 1  # 제외 건은 LLM 도 부르지 않는다
    stats = result.stats
    assert stats["targets"] == 1 and stats["no_named_source"] == 1 and stats["allow_unnamed_source"] is False
    assert stats["discarded"] == 0 and stats["published"] == 1 and result.status == "success"  # 폐기가 아니라 제외
    assert finished["stats"]["no_named_source"] == 1

    # --allow-unnamed-source → 게이트 해제, 두 건 다 발행
    result, lines, saved, fake, _ = publish_with(
        monkeypatch, tmp_path / "allow", targets=gate_targets(), existing={}, per_event=1, responses=[explanation(), explanation()],
        allow_unnamed_source=True,
    )
    assert sorted(values["match_id"] for values in saved) == [3003, 3219] and len(fake.calls) == 2
    assert result.stats["no_named_source"] == 0 and result.stats["allow_unnamed_source"] is True
    assert "기업 미언급 출처만 0건" in lines[0] and not any("기업 미언급 출처만(" in line for line in lines)


def test_gate_runs_before_same_event_suppression(monkeypatch, tmp_path):
    # 같은 사건의 상위 매칭이 게이트에 걸리면 그 사건 전체가 걸린다(sources 가 같다) — 억제 자리도 차지하지 않는다
    targets = [
        target(match_id=1, event_id=9, materiality=90, named_source=False, source_count=3, context=context(scores={"materiality": 90, "confidence": 1, "components": {}})),
        target(match_id=2, event_id=9, materiality=80, named_source=False, source_count=3, context=context(scores={"materiality": 80, "confidence": 1, "components": {}})),
        target(match_id=3, event_id=8, materiality=40, named_source=True, source_count=1, context=context(scores={"materiality": 40, "confidence": 1, "components": {}})),
    ]
    result, lines, saved, _, _ = publish_with(monkeypatch, tmp_path, targets=targets, existing={}, per_event=1, responses=[explanation()])
    assert [values["match_id"] for values in saved] == [3]
    assert result.stats["no_named_source"] == 2 and result.stats["suppressed_same_event"] == 0
    assert "기업 미언급 출처만 2건 · 같은 사건 억제 0건" in lines[0]


def test_cli_publish_allow_unnamed_source_flag():
    from esg_watchdog import cli

    assert cli.build_parser().parse_args(["publish"]).allow_unnamed_source is False
    assert cli.build_parser().parse_args(["publish", "--allow-unnamed-source", "--per-event", "2"]).allow_unnamed_source is True


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
    assert outcome.explanation is None and "(스키마 실패) 재발" in outcome.discarded
    assert outcome.regenerations == {"banned": 0, "schema": 1, "length": 0}


# --------------------------------------------------------------------------- 사유별 재생성 예산 (D-17, 실측 run=42 match 3084)
def without_headline(**overrides) -> dict:
    """실측에서 재생성 응답이 headline 을 빠뜨린 경우 — 스키마 실패."""
    response = explanation(**overrides)
    del response["headline"]
    return response


def test_banned_then_schema_failure_then_success_survives(tmp_path):
    banned = explanation(fallback="반드시 공약 위반이라고 볼 수는 없습니다.")
    client, fake = make_client(tmp_path, [banned, without_headline(), explanation()])
    outcome = svc.explain_target(client, "m", target(), log=lambda _line: None)
    assert len(fake.calls) == 3 and outcome.attempts == 3
    assert outcome.explanation is not None and outcome.discarded is None
    assert outcome.regenerations == {"banned": 1, "schema": 1, "length": 0} and outcome.regenerated
    banned_hint = load_banned_terms()["policy"]["regeneration_hint"].format(hit_terms="반드시")
    assert banned_hint in fake.calls[1]["user"] and "형식에 맞지 않았습니다" not in fake.calls[1]["user"]
    # 3회차: 스키마 오류 + 금지어 힌트 유지(같은 표현이 되살아나지 않게)
    assert banned_hint in fake.calls[2]["user"] and "형식에 맞지 않았습니다" in fake.calls[2]["user"]


def test_banned_twice_is_discarded_even_with_schema_failure_between(tmp_path):
    banned = explanation(fallback="반드시 공약 위반이라고 볼 수는 없습니다.")
    client, fake = make_client(tmp_path, [banned, without_headline(), explanation(limitation="투자의견 매수입니다.")])
    outcome = svc.explain_target(client, "m", target(), log=lambda _line: None)
    assert len(fake.calls) == 3 and outcome.explanation is None
    assert outcome.discarded.startswith("match 3005: 금지어 재발") and outcome.hits == {"limitation": ["매수", "투자의견"]}
    assert outcome.regenerations == {"banned": 1, "schema": 1, "length": 0}


def test_total_regeneration_cap_is_two(tmp_path):
    assert svc.MAX_REGENERATIONS == 2
    short = explanation(explanation="짧다.\n\n둘.\n\n셋.\n\n넷.")
    # 스키마 → 길이 → 금지어: 세 사유가 다 다르지만 합계 2회를 넘기므로 세 번째 문제(금지어)는 폐기
    client, fake = make_client(tmp_path / "cap-banned", [{"headline": 1}, short, explanation(headline="지금 매수")])
    outcome = svc.explain_target(client, "m", target(), log=lambda _line: None)
    assert len(fake.calls) == 3 and outcome.explanation is None
    assert "재생성 한도 2회 소진" in outcome.discarded and outcome.regenerations == {"banned": 0, "schema": 1, "length": 1}
    # 스키마 → 금지어 → 길이: 길이는 폐기 사유가 아니라 채택 + note
    client, fake = make_client(tmp_path / "cap-length", [{"headline": 1}, explanation(headline="지금 매수"), short])
    outcome = svc.explain_target(client, "m", target(), log=lambda _line: None)
    assert len(fake.calls) == 3 and outcome.explanation is not None
    assert any("범위 밖이지만 채택" in note for note in outcome.notes)
    assert outcome.regenerations == {"banned": 1, "schema": 1, "length": 0}
    # 스키마 두 번은 폐기 (같은 사유 재발)
    client, fake = make_client(tmp_path / "schema-twice", [{"headline": 1}, without_headline(), explanation()])
    outcome = svc.explain_target(client, "m", target(), log=lambda _line: None)
    assert len(fake.calls) == 2 and outcome.explanation is None and "(스키마 실패) 재발" in outcome.discarded


def test_stage_stats_split_regenerations_by_reason(monkeypatch, tmp_path):
    finished: dict = {}
    targets = [
        target(match_id=1, event_id=2001, materiality=60, context=context(scores={"materiality": 60, "confidence": 65, "components": {}})),
        target(match_id=2, event_id=2002, materiality=45, context=context(scores={"materiality": 45, "confidence": 65, "components": {}})),
    ]
    monkeypatch.setattr(svc, "_load_targets", lambda stock_code: svc.TargetSet("전체", targets))
    monkeypatch.setattr(svc, "start_run", lambda stage: 78)
    monkeypatch.setattr(svc, "finish_run", lambda run_id, status, stats, note=None: finished.update(stats=stats, note=note))
    monkeypatch.setattr(svc, "_insert_alert", lambda values: True)
    # match 1: 금지어 → 스키마 실패 → 성공 (3회) · match 2: 한 번에 성공 (1회)
    client, fake = make_client(
        tmp_path, [explanation(fallback="반드시 그렇다고 볼 수는 없습니다."), without_headline(), explanation(), explanation()]
    )
    result = svc.publish_alerts(client=client, model="m", now=datetime(2026, 9, 5, 9, 0, tzinfo=KST), log=lambda _line: None)
    stats = result.stats
    assert len(fake.calls) == 4 and stats["attempts"] == 4 and stats["llm_calls"] == 4
    assert stats["published"] == 2 and stats["discarded"] == 0
    assert stats["regenerated"] == 1  # 재생성이 있었던 경보 수
    assert (stats["regenerated_banned"], stats["regenerated_schema"], stats["regenerated_length"]) == (1, 1, 0)
    assert finished["stats"]["regenerated_schema"] == 1 and result.status == "success"


# --------------------------------------------------------------------------- 프롬프트
def test_prompt_contains_inputs_and_rules():
    prompt = build_user_prompt(context(is_retrospective=True))
    assert "기업명: SPC삼립" in prompt and "26쪽" in prompt and "발간일 2025-08-14" in prompt
    assert "보도 언론사: 비즈중앙, 참여와혁신" in prompt and "사상자: 3명" in prompt and "재무적 중대성 71" in prompt
    assert "소급 건: 예" in prompt and "관계: 위반" in prompt and "공시 이후 6개월" in prompt
    month = build_user_prompt(context(event=AlertEvent(title="t", summary="s", evidence_quote="q", event_date=date(2026, 6, 1), date_precision="month", confirmed=False, confirmed_basis=None)))
    assert "2026년 06월" in month and "미확정(조사·의혹 단계)" in month
    assert PROMPT_VERSION == "f06-v2"  # 캐시 키에 들어간다 — 프롬프트를 고치면 올린다
    from esg_watchdog.prompts.alert_explain import SYSTEM_PROMPT

    for category in ("매매권유", "가격전망", "밸류에이션", "투자자문", "확정단정", "사법단정"):
        assert category in SYSTEM_PROMPT
    assert "4단락" in SYSTEM_PROMPT and "400~800자" in SYSTEM_PROMPT and "기업명은 카드가 따로 표시" in SYSTEM_PROMPT
    # f06-v2: 네 필드 항상 포함 · fallback 단정어 부정 구문 금지. 지시문 자체에는 '반드시' 를 출력 금지 예시 안에서만 쓴다
    assert "네 필드를 항상 모두 포함한다" in SYSTEM_PROMPT and "다른 필드를 생략하지 마라" in SYSTEM_PROMPT
    assert "단정어 부정 구문을 쓰지 마라" in SYSTEM_PROMPT and "…로 읽힐 여지도 있습니다" in SYSTEM_PROMPT
    assert '"반드시 …라고 볼 수는 없습니다" 같은' in SYSTEM_PROMPT
    assert "반드시 넣는다" not in SYSTEM_PROMPT and "반드시 지킨다" not in SYSTEM_PROMPT  # 지시문의 '반드시' 는 뺐다(금지 예시 목록에만 남는다)
    schema = AlertExplanation.model_json_schema()
    assert schema["required"] == ["headline", "explanation", "limitation", "fallback"]
