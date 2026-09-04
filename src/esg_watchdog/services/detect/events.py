"""F-03 사건 탐지 — articles → events (D-05 · D-07 · D-08 · D-36 · D-39).

- 대상: 한 회사(--company)의 articles 중 status 'pending' · published_at 최근 12개월(article_companies 로 연결). --limit N.
  BATCH_SIZE(15)건씩 LLM(llm_model_extract) 배치 → is_esg_event 이고 is_subject 인 것만 사건 후보.
- 인용 검사: quote_in(evidence_quote, title + " " + description). 실패(또는 필수 필드 누락)한 기사만 힌트를 붙여 1회 재생성 →
  재실패는 폐기하고 pipeline_runs.note 에 남긴다. 스키마 검증 실패(ValueError)도 같은 재생성 1회.
- 택소노미 검증(적재 직전, DB CHECK 없음): event_type 이 EVENT_TYPES 밖이면 '기타'로 강등 · sub_tags 는 SUB_TAGS 밖 항목만 버림 ·
  confirmed_basis 가 CONFIRMED_BASES 밖이면 '없음'(그러면 confirmed 도 false, D-08). 원래 값은 note 에 남긴다.
- 중복 제거(D-07): 제목 정규화 키(기업명·별칭·공백·문장부호 제거) + event_date 같은 월 → 같은 사건. 배치 안끼리, 그리고 그 회사의
  기존 DB events(load-fixtures 씨앗 포함)와도 대조한다. 병합은 sources 에 article_id 추가 · source_count · reported_at(가장 이른 보도일) ·
  thin_source(source_count == 1) · filing_ids 만 갱신하고, 기존 행의 title · summary · evidence_quote 는 덮어쓰지 않는다.
- filing_ids(best-effort): 같은 회사 filings 중 |filed_at − reported_at| ≤ 14일이고 title 에 FILING_KEYWORDS 가 들어가면 붙인다.
- 처리한 기사는 사건이 아니어도 status 'processed'. 배치 단위 예외 격리(실패 배치의 기사는 pending 유지).
- 실행 전 "기사 N건 → LLM 배치 M회" 를 출력하고, --limit 없이 N > CONFIRM_LIMIT(200) 이면 --yes 없이는 실행하지 않는다.
- pipeline_runs(stage='detect', trigger='manual'). DB·settings import 는 함수 안에서만 — .env 없이 import 가능.
"""

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta

from esg_watchdog.knowledge.taxonomy import CONFIRMED_BASES, EVENT_TYPES, SUB_TAGS
from esg_watchdog.llm.client import LLMClient
from esg_watchdog.llm.quotes import quote_in
from esg_watchdog.prompts.event_detect import (
    BATCH_SIZE,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    ArticleInput,
    ArticleJudgement,
    EventBatch,
    build_user_prompt,
)
from esg_watchdog.services.runs import (
    KST,
    decide_status,
    failure_note,
    finish_run,
    now_kst,
    start_run,
)

STAGE = "detect"
WINDOW_DAYS = 365
CONFIRM_LIMIT = 200
FILING_WINDOW_DAYS = 14
FILING_KEYWORDS = ("소송", "과징금", "제재", "재해", "사고", "화재", "영업정지", "횡령", "배임", "제소")
# load-fixtures --merge 로 먼저 들어온 씨앗 사건. 사람이 쓴 title·summary·evidence_quote 는 덮어쓰지 않는다
SEED_PREFIXES = ("seed-", "fixture-")
FALLBACK_EVENT_TYPE = "기타"
NO_BASIS = "없음"
EMPTY_SIGNALS = {"fine_amount": None, "casualties": 0, "lawsuit": False, "is_repeat": False, "regulator": None}

_KEEP_RE = re.compile(r"[^0-9a-z가-힣]+")
# 제목 앞의 괄호 표식 — 씨앗의 "(임시)", 기사의 "[단독]" "[속보]" "(종합)" — 은 사건 식별에 안 쓴다
_LEADING_MARK_RE = re.compile(r"^\s*(?:\([^)]*\)|\[[^\]]*\]|【[^】]*】)\s*")

Log = Callable[[str], None]


# --------------------------------------------------------------------------- 자료형
@dataclass
class CompanyTarget:
    id: int
    stock_code: str
    name: str
    aliases: list[str]

    @property
    def label(self) -> str:
        return f"{self.name}({self.stock_code})"

    @property
    def names(self) -> list[str]:
        return list(dict.fromkeys([self.name, *self.aliases]))


@dataclass
class EventCandidate:
    """인용·택소노미 검사를 통과한 사건 후보(기사 하나)."""

    article_id: int
    published_at: date
    category: str
    sub_tags: list[str]
    event_type: str
    title: str
    summary: str
    event_date: date
    date_precision: str
    severity_signals: dict
    via_subsidiary: bool
    confirmed: bool
    confirmed_basis: str
    is_retrospective: bool
    evidence_quote: str


@dataclass
class ExistingEvent:
    """그 회사의 DB events(씨앗 포함) 또는 이번 실행에서 넣은 사건 — 중복 대조용."""

    id: int
    title: str
    event_date: date
    sources: list[int]
    reported_at: date | None
    prompt_version: str
    filing_ids: list[int] = field(default_factory=list)

    @property
    def is_seed(self) -> bool:
        return self.prompt_version.startswith(SEED_PREFIXES)


@dataclass
class FilingRow:
    id: int
    filed_at: date
    title: str


@dataclass
class BatchOutcome:
    candidates: list[EventCandidate] = field(default_factory=list)
    discarded: list[str] = field(default_factory=list)
    taxonomy_notes: list[str] = field(default_factory=list)
    judged: int = 0
    not_event: int = 0
    not_subject: int = 0
    missing: int = 0
    attempts: int = 0
    regenerated: bool = False


@dataclass
class NewEvent:
    first: EventCandidate
    article_ids: list[int]
    reported_at: date


@dataclass
class MergeOp:
    event: ExistingEvent
    article_ids: list[int]
    reported_at: date


# --------------------------------------------------------------------------- 순수 함수
def normalize_title_key(title: str, names: Sequence[str]) -> str:
    """앞 괄호 표식 제거 → 기업명·별칭 제거 → 소문자 → 한글·영숫자만 남긴다(공백·문장부호 제거)."""
    text = title
    while True:
        stripped = _LEADING_MARK_RE.sub("", text, count=1)
        if stripped == text:
            break
        text = stripped
    text = text.casefold()
    for name in sorted((n for n in names if n), key=len, reverse=True):
        text = text.replace(name.casefold(), "")
    return _KEEP_RE.sub("", text)


def dedup_key(title: str, event_date: date, names: Sequence[str]) -> tuple[str, int, int]:
    """(정규화 제목, 연, 월) — 같으면 같은 사건 (D-07)."""
    return normalize_title_key(title, names), event_date.year, event_date.month


def sanitize_taxonomy(
    article_id: int,
    event_type: str | None,
    sub_tags: Sequence[str],
    confirmed: bool,
    confirmed_basis: str | None,
) -> tuple[str, list[str], bool, str, list[str]]:
    """DB CHECK 가 없는 세 값을 taxonomy 상수로 검증한다. 목록 밖 값은 강등·삭제하고 note 를 남긴다."""
    notes: list[str] = []
    kept_type = event_type or ""
    if kept_type not in EVENT_TYPES:
        notes.append(f"article {article_id}: event_type {kept_type!r} → {FALLBACK_EVENT_TYPE}")
        kept_type = FALLBACK_EVENT_TYPE

    kept_tags = list(dict.fromkeys(tag for tag in sub_tags if tag in SUB_TAGS))
    dropped = [tag for tag in sub_tags if tag not in SUB_TAGS]
    if dropped:
        notes.append(f"article {article_id}: sub_tags 제외 {dropped}")

    kept_basis = confirmed_basis or NO_BASIS
    if kept_basis not in CONFIRMED_BASES:
        notes.append(f"article {article_id}: confirmed_basis {kept_basis!r} → {NO_BASIS}")
        kept_basis = NO_BASIS
    kept_confirmed = bool(confirmed)
    if kept_confirmed and kept_basis == NO_BASIS:
        notes.append(f"article {article_id}: confirmed 인데 근거가 없어 false 로 (D-08)")
        kept_confirmed = False
    return kept_type, kept_tags, kept_confirmed, kept_basis, notes


def article_text(article: ArticleInput) -> str:
    return f"{article.title} {article.description or ''}"


def to_candidate(judgement: ArticleJudgement, article: ArticleInput) -> tuple[EventCandidate | None, str | None, list[str]]:
    """판정 → 후보. (후보, 실패 이유, 택소노미 note). 사건이 아니거나 주체가 아니면 (None, None, [])."""
    if not judgement.is_esg_event or not judgement.is_subject:
        return None, None, []

    missing = [
        name
        for name, value in (
            ("category", judgement.category),
            ("title", judgement.title),
            ("summary", judgement.summary),
            ("event_date", judgement.event_date),
            ("evidence_quote", judgement.evidence_quote),
        )
        if not value
    ]
    if missing:
        return None, f"필수 필드 누락: {', '.join(missing)}", []
    if not quote_in(judgement.evidence_quote, article_text(article)):
        return None, f"evidence_quote 가 제목·설명에 없음: {judgement.evidence_quote!r}", []

    event_type, sub_tags, confirmed, basis, notes = sanitize_taxonomy(
        article.id, judgement.event_type, judgement.sub_tags, judgement.confirmed, judgement.confirmed_basis
    )
    signals = dict(EMPTY_SIGNALS)
    if judgement.severity_signals is not None:
        signals.update(judgement.severity_signals.model_dump())
    candidate = EventCandidate(
        article_id=article.id,
        published_at=article.published_at,
        category=judgement.category,
        sub_tags=sub_tags,
        event_type=event_type,
        title=" ".join(judgement.title.split()),
        summary=judgement.summary.strip(),
        event_date=judgement.event_date,
        date_precision=judgement.date_precision or "day",
        severity_signals=signals,
        via_subsidiary=judgement.via_subsidiary,
        confirmed=confirmed,
        confirmed_basis=basis,
        is_retrospective=judgement.is_retrospective,
        evidence_quote=judgement.evidence_quote.strip(),
    )
    return candidate, None, notes


def _judge(client: LLMClient, model: str, company: CompanyTarget, articles: Sequence[ArticleInput], **hints) -> EventBatch:
    return client.complete(
        EventBatch,
        SYSTEM_PROMPT,
        build_user_prompt(company.name, company.aliases, articles, **hints),
        model=model,
        prompt_version=PROMPT_VERSION,
    )


def _apply_judgements(
    result: EventBatch, articles: Sequence[ArticleInput], outcome: BatchOutcome, *, count_judged: bool = True
) -> dict[int, str]:
    """판정을 후보로 바꾼다. 실패한 기사 id → 이유. 재생성 패스는 이미 judged 로 센 기사라 count_judged=False."""
    by_id = {article.id: article for article in articles}
    failed: dict[int, str] = {}
    seen: set[int] = set()
    for judgement in result.judgements:
        article = by_id.get(judgement.article_id)
        if article is None or judgement.article_id in seen:
            continue
        seen.add(judgement.article_id)
        if count_judged:
            outcome.judged += 1
        if not judgement.is_esg_event:
            outcome.not_event += 1
            continue
        if not judgement.is_subject:
            outcome.not_subject += 1
            continue
        candidate, problem, notes = to_candidate(judgement, article)
        outcome.taxonomy_notes.extend(notes)
        if candidate is None:
            failed[article.id] = problem or "알 수 없음"
        else:
            outcome.candidates.append(candidate)
    return failed


def detect_batch(
    client: LLMClient, model: str, company: CompanyTarget, articles: Sequence[ArticleInput], *, log: Log = print
) -> BatchOutcome:
    """기사 배치 하나를 판정한다. 인용·필드 실패 기사만 힌트를 붙여 1회 재생성 → 재실패 폐기."""
    outcome = BatchOutcome()
    failed: dict[int, str] = {}
    schema_error: str | None = None

    outcome.attempts += 1
    try:
        failed = _apply_judgements(_judge(client, model, company, articles), articles, outcome)
    except ValueError as exc:
        schema_error = str(exc)

    if failed or schema_error:
        outcome.regenerated = True
        outcome.attempts += 1
        retry_articles = [article for article in articles if article.id in failed] if failed else list(articles)
        log(
            "  배치 재생성 — "
            + (f"인용·필드 실패 {len(failed)}건" if failed else f"스키마 실패: {schema_error[:120]}")
        )
        try:
            hints = {"failed": failed} if failed else {"schema_error": schema_error}
            second_failed = _apply_judgements(
                _judge(client, model, company, retry_articles, **hints), retry_articles, outcome, count_judged=not failed
            )
            outcome.discarded = [f"article {article_id}: {reason}" for article_id, reason in second_failed.items()]
        except ValueError as exc:
            log(f"  재생성도 스키마 실패 — 폐기: {str(exc)[:120]}")
            outcome.discarded = [f"article {article_id}: {reason}" for article_id, reason in failed.items()] or [
                f"배치 {[article.id for article in articles]}: (스키마 실패) {str(exc)[:200]}"
            ]

    return outcome


def merge_candidates(
    candidates: Sequence[EventCandidate], existing: Sequence[ExistingEvent], names: Sequence[str]
) -> tuple[list[NewEvent], list[MergeOp]]:
    """D-07 중복 제거. 기존 DB 사건과 같은 키면 MergeOp, 배치 안에서 같은 키면 하나의 NewEvent 로 묶는다."""
    existing_by_key = {dedup_key(event.title, event.event_date, names): event for event in existing}
    new_by_key: dict[tuple[str, int, int], NewEvent] = {}
    merges: dict[int, MergeOp] = {}

    for candidate in sorted(candidates, key=lambda c: (c.published_at, c.article_id)):
        key = dedup_key(candidate.title, candidate.event_date, names)
        event = existing_by_key.get(key)
        if event is not None:
            op = merges.setdefault(event.id, MergeOp(event=event, article_ids=[], reported_at=candidate.published_at))
            if candidate.article_id not in event.sources and candidate.article_id not in op.article_ids:
                op.article_ids.append(candidate.article_id)
            op.reported_at = min(op.reported_at, candidate.published_at)
            continue
        group = new_by_key.get(key)
        if group is None:
            new_by_key[key] = NewEvent(first=candidate, article_ids=[candidate.article_id], reported_at=candidate.published_at)
        else:
            if candidate.article_id not in group.article_ids:
                group.article_ids.append(candidate.article_id)
            group.reported_at = min(group.reported_at, candidate.published_at)
    return list(new_by_key.values()), [op for op in merges.values() if op.article_ids or op.reported_at]


def match_filings(filings: Sequence[FilingRow], reported_at: date) -> list[int]:
    """|filed_at − reported_at| ≤ 14일이고 제목에 FILING_KEYWORDS 가 있는 공시 id (best-effort)."""
    return [
        filing.id
        for filing in filings
        if abs((filing.filed_at - reported_at).days) <= FILING_WINDOW_DAYS
        and any(keyword in filing.title for keyword in FILING_KEYWORDS)
    ]


def event_values(company_id: int, group: NewEvent, filing_ids: Sequence[int]) -> dict:
    """새 Event 행 값. 대표 후보(가장 이른 기사)의 제목·요약·인용을 쓴다."""
    first = group.first
    return {
        "company_id": company_id,
        "category": first.category,
        "sub_tags": first.sub_tags,
        "event_type": first.event_type,
        "title": first.title,
        "summary": first.summary,
        "event_date": first.event_date,
        "date_precision": first.date_precision,
        "reported_at": group.reported_at,
        "severity_signals": first.severity_signals,
        "is_subject": True,
        "via_subsidiary": first.via_subsidiary,
        "confirmed": first.confirmed,
        "confirmed_basis": first.confirmed_basis,
        "is_retrospective": first.is_retrospective,
        "thin_source": len(group.article_ids) == 1,
        "evidence_quote": first.evidence_quote,
        "sources": list(group.article_ids),
        "filing_ids": list(filing_ids),
        "source_count": len(group.article_ids),
        "prompt_version": PROMPT_VERSION,
    }


def merged_values(op: MergeOp, filings: Sequence[FilingRow]) -> dict:
    """기존 사건에 합칠 때 갱신하는 컬럼만. title · summary · evidence_quote 는 건드리지 않는다."""
    sources = [*op.event.sources, *op.article_ids]
    reported_at = op.reported_at if op.event.reported_at is None else min(op.event.reported_at, op.reported_at)
    filing_ids = list(dict.fromkeys([*op.event.filing_ids, *match_filings(filings, reported_at)]))
    return {
        "sources": sources,
        "source_count": len(sources),
        "reported_at": reported_at,
        "thin_source": len(sources) == 1,
        "filing_ids": filing_ids,
    }


def batches_for(count: int) -> int:
    return -(-count // BATCH_SIZE) if count else 0


# --------------------------------------------------------------------------- DB
@dataclass
class DetectResult:
    run_id: int | None
    status: str
    stats: dict = field(default_factory=dict)


def _new_stats(company: CompanyTarget, articles: int) -> dict:
    return {
        "company": company.stock_code,
        "articles": articles,
        "batches": batches_for(articles),
        "batches_failed": 0,
        "attempts": 0,
        "regenerated": 0,
        "judged": 0,
        "not_event": 0,
        "not_subject": 0,
        "missing": 0,
        "candidates": 0,
        "discarded": 0,
        "demoted": 0,
        "inserted": 0,
        "merged": 0,
        "processed": 0,
        "llm_calls": 0,
        "cache_hits": 0,
        "errors": [],
    }


def _load_company(stock_code: str) -> CompanyTarget:
    from sqlalchemy import select

    from esg_watchdog.db import SessionLocal
    from esg_watchdog.models import Company

    with SessionLocal() as session:
        company = session.scalar(select(Company).where(Company.stock_code == stock_code))
        if company is None:
            raise LookupError(f"companies 에 없는 stock_code: {stock_code} — 먼저 `esg-watchdog seed-companies`")
        return CompanyTarget(id=company.id, stock_code=company.stock_code, name=company.name, aliases=list(company.aliases or []))


def _load_articles(company_id: int, since, limit: int | None) -> list[ArticleInput]:
    from sqlalchemy import select

    from esg_watchdog.db import SessionLocal
    from esg_watchdog.models import Article, ArticleCompany

    with SessionLocal() as session:
        stmt = (
            select(Article)
            .join(ArticleCompany, ArticleCompany.article_id == Article.id)
            .where(ArticleCompany.company_id == company_id, Article.status == "pending", Article.published_at >= since)
            .order_by(Article.published_at, Article.id)
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        return [
            ArticleInput(
                id=article.id,
                title=article.title,
                description=article.description or "",
                published_at=article.published_at.astimezone(KST).date(),
                press=article.press,
            )
            for article in session.scalars(stmt).all()
        ]


def _load_existing(session, company_id: int) -> list[ExistingEvent]:
    from sqlalchemy import select

    from esg_watchdog.models import Event

    return [
        ExistingEvent(
            id=event.id,
            title=event.title,
            event_date=event.event_date,
            sources=list(event.sources or []),
            reported_at=event.reported_at,
            prompt_version=event.prompt_version,
            filing_ids=list(event.filing_ids or []),
        )
        for event in session.scalars(select(Event).where(Event.company_id == company_id).order_by(Event.id)).all()
    ]


def _load_filings(session, company_id: int) -> list[FilingRow]:
    from sqlalchemy import select

    from esg_watchdog.models import Filing

    return [
        FilingRow(id=filing.id, filed_at=filing.filed_at, title=filing.title)
        for filing in session.scalars(select(Filing).where(Filing.company_id == company_id)).all()
    ]


def _store_batch(
    session,
    company: CompanyTarget,
    articles: Sequence[ArticleInput],
    outcome: BatchOutcome,
    existing: list[ExistingEvent],
    filings: Sequence[FilingRow],
    stats: dict,
) -> None:
    from sqlalchemy import update

    from esg_watchdog.models import Article, Event

    new_events, merges = merge_candidates(outcome.candidates, existing, company.names)
    for group in new_events:
        values = event_values(company.id, group, match_filings(filings, group.reported_at))
        event = Event(**values)
        session.add(event)
        session.flush()
        existing.append(
            ExistingEvent(
                id=event.id,
                title=values["title"],
                event_date=values["event_date"],
                sources=list(values["sources"]),
                reported_at=values["reported_at"],
                prompt_version=PROMPT_VERSION,
                filing_ids=list(values["filing_ids"]),
            )
        )
        stats["inserted"] += 1
    for op in merges:
        values = merged_values(op, filings)
        session.execute(update(Event).where(Event.id == op.event.id).values(**values))
        op.event.sources = list(values["sources"])
        op.event.reported_at = values["reported_at"]
        op.event.filing_ids = list(values["filing_ids"])
        stats["merged"] += len(op.article_ids)

    ids = [article.id for article in articles]
    session.execute(update(Article).where(Article.id.in_(ids)).values(status="processed"))
    stats["processed"] += len(ids)


def detect_events(
    *,
    stock_code: str,
    limit: int | None = None,
    yes: bool = False,
    client: LLMClient | None = None,
    model: str | None = None,
    now=None,
    log: Log = print,
) -> DetectResult:
    """사건 탐지 한 번 실행. 배치 단위로 예외를 격리하고 pipeline_runs 에 기록한다."""
    from sqlalchemy.exc import SQLAlchemyError

    from esg_watchdog.db import SessionLocal

    if client is None:
        client = LLMClient()
    if model is None:
        from esg_watchdog.config import settings

        model = settings.llm_model_extract
    # 키·공급자 설정 오류는 pipeline_runs 를 만들기 전에 낸다
    provider_name = client.provider.name

    since = (now or now_kst()) - timedelta(days=WINDOW_DAYS)
    company = _load_company(stock_code)
    articles = _load_articles(company.id, since, limit)
    batches = batches_for(len(articles))
    log(f"[detect] {company.label}: 기사 {len(articles)}건 → LLM 배치 {batches}회 (since={since.date()}, limit={limit or '-'})")
    if limit is None and len(articles) > CONFIRM_LIMIT and not yes:
        log(f"기사가 {CONFIRM_LIMIT}건을 넘는다. --limit N 으로 줄이거나 --yes 로 확인하고 다시 실행하라. (실행하지 않음)")
        return DetectResult(run_id=None, status="skipped", stats={"company": stock_code, "articles": len(articles), "batches": batches})

    run_id = start_run(STAGE)
    stats = _new_stats(company, len(articles))
    notes: list[str] = []
    failed: list[str] = []
    calls_before, hits_before = client.stats["calls"], client.stats["cache_hits"]
    log(f"[detect] run={run_id} provider={provider_name} model={model or '-'} prompt={PROMPT_VERSION}")

    with SessionLocal() as session:
        existing = _load_existing(session, company.id)
        filings = _load_filings(session, company.id)
    log(f"  기존 events {len(existing)}건(씨앗 {sum(e.is_seed for e in existing)}) · filings {len(filings)}건과 대조")

    for index in range(batches):
        chunk = articles[index * BATCH_SIZE : (index + 1) * BATCH_SIZE]
        label = f"배치 {index + 1}/{batches}"
        try:
            outcome = detect_batch(client, model, company, chunk, log=log)
            stats["attempts"] += outcome.attempts
            stats["regenerated"] += int(outcome.regenerated)
            stats["judged"] += outcome.judged
            stats["not_event"] += outcome.not_event
            stats["not_subject"] += outcome.not_subject
            stats["missing"] += len(chunk) - outcome.judged
            stats["candidates"] += len(outcome.candidates)
            stats["discarded"] += len(outcome.discarded)
            stats["demoted"] += len(outcome.taxonomy_notes)
            notes.extend(f"폐기: {item}" for item in outcome.discarded)
            notes.extend(f"택소노미: {item}" for item in outcome.taxonomy_notes)
            with SessionLocal() as session:
                before = (stats["inserted"], stats["merged"])
                _store_batch(session, company, chunk, outcome, existing, filings, stats)
                session.commit()
            log(
                f"  {label}: judged={outcome.judged} not_event={outcome.not_event} not_subject={outcome.not_subject} "
                f"candidates={len(outcome.candidates)} inserted={stats['inserted'] - before[0]} merged={stats['merged'] - before[1]} "
                f"discarded={len(outcome.discarded)} demoted={len(outcome.taxonomy_notes)}"
            )
        except (ValueError, LookupError, RuntimeError, SQLAlchemyError) as exc:
            failed.append(label)
            stats["batches_failed"] += 1
            stats["errors"].append({"batch": index + 1, "articles": [article.id for article in chunk], "error": f"{type(exc).__name__}: {exc}"})
            log(f"ERROR {label}: {type(exc).__name__}: {exc}")

    stats["llm_calls"] = client.stats["calls"] - calls_before
    stats["cache_hits"] = client.stats["cache_hits"] - hits_before
    status = decide_status(batches - len(failed), len(failed)) if batches else "success"
    note_parts = [part for part in (failure_note(failed), *notes) if part]
    finish_run(run_id, status, stats, "; ".join(note_parts) or None)
    return DetectResult(run_id=run_id, status=status, stats=stats)
