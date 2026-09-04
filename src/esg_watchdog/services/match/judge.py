"""F-04 관계 판정 — 후보마다 LLM(llm_model_judge) → 후처리 강제 → matches upsert (D-10 · D-11 · D-12).

- 후보는 candidates.load_candidates(SQL). 후보마다 RelationJudgement 를 받아 다음을 강제한다:
  (a) target_year > 올해 이고 relation == '위반' → '이행지연' 으로 바꾸고 rationale 끝에 PENDING_NOTE 를 붙인다.
  (b) is_retrospective 인데 rationale 에 "소급 확인" 이 없으면 RETRO_SENTENCE 를 붙인다.
  (c) quote_in(commitment_quote, commitment_text) 와 quote_in(event_quote, event.evidence_quote or event.summary) 둘 다 통과해야 한다.
      실패(또는 스키마 실패 ValueError)면 힌트를 붙여 1회 재생성 → 재실패는 폐기하고 pipeline_runs.note 에 남긴다.
- 통과한 것은 Match(status 'accepted', scores None, gap_months, prompt_version) 로 upsert(commitment_id, event_id UNIQUE).
  '무관' 도 저장한다 — 다음 실행에서 후보 SQL 이 제외해 재판정하지 않는다. 점수는 F-05(score) 가 채운다.
- pipeline_runs(stage='match', trigger='manual'). 후보 단위 예외 격리, 후보마다 commit.
- DB·settings import 는 함수 안에서만 — .env 없이 import 가능.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

from esg_watchdog.knowledge.taxonomy import RELATIONS
from esg_watchdog.llm.client import LLMClient
from esg_watchdog.llm.quotes import quote_in
from esg_watchdog.prompts.relation_judge import (
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    RelationJudgement,
    build_user_prompt,
)
from esg_watchdog.services.match.candidates import Candidate, load_candidates
from esg_watchdog.services.runs import (
    decide_status,
    failure_note,
    finish_run,
    now_kst,
    start_run,
)

STAGE = "match"
MATCH_STATUS = "accepted"
PENDING_NOTE = "(목표연도 미도래로 '위반' 대신 '이행지연'으로 분류)"
RETRO_MARK = "소급 확인"
RETRO_SENTENCE = "이 사건은 공시 이후 보도로 소급 확인된 건입니다."

Log = Callable[[str], None]


# --------------------------------------------------------------------------- 순수 함수
@dataclass
class JudgeOutcome:
    judgement: RelationJudgement | None = None
    discarded: str | None = None
    enforced: list[str] = field(default_factory=list)
    attempts: int = 0
    regenerated: bool = False


def target_year_pending(target_year: int | None, today: date) -> bool:
    return target_year is not None and target_year > today.year


def enforce(judgement: RelationJudgement, candidate: Candidate, today: date) -> tuple[RelationJudgement, list[str]]:
    """(a) 목표연도 미도래 '위반' → '이행지연' · (b) 소급 건 rationale 에 '소급 확인' 강제. 바꾼 내용은 note 로 돌려준다."""
    notes: list[str] = []
    updates: dict = {}
    rationale = judgement.rationale.strip()
    if judgement.relation == "위반" and target_year_pending(candidate.commitment.target_year, today):
        updates["relation"] = "이행지연"
        rationale = f"{rationale} {PENDING_NOTE}"
        notes.append(f"{candidate.label}: target_year {candidate.commitment.target_year} 미도래 — 위반 → 이행지연")
    if candidate.event.is_retrospective and RETRO_MARK not in rationale:
        rationale = f"{rationale} {RETRO_SENTENCE}"
        notes.append(f"{candidate.label}: 소급 건 — rationale 에 '소급 확인' 추가")
    updates["rationale"] = rationale
    return judgement.model_copy(update=updates), notes


def event_source(candidate: Candidate) -> str:
    return candidate.event.evidence_quote or candidate.event.summary


def verify_quotes(judgement: RelationJudgement, candidate: Candidate) -> list[str]:
    """원문에 없는 인용 목록. 비어 있으면 통과."""
    failed: list[str] = []
    if not quote_in(judgement.commitment_quote, candidate.commitment.commitment_text):
        failed.append(f"commitment_quote: {judgement.commitment_quote}")
    if not quote_in(judgement.event_quote, event_source(candidate)):
        failed.append(f"event_quote: {judgement.event_quote}")
    return failed


def _judge(client: LLMClient, model: str, candidate: Candidate, today: date, **hints) -> RelationJudgement:
    return client.complete(
        RelationJudgement,
        SYSTEM_PROMPT,
        build_user_prompt(candidate.commitment, candidate.event, candidate.gap_months, today=today, **hints),
        model=model,
        prompt_version=PROMPT_VERSION,
    )


def judge_candidate(client: LLMClient, model: str, candidate: Candidate, *, today: date, log: Log = print) -> JudgeOutcome:
    """후보 하나 판정. 인용·스키마 실패 → 힌트 붙여 1회 재생성 → 재실패 폐기. 통과분에 (a)(b) 강제."""
    outcome = JudgeOutcome()
    judgement: RelationJudgement | None = None
    failed: list[str] = []
    schema_error: str | None = None

    outcome.attempts += 1
    try:
        judgement = _judge(client, model, candidate, today)
        failed = verify_quotes(judgement, candidate)
    except ValueError as exc:
        schema_error = str(exc)

    if failed or schema_error:
        outcome.regenerated = True
        outcome.attempts += 1
        log(f"  {candidate.label}: 재생성 — " + (f"원문에 없는 인용 {len(failed)}건" if failed else f"스키마 실패: {schema_error[:120]}"))
        try:
            hints = {"failed_quotes": failed} if failed else {"schema_error": schema_error}
            judgement = _judge(client, model, candidate, today, **hints)
            failed = verify_quotes(judgement, candidate)
        except ValueError as exc:
            outcome.discarded = f"{candidate.label}: (스키마 실패) {str(exc)[:200]}"
            return outcome
        if failed:
            outcome.discarded = f"{candidate.label}: 인용 실패 — {'; '.join(failed)[:300]}"
            return outcome

    assert judgement is not None
    outcome.judgement, outcome.enforced = enforce(judgement, candidate, today)
    return outcome


def clamp_confidence(value: int) -> int:
    return max(0, min(100, int(value)))


def match_values(candidate: Candidate, judgement: RelationJudgement) -> dict:
    """Match 행 값. scores 는 None — F-05 가 채운다."""
    return {
        "commitment_id": candidate.commitment.id,
        "event_id": candidate.event.id,
        "relation": judgement.relation,
        "rationale": judgement.rationale,
        "evidence_quotes": {"commitment_quote": judgement.commitment_quote, "event_quote": judgement.event_quote},
        "llm_confidence": clamp_confidence(judgement.llm_confidence),
        "gap_months": candidate.gap_months,
        "prompt_version": PROMPT_VERSION,
        "status": MATCH_STATUS,
        "scores": None,
    }


# --------------------------------------------------------------------------- DB
@dataclass
class MatchResult:
    run_id: int
    status: str
    stats: dict = field(default_factory=dict)


def _new_stats(stock_code: str, candidates: int) -> dict:
    return {
        "company": stock_code,
        "candidates": candidates,
        "attempts": 0,
        "regenerated": 0,
        "judged": 0,
        "upserted": 0,
        "relations": dict.fromkeys(RELATIONS, 0),
        "enforced": 0,
        "discarded": 0,
        "llm_calls": 0,
        "cache_hits": 0,
        "errors": [],
    }


def _load_company(stock_code: str):
    from sqlalchemy import select

    from esg_watchdog.db import SessionLocal
    from esg_watchdog.models import Company

    with SessionLocal() as session:
        company = session.scalar(select(Company).where(Company.stock_code == stock_code))
        if company is None:
            raise LookupError(f"companies 에 없는 stock_code: {stock_code} — 먼저 `esg-watchdog seed-companies`")
        return company.id, f"{company.name}({company.stock_code})"


def _upsert_match(session, values: dict) -> None:
    from sqlalchemy.dialects.postgresql import insert

    from esg_watchdog.models import Match

    stmt = insert(Match).values(**values)
    update_cols = {key: value for key, value in values.items() if key not in ("commitment_id", "event_id")}
    session.execute(stmt.on_conflict_do_update(constraint="uq_matches_commitment_event", set_=update_cols))


def match_events(
    *,
    stock_code: str,
    client: LLMClient | None = None,
    model: str | None = None,
    today: date | None = None,
    log: Log = print,
) -> MatchResult:
    """관계 판정 한 번 실행. 후보 단위로 예외를 격리하고 pipeline_runs 에 기록한다."""
    from sqlalchemy.exc import SQLAlchemyError

    from esg_watchdog.db import SessionLocal

    if client is None:
        client = LLMClient()
    if model is None:
        from esg_watchdog.config import settings

        model = settings.llm_model_judge
    # 키·공급자 설정 오류는 pipeline_runs 를 만들기 전에 낸다
    provider_name = client.provider.name
    today = today or now_kst().date()

    company_id, label = _load_company(stock_code)
    candidates = load_candidates(company_id)
    log(f"[match] {label}: 후보 {len(candidates)}건 (같은 category · 0<gap≤24 · 기존 matches 제외) → LLM 호출 {len(candidates)}회")

    run_id = start_run(STAGE)
    stats = _new_stats(stock_code, len(candidates))
    notes: list[str] = []
    failed: list[str] = []
    calls_before, hits_before = client.stats["calls"], client.stats["cache_hits"]
    log(f"[match] run={run_id} provider={provider_name} model={model or '-'} prompt={PROMPT_VERSION}")

    for candidate in candidates:
        try:
            outcome = judge_candidate(client, model, candidate, today=today, log=log)
            stats["attempts"] += outcome.attempts
            stats["regenerated"] += int(outcome.regenerated)
            stats["judged"] += 1
            if outcome.judgement is None:
                stats["discarded"] += 1
                notes.append(f"폐기: {outcome.discarded}")
                log(f"  {candidate.label}: 폐기 — {outcome.discarded}")
                continue
            stats["enforced"] += len(outcome.enforced)
            notes.extend(f"강제: {item}" for item in outcome.enforced)
            values = match_values(candidate, outcome.judgement)
            with SessionLocal() as session:
                _upsert_match(session, values)
                session.commit()
            stats["upserted"] += 1
            stats["relations"][values["relation"]] += 1
            log(f"  {candidate.label}: gap={candidate.gap_months} relation={values['relation']} confidence={values['llm_confidence']}")
        except (ValueError, LookupError, RuntimeError, SQLAlchemyError) as exc:
            failed.append(candidate.label)
            stats["errors"].append({"candidate": candidate.label, "error": f"{type(exc).__name__}: {exc}"})
            log(f"ERROR {candidate.label}: {type(exc).__name__}: {exc}")

    stats["llm_calls"] = client.stats["calls"] - calls_before
    stats["cache_hits"] = client.stats["cache_hits"] - hits_before
    status = decide_status(len(candidates) - len(failed), len(failed)) if candidates else "success"
    note_parts = [part for part in (failure_note(failed), *notes) if part]
    finish_run(run_id, status, stats, "; ".join(note_parts) or None)
    return MatchResult(run_id=run_id, status=status, stats=stats)
