"""F-06 경보 발행 — matches → alerts (D-08 · D-16 · D-17).

- 대상: matches(status 'accepted', relation ∈ ALERT_RELATIONS, scores not null, alert 없음). 회사(--company)를 주면 그 회사만.
- grade: GRADE_THRESHOLDS(knowledge/weights.py) 로 scores.materiality 를 등급화하고, event.confirmed false 면 '주의' 로 캡(D-08 ②).
- LLM(llm_model_judge) → AlertExplanation → banned_terms.check_fields(applies_to 4필드) → 걸리면 regeneration_hint 를 붙여 1회 재생성 →
  또 걸리면 폐기 + pipeline_runs.note 에 (match_id, hits). explanation 길이가 400~800자를 벗어나면 1회 재생성, 그래도 벗어나면 채택 + note.
  두 문제가 같이 나면 힌트를 함께 붙여 한 번만 재생성한다(경보당 LLM 최대 2회). 스키마 실패(ValueError)도 같은 재생성 1회.
- confirmed false 인데 limitation 에 "확정되지 않" 이 없으면 UNCONFIRMED_SENTENCE 를 붙인다(D-08 ④).
- Alert(published_at=now KST, status 'published', prompt_version) insert — match_id UNIQUE 로 idempotent(있으면 건너뜀).
- pipeline_runs(stage='publish', trigger='manual'). 경보 단위 예외 격리. DB·settings import 는 함수 안에서만.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from esg_watchdog.knowledge.banned_terms import check_fields, regeneration_hint
from esg_watchdog.knowledge.taxonomy import ALERT_RELATIONS, GRADES
from esg_watchdog.knowledge.weights import GRADE_THRESHOLDS
from esg_watchdog.llm.client import LLMClient
from esg_watchdog.prompts.alert_explain import (
    MAX_LENGTH,
    MIN_LENGTH,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    AlertCommitment,
    AlertContext,
    AlertEvent,
    AlertExplanation,
    build_user_prompt,
    length_hint,
    presses_of,
)
from esg_watchdog.services.runs import (
    decide_status,
    failure_note,
    finish_run,
    now_kst,
    start_run,
)

STAGE = "publish"
ALERT_STATUS = "published"
CAP_GRADE = "주의"  # D-08 ②: 미확정 사건의 상한
UNCONFIRMED_MARK = "확정되지 않"
UNCONFIRMED_SENTENCE = "이 사건은 조사·의혹 단계로 확정되지 않았습니다."

Log = Callable[[str], None]


# --------------------------------------------------------------------------- 순수 함수
def grade_for(materiality: float | None) -> str:
    """GRADE_THRESHOLDS 위에서부터 materiality ≥ threshold 인 첫 등급."""
    value = float(materiality or 0)
    for grade, threshold in GRADE_THRESHOLDS:
        if value >= threshold:
            return grade
    return GRADE_THRESHOLDS[-1][0]


def cap_grade(grade: str, confirmed: bool) -> str:
    """confirmed false 면 '주의' 를 넘지 않는다."""
    if confirmed:
        return grade
    return min(grade, CAP_GRADE, key=GRADES.index)


def explanation_length(text: str) -> int:
    """공백을 하나로 합친 글자 수(단락 구분 빈 줄은 세지 않는다)."""
    return len(" ".join((text or "").split()))


def length_problem(text: str) -> int | None:
    """범위 밖이면 길이, 안이면 None."""
    length = explanation_length(text)
    return length if not (MIN_LENGTH <= length <= MAX_LENGTH) else None


def ensure_limitation(limitation: str, confirmed: bool) -> str:
    text = (limitation or "").strip()
    if not confirmed and UNCONFIRMED_MARK not in text:
        return f"{text} {UNCONFIRMED_SENTENCE}".strip()
    return text


@dataclass
class PublishTarget:
    match_id: int
    company_id: int
    confirmed: bool
    materiality: int
    context: AlertContext

    @property
    def label(self) -> str:
        return f"match {self.match_id}"


@dataclass
class ExplainOutcome:
    explanation: AlertExplanation | None = None
    discarded: str | None = None
    hits: dict[str, list[str]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    attempts: int = 0
    regenerated: bool = False


def _explain(client: LLMClient, model: str, context: AlertContext, **hints) -> AlertExplanation:
    return client.complete(
        AlertExplanation, SYSTEM_PROMPT, build_user_prompt(context, **hints), model=model, prompt_version=PROMPT_VERSION
    )


def _problems(explanation: AlertExplanation) -> tuple[dict[str, list[str]], int | None]:
    return check_fields(explanation.model_dump()), length_problem(explanation.explanation)


def explain_target(client: LLMClient, model: str, target: PublishTarget, *, log: Log = print) -> ExplainOutcome:
    """경보 하나의 설명문. 금지어·길이·스키마 문제는 힌트를 붙여 1회 재생성 → 금지어 재발은 폐기, 길이 재발은 채택 + note."""
    outcome = ExplainOutcome()
    explanation: AlertExplanation | None = None
    hits: dict[str, list[str]] = {}
    length: int | None = None
    schema_error: str | None = None

    outcome.attempts += 1
    try:
        explanation = _explain(client, model, target.context)
        hits, length = _problems(explanation)
    except ValueError as exc:
        schema_error = str(exc)

    if hits or length is not None or schema_error:
        outcome.regenerated = True
        outcome.attempts += 1
        reason = (
            f"스키마 실패: {schema_error[:120]}"
            if schema_error
            else " · ".join(part for part in (f"금지어 {hits}" if hits else "", f"길이 {length}자" if length is not None else "") if part)
        )
        log(f"  {target.label}: 재생성 — {reason}")
        hint_terms = [term for terms in hits.values() for term in terms]
        hints = {
            "banned_hint": regeneration_hint(hint_terms) if hint_terms else None,
            "length_hint": length_hint(length) if length is not None else None,
            "schema_error": schema_error,
        }
        try:
            explanation = _explain(client, model, target.context, **hints)
            hits, length = _problems(explanation)
        except ValueError as exc:
            outcome.discarded = f"{target.label}: (스키마 실패) {str(exc)[:200]}"
            return outcome
        if hits:
            outcome.hits = hits
            outcome.discarded = f"{target.label}: 금지어 재발 {hits}"
            return outcome
        if length is not None:
            outcome.notes.append(f"{target.label}: explanation {length}자 — {MIN_LENGTH}~{MAX_LENGTH}자 범위 밖이지만 채택")

    assert explanation is not None
    limitation = ensure_limitation(explanation.limitation, target.confirmed)
    if limitation != explanation.limitation.strip():
        outcome.notes.append(f"{target.label}: 미확정 사건 — limitation 에 확정되지 않았다는 문장 추가 (D-08 ④)")
    outcome.explanation = explanation.model_copy(update={"limitation": limitation})
    return outcome


def alert_values(target: PublishTarget, explanation: AlertExplanation, published_at: datetime) -> dict:
    grade = cap_grade(grade_for(target.materiality), target.confirmed)
    return {
        "match_id": target.match_id,
        "company_id": target.company_id,
        "grade": grade,
        "headline": explanation.headline.strip(),
        "explanation": explanation.explanation.strip(),
        "limitation": explanation.limitation.strip(),
        "fallback": explanation.fallback.strip() if explanation.fallback else None,
        "published_at": published_at,
        "status": ALERT_STATUS,
        "prompt_version": PROMPT_VERSION,
    }


# --------------------------------------------------------------------------- DB
@dataclass
class PublishResult:
    run_id: int
    status: str
    stats: dict = field(default_factory=dict)


def _new_stats(stock_code: str | None, targets: int) -> dict:
    return {
        "company": stock_code or "all",
        "targets": targets,
        "attempts": 0,
        "regenerated": 0,
        "published": 0,
        "grades": dict.fromkeys(GRADES, 0),
        "capped": 0,
        "discarded": 0,
        "length_noted": 0,
        "skipped_existing": 0,
        "llm_calls": 0,
        "cache_hits": 0,
        "errors": [],
    }


def _load_targets(stock_code: str | None) -> tuple[str, list[PublishTarget]]:
    from sqlalchemy import exists, select

    from esg_watchdog.db import SessionLocal
    from esg_watchdog.models import (
        Alert,
        Article,
        Commitment,
        Company,
        Document,
        Event,
        Match,
    )

    has_alert = exists().where(Alert.match_id == Match.id)
    stmt = (
        select(Match, Commitment, Event, Company)
        .join(Commitment, Commitment.id == Match.commitment_id)
        .join(Company, Company.id == Commitment.company_id)
        .join(Event, Event.id == Match.event_id)
        .where(Match.status == "accepted", Match.relation.in_(ALERT_RELATIONS), Match.scores.is_not(None), ~has_alert)
        .order_by(Match.id)
    )
    label = "전체"
    targets: list[PublishTarget] = []
    with SessionLocal() as session:
        if stock_code:
            company = session.scalar(select(Company).where(Company.stock_code == stock_code))
            if company is None:
                raise LookupError(f"companies 에 없는 stock_code: {stock_code} — 먼저 `esg-watchdog seed-companies`")
            label = f"{company.name}({company.stock_code})"
            stmt = stmt.where(Company.id == company.id)
        for match, commitment, event, company in session.execute(stmt).all():
            source = commitment.source or {}
            document = session.get(Document, source.get("doc_id")) if source.get("doc_id") else None
            presses = presses_of(
                session.scalars(select(Article.press).where(Article.id.in_(list(event.sources or [])))).all()
            )
            scores = dict(match.scores or {})
            targets.append(
                PublishTarget(
                    match_id=match.id,
                    company_id=company.id,
                    confirmed=bool(event.confirmed),
                    materiality=int(scores.get("materiality") or 0),
                    context=AlertContext(
                        company_name=company.name,
                        commitment=AlertCommitment(
                            commitment_text=commitment.commitment_text,
                            document_title=document.title if document else None,
                            fiscal_year=document.fiscal_year if document else None,
                            page=source.get("page"),
                            filed_at=commitment.filed_at,
                        ),
                        event=AlertEvent(
                            title=event.title,
                            summary=event.summary,
                            evidence_quote=event.evidence_quote,
                            event_date=event.event_date,
                            date_precision=event.date_precision,
                            confirmed=bool(event.confirmed),
                            confirmed_basis=event.confirmed_basis,
                            severity_signals=dict(event.severity_signals or {}),
                            presses=presses,
                        ),
                        relation=match.relation,
                        gap_months=match.gap_months,
                        is_retrospective=bool(event.is_retrospective),
                        scores=scores,
                    ),
                )
            )
    return label, targets


def _insert_alert(session, values: dict) -> bool:
    """match_id UNIQUE — 이미 있으면 건너뛴다(idempotent). 넣었으면 True."""
    from sqlalchemy.dialects.postgresql import insert

    from esg_watchdog.models import Alert

    result = session.execute(insert(Alert).values(**values).on_conflict_do_nothing(index_elements=["match_id"]))
    return bool(result.rowcount)


def publish_alerts(
    *,
    stock_code: str | None = None,
    client: LLMClient | None = None,
    model: str | None = None,
    now: datetime | None = None,
    log: Log = print,
) -> PublishResult:
    """경보 발행 한 번 실행. 경보 단위로 예외를 격리하고 pipeline_runs 에 기록한다."""
    from sqlalchemy.exc import SQLAlchemyError

    from esg_watchdog.db import SessionLocal

    if client is None:
        client = LLMClient()
    if model is None:
        from esg_watchdog.config import settings

        model = settings.llm_model_judge
    # 키·공급자 설정 오류는 pipeline_runs 를 만들기 전에 낸다
    provider_name = client.provider.name

    label, targets = _load_targets(stock_code)
    log(f"[publish] {label}: 대상 {len(targets)}건 (accepted · {'/'.join(ALERT_RELATIONS)} · scores 있음 · alert 없음) → LLM 호출 {len(targets)}회")

    run_id = start_run(STAGE)
    stats = _new_stats(stock_code, len(targets))
    notes: list[str] = []
    failed: list[str] = []
    calls_before, hits_before = client.stats["calls"], client.stats["cache_hits"]
    log(f"[publish] run={run_id} provider={provider_name} model={model or '-'} prompt={PROMPT_VERSION}")

    for target in targets:
        try:
            outcome = explain_target(client, model, target, log=log)
            stats["attempts"] += outcome.attempts
            stats["regenerated"] += int(outcome.regenerated)
            notes.extend(outcome.notes)
            stats["length_noted"] += sum("범위 밖" in note for note in outcome.notes)
            if outcome.explanation is None:
                stats["discarded"] += 1
                notes.append(f"폐기: {outcome.discarded}")
                log(f"  {target.label}: 폐기 — {outcome.discarded}")
                continue
            values = alert_values(target, outcome.explanation, now or now_kst())
            with SessionLocal() as session:
                inserted = _insert_alert(session, values)
                session.commit()
            if not inserted:
                stats["skipped_existing"] += 1
                log(f"  {target.label}: 이미 alert 가 있어 건너뜀")
                continue
            stats["published"] += 1
            stats["grades"][values["grade"]] += 1
            if not target.confirmed and grade_for(target.materiality) != values["grade"]:
                stats["capped"] += 1
            log(
                f"  {target.label}: grade={values['grade']} (materiality={target.materiality}, confirmed={target.confirmed}) "
                f"headline={values['headline'][:40]!r} explanation={explanation_length(values['explanation'])}자"
            )
        except (ValueError, LookupError, RuntimeError, SQLAlchemyError) as exc:
            failed.append(target.label)
            stats["errors"].append({"match": target.match_id, "error": f"{type(exc).__name__}: {exc}"})
            log(f"ERROR {target.label}: {type(exc).__name__}: {exc}")

    stats["llm_calls"] = client.stats["calls"] - calls_before
    stats["cache_hits"] = client.stats["cache_hits"] - hits_before
    status = decide_status(len(targets) - len(failed), len(failed)) if targets else "success"
    note_parts = [part for part in (failure_note(failed), *notes) if part]
    finish_run(run_id, status, stats, "; ".join(note_parts) or None)
    return PublishResult(run_id=run_id, status=status, stats=stats)
