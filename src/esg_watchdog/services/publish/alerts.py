"""F-06 경보 발행 — matches → alerts (D-08 · D-16 · D-17).

- 대상: matches(status 'accepted', relation ∈ ALERT_RELATIONS, scores not null, alert 없음, scores.materiality ≥ MIN_PUBLISH_MATERIALITY).
  회사(--company)를 주면 그 회사만. 하한 미달은 폐기가 아니라 대상에서 빠질 뿐이고(matches 는 accepted 유지) 건수만 stage_stats.below_threshold 에 남긴다(D-16).
- 근거 품질 게이트(D-08 · D-17): 사건 sources 기사 중 제목에 그 기업의 이름 또는 별칭(companies.name · aliases, 대소문자 무시)이 든 것이
  하나도 없으면 발행하지 않는다. F-01 이 별칭을 본문에서도 찾고 D-07 이 (기업·카테고리·유형·연-월)로 묶어, 업종 나열로 스친 총계 기사만으로
  사건이 서는 구조적 결함(실측 match 3219: 출처 29건 전부 제목에 KT 없음)을 막는다. 제외 건은 폐기가 아니라 대상에서 빠지고(matches 는
  accepted 유지) stage_stats.no_named_source 에 남긴다. sources 가 비어 있어도 걸린다. --allow-unnamed-source 로 끈다(기본 적용).
  순서: 하한 → 이 게이트 → 같은 사건 억제(제외 건이 per-event 자리를 차지하지 않게).
- 같은 사건 억제(D-16): 같은 event_id 에는 경보를 최대 --per-event(기본 DEFAULT_PER_EVENT=1)건만 낸다. 남길 기준은 materiality 내림차순 →
  confidence 내림차순 → match_id 오름차순. DB 에 이미 그 사건으로 발행된 alert(published)도 개수에 넣어 센다(재실행 시 중복 증식 방지).
  억제된 matches 는 status 를 바꾸지 않고 accepted 로 둔다 — 나중에 --per-event 를 올리면 발행된다. 건수는 stage_stats.suppressed_same_event.
  첫 줄 출력: "대상 N건(하한 미달 제외 M건 · 기업 미언급 출처만 K건 · 같은 사건 억제 J건) → 등급 심각 a · 경고 b · 주의 c"
  — 등급 미리보기는 미확정 '주의' 캡을 적용한 값.
- grade: GRADE_THRESHOLDS(knowledge/weights.py) 로 scores.materiality 를 등급화하고, event.confirmed false 면 '주의' 로 캡(D-08 ②).
- LLM(llm_model_judge) → AlertExplanation → banned_terms.check_fields(applies_to 4필드). 문제는 세 사유로 나눠 사유별 1회씩, 경보당 합계
  MAX_REGENERATIONS(2)회까지 힌트를 붙여 재생성한다(LLM 최대 3회). 같은 사유가 두 번이면 — 금지어: 폐기 + note(match_id, hits) ·
  스키마 실패(ValueError): 폐기 · 길이(400~800자 밖): 채택 + note. 금지어와 길이가 같이 나면 힌트를 함께 붙여 한 번에 재생성하고,
  금지어 힌트는 이후 재생성에도 유지한다. 실측(run=42)에서 금지어 재생성 응답이 필드 누락(스키마)으로 죽어 살릴 수 없었던 경로를 살린다(D-17).
  사유별 횟수는 stage_stats.regenerated_banned · regenerated_schema · regenerated_length 에 남긴다.
- confirmed false 인데 limitation 에 "확정되지 않" 이 없으면 UNCONFIRMED_SENTENCE 를 붙인다(D-08 ④).
- Alert(published_at=now KST, status 'published', prompt_version) insert — match_id UNIQUE 로 idempotent(있으면 건너뜀).
- pipeline_runs(stage='publish', trigger='manual'). 경보 단위 예외 격리. DB·settings import 는 함수 안에서만.
"""

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from esg_watchdog.knowledge.banned_terms import check_fields, regeneration_hint
from esg_watchdog.knowledge.taxonomy import ALERT_RELATIONS, GRADES
from esg_watchdog.knowledge.weights import GRADE_THRESHOLDS, MIN_PUBLISH_MATERIALITY
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
# 재생성 사유 — 사유마다 1회씩, 경보당 합계 MAX_REGENERATIONS 회까지 (D-17)
REASON_BANNED = "banned"
REASON_SCHEMA = "schema"
REASON_LENGTH = "length"
REASONS = (REASON_BANNED, REASON_SCHEMA, REASON_LENGTH)
MAX_REGENERATIONS = 2
# 같은 event_id 에 내는 경보 상한 기본값 (--per-event, D-16). 실측: 발행 20건 중 절반이 같은 사건에 공약만 다른 경보였다
DEFAULT_PER_EVENT = 1

Log = Callable[[str], None]


# --------------------------------------------------------------------------- 순수 함수
def grade_for(materiality: float | None) -> str:
    """GRADE_THRESHOLDS 위에서부터 materiality ≥ threshold 인 첫 등급. 하한 미달(발행 대상 아님)은 마지막 등급을 돌려준다."""
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


def is_publishable(materiality: float | None) -> bool:
    """D-16 발행 하한 — materiality ≥ MIN_PUBLISH_MATERIALITY 만 경보 대상. 미달은 폐기가 아니라 미발행(matches 는 그대로)."""
    return float(materiality or 0) >= MIN_PUBLISH_MATERIALITY


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
    # 같은 사건 억제용 (event 별 상한 · 남길 순서)
    event_id: int = 0
    confidence: int = 0
    # 근거 품질 게이트: sources 기사 제목 중 기업명·별칭이 든 것이 하나라도 있는가 · sources 수
    named_source: bool = True
    source_count: int = 0

    @property
    def label(self) -> str:
        return f"match {self.match_id}"

    @property
    def rank(self) -> tuple[int, int, int]:
        """같은 사건에서 남길 순서: materiality 내림차순 → confidence 내림차순 → match_id 오름차순."""
        return (-self.materiality, -self.confidence, self.match_id)

    @property
    def grade(self) -> str:
        """발행될 등급(미확정 캡 적용). alert_values 와 같은 계산."""
        return cap_grade(grade_for(self.materiality), self.confirmed)


def company_terms(name: str | None, aliases: Iterable[str] | None) -> list[str]:
    """제목 매칭에 쓰는 기업명 + 별칭. 빈 값·중복 제거."""
    values = [name, *(aliases or [])]
    return list(dict.fromkeys(str(value).strip() for value in values if value and str(value).strip()))


def has_named_source(titles: Iterable[str | None], terms: Sequence[str]) -> bool:
    """sources 기사 제목 중 하나라도 기업명·별칭을 담고 있으면 True (대소문자 무시). 제목이 없으면 False."""
    folded = [term.casefold() for term in terms if term]
    return any(term in str(title).casefold() for title in titles if title for term in folded)


def apply_source_gate(
    targets: Sequence[PublishTarget], allow_unnamed_source: bool
) -> tuple[list[PublishTarget], list[PublishTarget]]:
    """(통과, 제외). allow_unnamed_source 면 게이트를 끈다."""
    if allow_unnamed_source:
        return list(targets), []
    kept = [target for target in targets if target.named_source]
    gated = [target for target in targets if not target.named_source]
    return kept, gated


def gate_lines(gated: Sequence[PublishTarget]) -> list[str]:
    """제외 내역을 사건별로 한 줄씩."""
    by_event: dict[int, list[PublishTarget]] = {}
    for target in gated:
        by_event.setdefault(target.event_id, []).append(target)
    return [
        f"[publish] 기업 미언급 출처만(--allow-unnamed-source 로 해제): event {event_id} → match "
        f"{' · '.join(str(item.match_id) for item in items)} (출처 {items[0].source_count}건, 제목에 {items[0].context.company_name} 없음)"
        for event_id, items in by_event.items()
    ]


@dataclass
class TargetSet:
    """_load_targets 결과. existing_alerts 는 event_id → 이미 발행된(published) alert 수."""

    label: str
    targets: list[PublishTarget]
    below_threshold: int = 0
    existing_alerts: dict[int, int] = field(default_factory=dict)


def suppress_same_event(
    targets: Sequence[PublishTarget], existing_alerts: Mapping[int, int], per_event: int
) -> tuple[list[PublishTarget], list[PublishTarget]]:
    """같은 event_id 는 최대 per_event 건(기존 alert 포함). (남긴 대상 match_id 순, 억제된 대상 rank 순)."""
    counts: dict[int, int] = {int(event_id): int(count) for event_id, count in existing_alerts.items()}
    kept: list[PublishTarget] = []
    suppressed: list[PublishTarget] = []
    for target in sorted(targets, key=lambda item: item.rank):
        if counts.get(target.event_id, 0) >= per_event:
            suppressed.append(target)
            continue
        counts[target.event_id] = counts.get(target.event_id, 0) + 1
        kept.append(target)
    kept.sort(key=lambda item: item.match_id)
    return kept, suppressed


def suppression_lines(kept: Sequence[PublishTarget], suppressed: Sequence[PublishTarget], per_event: int) -> list[str]:
    """억제 내역을 사건별로 한 줄씩 — 어떤 match 를 남기고 어떤 match 를 눌렀는지."""
    kept_by_event: dict[int, list[PublishTarget]] = {}
    for target in kept:
        kept_by_event.setdefault(target.event_id, []).append(target)
    suppressed_by_event: dict[int, list[PublishTarget]] = {}
    for target in suppressed:
        suppressed_by_event.setdefault(target.event_id, []).append(target)
    lines = []
    for event_id, targets in suppressed_by_event.items():
        kept_text = " · ".join(f"{item.match_id}({item.materiality})" for item in kept_by_event.get(event_id, [])) or "기존 alert"
        dropped = " · ".join(f"{item.match_id}({item.materiality})" for item in targets)
        lines.append(f"[publish] 같은 사건 억제(--per-event {per_event}): event {event_id} → 남김 {kept_text} · 억제 {dropped}")
    return lines


def grade_counts(targets: Sequence[PublishTarget]) -> dict[str, int]:
    """대상의 등급 분포 미리보기 — 키는 GRADES 순서(주의 · 경고 · 심각)."""
    counts = dict.fromkeys(GRADES, 0)
    for target in targets:
        counts[target.grade] += 1
    return counts


def targets_line(
    label: str, targets: Sequence[PublishTarget], below_threshold: int, suppressed: int = 0, no_named_source: int = 0
) -> str:
    """publish 첫 줄: 대상 N건(하한 미달 제외 M건 · 기업 미언급 출처만 K건 · 같은 사건 억제 J건) → 등급 심각 a · 경고 b · 주의 c."""
    counts = grade_counts(targets)
    grades = " · ".join(f"{grade} {counts[grade]}" for grade in reversed(GRADES))
    return (
        f"[publish] {label}: 대상 {len(targets)}건(하한 미달 제외 {below_threshold}건 · 기업 미언급 출처만 {no_named_source}건 · "
        f"같은 사건 억제 {suppressed}건) → 등급 {grades} "
        f"(accepted · {'/'.join(ALERT_RELATIONS)} · materiality ≥ {MIN_PUBLISH_MATERIALITY} · alert 없음 → LLM 호출 {len(targets)}회)"
    )


@dataclass
class ExplainOutcome:
    explanation: AlertExplanation | None = None
    discarded: str | None = None
    hits: dict[str, list[str]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    attempts: int = 0
    # 사유별 재생성 횟수 {banned, schema, length}
    regenerations: dict[str, int] = field(default_factory=lambda: dict.fromkeys(REASONS, 0))

    @property
    def regenerated(self) -> bool:
        return any(self.regenerations.values())


def _explain(client: LLMClient, model: str, context: AlertContext, **hints) -> AlertExplanation:
    return client.complete(
        AlertExplanation, SYSTEM_PROMPT, build_user_prompt(context, **hints), model=model, prompt_version=PROMPT_VERSION
    )


def _problems(explanation: AlertExplanation) -> tuple[dict[str, list[str]], int | None]:
    return check_fields(explanation.model_dump()), length_problem(explanation.explanation)


def explain_target(client: LLMClient, model: str, target: PublishTarget, *, log: Log = print) -> ExplainOutcome:
    """경보 하나의 설명문. 문제(금지어 · 스키마 · 길이)마다 힌트를 붙여 사유별 1회씩, 합계 MAX_REGENERATIONS 회까지 재생성한다.

    같은 사유가 두 번(또는 합계 한도 소진)이면 — 금지어·스키마: 폐기, 길이: 채택 + note. 금지어 힌트는 한 번 붙으면 이후 재생성에도 남겨
    같은 표현이 되살아나지 않게 하고, 스키마 오류는 직전 실패에만 붙인다.
    """
    outcome = ExplainOutcome()
    used: set[str] = set()
    banned_hint: str | None = None
    hint_for_length: str | None = None
    schema_error: str | None = None
    explanation: AlertExplanation | None = None

    while True:
        outcome.attempts += 1
        hits: dict[str, list[str]] = {}
        length: int | None = None
        failed_schema: str | None = None
        try:
            explanation = _explain(
                client, model, target.context, banned_hint=banned_hint, length_hint=hint_for_length, schema_error=schema_error
            )
            hits, length = _problems(explanation)
        except ValueError as exc:
            failed_schema = str(exc)

        if failed_schema:
            reason, detail = REASON_SCHEMA, f"스키마 실패: {failed_schema[:120]}"
        elif hits:
            reason, detail = REASON_BANNED, f"금지어 {hits}" + (f" · 길이 {length}자" if length is not None else "")
        elif length is not None:
            reason, detail = REASON_LENGTH, f"길이 {length}자"
        else:
            break  # 문제 없음

        repeated = reason in used
        if repeated or sum(outcome.regenerations.values()) >= MAX_REGENERATIONS:
            why = "재발" if repeated else f"재생성 한도 {MAX_REGENERATIONS}회 소진"
            if reason == REASON_SCHEMA:
                outcome.discarded = f"{target.label}: (스키마 실패) {why} — {failed_schema[:200]}"
                return outcome
            if reason == REASON_BANNED:
                outcome.hits = hits
                outcome.discarded = f"{target.label}: 금지어 {why} {hits}"
                return outcome
            outcome.notes.append(f"{target.label}: explanation {length}자 — {MIN_LENGTH}~{MAX_LENGTH}자 범위 밖이지만 채택")
            break

        used.add(reason)
        outcome.regenerations[reason] += 1
        log(f"  {target.label}: 재생성({reason}) — {detail}")
        if hits:
            banned_hint = regeneration_hint([term for terms in hits.values() for term in terms])
        # 길이 힌트는 마지막으로 본 응답 기준. 스키마 실패면 길이를 모르니 이전 힌트를 그대로 둔다
        if length is not None:
            hint_for_length = length_hint(length)
        elif explanation is not None and not failed_schema:
            hint_for_length = None
        schema_error = failed_schema

    assert explanation is not None
    limitation = ensure_limitation(explanation.limitation, target.confirmed)
    if limitation != explanation.limitation.strip():
        outcome.notes.append(f"{target.label}: 미확정 사건 — limitation 에 확정되지 않았다는 문장 추가 (D-08 ④)")
    outcome.explanation = explanation.model_copy(update={"limitation": limitation})
    return outcome


def alert_values(target: PublishTarget, explanation: AlertExplanation, published_at: datetime) -> dict:
    return {
        "match_id": target.match_id,
        "company_id": target.company_id,
        "grade": target.grade,
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


def _new_stats(
    stock_code: str | None,
    targets: int,
    below_threshold: int,
    no_named_source: int,
    suppressed: int,
    *,
    per_event: int,
    allow_unnamed_source: bool,
) -> dict:
    return {
        "company": stock_code or "all",
        "targets": targets,
        "below_threshold": below_threshold,
        "no_named_source": no_named_source,
        "allow_unnamed_source": allow_unnamed_source,
        "suppressed_same_event": suppressed,
        "per_event": per_event,
        "attempts": 0,
        "regenerated": 0,
        "regenerated_banned": 0,
        "regenerated_schema": 0,
        "regenerated_length": 0,
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


def _existing_alert_counts(session, event_ids: set[int]) -> dict[int, int]:
    """event_id → 이미 발행된(published) alert 수. 같은 사건 억제가 재실행에서도 증식하지 않게 한다."""
    from sqlalchemy import func, select

    from esg_watchdog.models import Alert, Match

    if not event_ids:
        return {}
    rows = session.execute(
        select(Match.event_id, func.count(Alert.id))
        .join(Alert, Alert.match_id == Match.id)
        .where(Match.event_id.in_(sorted(event_ids)), Alert.status == ALERT_STATUS)
        .group_by(Match.event_id)
    ).all()
    return {int(event_id): int(count) for event_id, count in rows}


def _load_targets(stock_code: str | None) -> TargetSet:
    """대상(하한 통과) · 하한 미달 수 · 사건별 기존 alert 수. 하한은 컨텍스트(문서·언론사 조회)를 만들기 전에 건다."""
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
    below_threshold = 0
    with SessionLocal() as session:
        if stock_code:
            company = session.scalar(select(Company).where(Company.stock_code == stock_code))
            if company is None:
                raise LookupError(f"companies 에 없는 stock_code: {stock_code} — 먼저 `esg-watchdog seed-companies`")
            label = f"{company.name}({company.stock_code})"
            stmt = stmt.where(Company.id == company.id)
        for match, commitment, event, company in session.execute(stmt).all():
            scores = dict(match.scores or {})
            materiality = int(scores.get("materiality") or 0)
            if not is_publishable(materiality):
                below_threshold += 1
                continue
            source = commitment.source or {}
            document = session.get(Document, source.get("doc_id")) if source.get("doc_id") else None
            sources = list(event.sources or [])
            articles = session.execute(select(Article.press, Article.title).where(Article.id.in_(sources))).all() if sources else []
            presses = presses_of([press for press, _ in articles])
            targets.append(
                PublishTarget(
                    match_id=match.id,
                    company_id=company.id,
                    confirmed=bool(event.confirmed),
                    materiality=materiality,
                    event_id=int(event.id),
                    confidence=int(scores.get("confidence") or 0),
                    named_source=has_named_source([title for _, title in articles], company_terms(company.name, company.aliases)),
                    source_count=len(sources),
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
        existing = _existing_alert_counts(session, {target.event_id for target in targets})
    return TargetSet(label=label, targets=targets, below_threshold=below_threshold, existing_alerts=existing)


def _insert_alert(values: dict) -> bool:
    """match_id UNIQUE — 이미 있으면 건너뛴다(idempotent). 넣었으면 True."""
    from sqlalchemy.dialects.postgresql import insert

    from esg_watchdog.db import SessionLocal
    from esg_watchdog.models import Alert

    with SessionLocal() as session:
        result = session.execute(insert(Alert).values(**values).on_conflict_do_nothing(index_elements=["match_id"]))
        session.commit()
        return bool(result.rowcount)


def publish_alerts(
    *,
    stock_code: str | None = None,
    client: LLMClient | None = None,
    model: str | None = None,
    now: datetime | None = None,
    per_event: int = DEFAULT_PER_EVENT,
    allow_unnamed_source: bool = False,
    log: Log = print,
) -> PublishResult:
    """경보 발행 한 번 실행. 기업 미언급 출처만 있는 사건은 제외(allow_unnamed_source 로 해제), 같은 사건은 per_event 건까지만.
    경보 단위로 예외를 격리하고 pipeline_runs 에 기록한다."""
    from sqlalchemy.exc import SQLAlchemyError

    if client is None:
        client = LLMClient()
    if model is None:
        from esg_watchdog.config import settings

        model = settings.llm_model_judge
    # 키·공급자 설정 오류는 pipeline_runs 를 만들기 전에 낸다
    provider_name = client.provider.name

    if per_event < 1:
        raise ValueError(f"--per-event 는 1 이상이어야 한다: {per_event}")
    loaded = _load_targets(stock_code)
    passed, gated = apply_source_gate(loaded.targets, allow_unnamed_source)
    targets, suppressed = suppress_same_event(passed, loaded.existing_alerts, per_event)
    log(targets_line(loaded.label, targets, loaded.below_threshold, len(suppressed), len(gated)))
    for line in (*gate_lines(gated), *suppression_lines(targets, suppressed, per_event)):
        log(line)

    run_id = start_run(STAGE)
    stats = _new_stats(
        stock_code,
        len(targets),
        loaded.below_threshold,
        len(gated),
        len(suppressed),
        per_event=per_event,
        allow_unnamed_source=allow_unnamed_source,
    )
    notes: list[str] = []
    failed: list[str] = []
    calls_before, hits_before = client.stats["calls"], client.stats["cache_hits"]
    log(f"[publish] run={run_id} provider={provider_name} model={model or '-'} prompt={PROMPT_VERSION}")

    for target in targets:
        try:
            outcome = explain_target(client, model, target, log=log)
            stats["attempts"] += outcome.attempts
            stats["regenerated"] += int(outcome.regenerated)
            for reason, count in outcome.regenerations.items():
                stats[f"regenerated_{reason}"] += count
            notes.extend(outcome.notes)
            stats["length_noted"] += sum("범위 밖" in note for note in outcome.notes)
            if outcome.explanation is None:
                stats["discarded"] += 1
                notes.append(f"폐기: {outcome.discarded}")
                log(f"  {target.label}: 폐기 — {outcome.discarded}")
                continue
            values = alert_values(target, outcome.explanation, now or now_kst())
            if not _insert_alert(values):
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
