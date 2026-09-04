"""F-03 사건 탐지 — articles → events (D-05 · D-07 · D-08 · D-36 · D-39).

- 대상: 한 회사(--company)의 articles 중 status 'pending' · published_at 이 처리 창 안(article_companies 로 연결).
  창은 --since YYYY-MM-DD · --until YYYY-MM-DD(둘 다 KST 날짜, until 은 그 날 포함). 기본은 지금까지와 같다 — 최근 12개월 전체.
  사전 필터(D-01): title 에 ALL_KEYWORDS 또는 그 회사 extra_keywords(keywords_for) 중 하나가 들어간 기사만 배치에 넣는다 —
  수집은 재현율을 위해 넓게 긁었지만(회사당 최대 22,000건) 전량 판정하면 배치가 1,900회가 된다. 걸러진 기사는 status 를
  바꾸지 않는다(--all 로 필터를 끄면 다시 대상이 된다). --limit N 은 필터 통과분에 적용한다(오래된 것부터).
  BATCH_SIZE(15)건씩 LLM(llm_model_extract) 배치 → is_esg_event 이고 is_subject 인 것만 사건 후보.
- 인용 검사: quote_in(evidence_quote, title + " " + description). 실패(또는 필수 필드 누락)한 기사만 힌트를 붙여 1회 재생성 →
  재실패는 폐기하고 pipeline_runs.note 에 남긴다. 스키마 검증 실패(ValueError)도 같은 재생성 1회.
- 택소노미 검증(적재 직전, DB CHECK 없음): event_type 이 EVENT_TYPES 밖이면 '기타'로 강등 · sub_tags 는 SUB_TAGS 밖 항목만 버림 ·
  confirmed_basis 가 CONFIRMED_BASES 밖이면 '없음'(그러면 confirmed 도 false, D-08). 원래 값은 note 에 남긴다.
- 중복 제거(D-07): 키는 (company_id, category, event_type, event_date 의 연-월). 제목은 언론사마다 표현이 달라 키에 쓰지 않는다
  (실측: KT 2025-09 침해사고가 제목 키로는 288개 사건으로 쪼개졌다). 배치 안끼리, 그리고 그 회사의 기존 DB events(load-fixtures
  씨앗 포함)와도 같은 키로 대조한다. 분리 유지 예외: severity_signals.fine_amount 가 양쪽 다 있고 다르면 별개 사건 · casualties 가
  양쪽 다 0 보다 크고 다르면 별개 사건(같은 달의 다른 사고를 뭉개지 않기 위함).
  병합 규칙(MergedFields.absorb): sources 합집합 · source_count 갱신 · reported_at 최솟값 · confirmed OR(true 인 쪽의 confirmed_basis) ·
  is_retrospective OR · date_precision 은 'day' 가 하나라도 있으면 day 이고 그때 event_date 도 그 행의 값 · severity_signals 는
  필드별로 값이 있는 쪽 우선(양쪽 다 있으면 큰 값) · thin_source 는 최종 source_count 로 재계산.
  title · summary · evidence_quote 는 기존 행을 유지한다 — 특히 씨앗(prompt_version seed-* · fixture-*)은 절대 덮어쓰지 않는다.
- filing_ids(best-effort): 같은 회사 filings 중 |filed_at − reported_at| ≤ 14일이고 title 에 FILING_KEYWORDS 가 들어가면 붙인다.
- 처리한 기사는 사건이 아니어도 status 'processed'. 배치 단위 예외 격리(실패 배치의 기사는 pending 유지).
- 실행 첫 줄에 "pending N건 → 사전 필터 통과 M건 → 배치 K회 · 처리 구간: YYYY-MM-DD ~ YYYY-MM-DD" 를 출력한다 — 구간은 이번
  배치에 들어간 기사들의 published_at 최소·최대(커버리지 편중이 눈에 보이게). --limit 없이 M > CONFIRM_LIMIT(200) 이면 --yes 없이는
  실행하지 않는다(가드는 필터 통과 후 건수 기준). stage_stats 에 pending · prefiltered(제외된 수) · window · coverage 를 남긴다.
- pipeline_runs(stage='detect', trigger='manual'). DB·settings import 는 함수 안에서만 — .env 없이 import 가능.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

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
from esg_watchdog.services.collect.news import keywords_for
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
DAY = "day"
EMPTY_SIGNALS = {"fine_amount": None, "casualties": 0, "lawsuit": False, "is_repeat": False, "regulator": None}

Log = Callable[[str], None]
# (company_id, category, event_type, 연, 월)
DedupKey = tuple[int, str, str, int, int]


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
    category: str
    event_type: str
    event_date: date
    sources: list[int]
    reported_at: date | None
    prompt_version: str
    filing_ids: list[int] = field(default_factory=list)
    date_precision: str = DAY
    severity_signals: dict = field(default_factory=lambda: dict(EMPTY_SIGNALS))
    confirmed: bool = False
    confirmed_basis: str | None = NO_BASIS
    is_retrospective: bool = False

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


def has_basis(basis: str | None) -> bool:
    return bool(basis) and basis != NO_BASIS


def merge_signals(base: Mapping, extra: Mapping) -> dict:
    """severity_signals 필드별 병합 — 값이 있는 쪽 우선, 양쪽 다 있으면 큰 값(불리언은 OR). 문자열(regulator)은 기존 값을 지킨다."""
    merged = {**EMPTY_SIGNALS, **base}
    for key, value in extra.items():
        if value is None or value == "":
            continue
        current = merged.get(key)
        if current is None or current == "":
            merged[key] = value
        elif isinstance(current, bool) or isinstance(value, bool):
            merged[key] = bool(current) or bool(value)
        elif isinstance(current, (int, float)) and isinstance(value, (int, float)):
            merged[key] = max(current, value)
    return merged


@dataclass
class MergedFields:
    """병합 규칙(D-07)이 갱신하는 필드 묶음. 배치 안 병합(NewEvent)과 기존 DB 사건 병합(MergeOp)이 같은 absorb 를 쓴다."""

    event_date: date
    date_precision: str
    reported_at: date | None
    severity_signals: dict
    confirmed: bool
    confirmed_basis: str | None
    is_retrospective: bool

    @classmethod
    def from_candidate(cls, candidate: EventCandidate) -> "MergedFields":
        return cls(
            event_date=candidate.event_date,
            date_precision=candidate.date_precision,
            reported_at=candidate.published_at,
            severity_signals=merge_signals(EMPTY_SIGNALS, candidate.severity_signals),
            confirmed=candidate.confirmed,
            confirmed_basis=candidate.confirmed_basis,
            is_retrospective=candidate.is_retrospective,
        )

    @classmethod
    def from_existing(cls, event: ExistingEvent) -> "MergedFields":
        return cls(
            event_date=event.event_date,
            date_precision=event.date_precision,
            reported_at=event.reported_at,
            severity_signals=merge_signals(EMPTY_SIGNALS, event.severity_signals),
            confirmed=event.confirmed,
            confirmed_basis=event.confirmed_basis,
            is_retrospective=event.is_retrospective,
        )

    def absorb(self, candidate: EventCandidate) -> None:
        """reported_at 최솟값 · date_precision 은 day 우선(그때 event_date 도 그 행의 값) · confirmed OR(true 인 쪽의 근거) ·
        is_retrospective OR · severity_signals 필드별 병합. 이미 day 인 행의 event_date 는 바꾸지 않는다."""
        self.reported_at = candidate.published_at if self.reported_at is None else min(self.reported_at, candidate.published_at)
        if candidate.date_precision == DAY and self.date_precision != DAY:
            self.event_date = candidate.event_date
            self.date_precision = DAY
        if candidate.confirmed and not (self.confirmed and has_basis(self.confirmed_basis)):
            self.confirmed_basis = candidate.confirmed_basis
        self.confirmed = self.confirmed or candidate.confirmed
        self.is_retrospective = self.is_retrospective or candidate.is_retrospective
        self.severity_signals = merge_signals(self.severity_signals, candidate.severity_signals)


@dataclass
class NewEvent:
    first: EventCandidate
    article_ids: list[int]
    fields: MergedFields


@dataclass
class MergeOp:
    event: ExistingEvent
    article_ids: list[int]
    fields: MergedFields


# --------------------------------------------------------------------------- 순수 함수
def title_has_keyword(title: str, keywords: Sequence[str]) -> bool:
    """제목에 키워드 중 하나가 부분 문자열로 있는가(대소문자 무시)."""
    haystack = title.casefold()
    return any(keyword and keyword.casefold() in haystack for keyword in keywords)


def prefilter_articles(
    articles: Sequence[ArticleInput], keywords: Sequence[str]
) -> tuple[list[ArticleInput], list[ArticleInput]]:
    """사전 필터(D-01): (제목에 키워드가 있는 기사, 걸러진 기사). 순서는 유지한다. 걸러진 기사는 status 를 건드리지 않는다."""
    kept: list[ArticleInput] = []
    excluded: list[ArticleInput] = []
    for article in articles:
        (kept if title_has_keyword(article.title, keywords) else excluded).append(article)
    return kept, excluded


def dedup_key(company_id: int, category: str, event_type: str, event_date: date) -> DedupKey:
    """(company_id, category, event_type, 연, 월) — 같으면 같은 사건 후보 (D-07). 제목은 키에 쓰지 않는다."""
    return company_id, category, event_type, event_date.year, event_date.month


def signals_conflict(left: Mapping, right: Mapping) -> bool:
    """같은 키라도 별개 사건으로 두는 예외 — fine_amount 가 양쪽 다 있고 다르거나, casualties 가 양쪽 다 0 보다 크고 다르면."""
    fine_left, fine_right = left.get("fine_amount"), right.get("fine_amount")
    if fine_left is not None and fine_right is not None and fine_left != fine_right:
        return True
    casualties_left, casualties_right = left.get("casualties") or 0, right.get("casualties") or 0
    return casualties_left > 0 and casualties_right > 0 and casualties_left != casualties_right


def window_bounds(since: date | None, until: date | None, now: datetime) -> tuple[datetime, datetime | None]:
    """published_at 필터 경계(KST). since 없으면 now − WINDOW_DAYS. until 은 그 날짜를 포함한다(다음날 0시 미만). 상한 없으면 None."""
    lower = datetime.combine(since, time.min, tzinfo=KST) if since is not None else now - timedelta(days=WINDOW_DAYS)
    upper = datetime.combine(until + timedelta(days=1), time.min, tzinfo=KST) if until is not None else None
    if upper is not None and lower >= upper:
        raise ValueError(f"--since {lower.date()} 가 --until {until} 보다 늦다")
    return lower, upper


def coverage_of(articles: Sequence[ArticleInput]) -> tuple[date, date] | None:
    """배치에 들어간 기사들의 published_at 최소·최대 — 실제 처리 구간. 기사가 없으면 None."""
    if not articles:
        return None
    dates = [article.published_at for article in articles]
    return min(dates), max(dates)


def coverage_text(coverage: tuple[date, date] | None) -> str:
    return f"{coverage[0].isoformat()} ~ {coverage[1].isoformat()}" if coverage else "없음"


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


def _find_merge(events: Sequence[ExistingEvent], merges: dict[int, MergeOp], candidate: EventCandidate) -> MergeOp | None:
    """같은 키의 기존 사건 중 분리 예외(signals_conflict)에 걸리지 않는 첫 사건의 MergeOp — 없으면 만든다.
    이미 병합 중인 사건은 병합된 signals 로 비교한다."""
    for event in events:
        op = merges.get(event.id)
        signals = op.fields.severity_signals if op is not None else event.severity_signals
        if signals_conflict(signals, candidate.severity_signals):
            continue
        if op is None:
            op = merges[event.id] = MergeOp(event=event, article_ids=[], fields=MergedFields.from_existing(event))
        return op
    return None


def merge_candidates(
    candidates: Sequence[EventCandidate], existing: Sequence[ExistingEvent], company_id: int
) -> tuple[list[NewEvent], list[MergeOp]]:
    """D-07 중복 제거. 기존 DB 사건과 같은 키면 MergeOp, 배치 안에서 같은 키면 하나의 NewEvent 로 묶는다.
    같은 키라도 signals_conflict 면 별개 사건이다. 후보는 보도일 순으로 흡수한다(가장 이른 기사가 대표)."""
    existing_by_key: dict[DedupKey, list[ExistingEvent]] = {}
    for event in existing:
        existing_by_key.setdefault(dedup_key(company_id, event.category, event.event_type, event.event_date), []).append(event)
    new_by_key: dict[DedupKey, list[NewEvent]] = {}
    merges: dict[int, MergeOp] = {}

    for candidate in sorted(candidates, key=lambda c: (c.published_at, c.article_id)):
        key = dedup_key(company_id, candidate.category, candidate.event_type, candidate.event_date)
        op = _find_merge(existing_by_key.get(key, ()), merges, candidate)
        if op is not None:
            op.fields.absorb(candidate)
            if candidate.article_id not in op.event.sources and candidate.article_id not in op.article_ids:
                op.article_ids.append(candidate.article_id)
            continue
        group = next(
            (g for g in new_by_key.get(key, ()) if not signals_conflict(g.fields.severity_signals, candidate.severity_signals)),
            None,
        )
        if group is None:
            new_by_key.setdefault(key, []).append(
                NewEvent(first=candidate, article_ids=[candidate.article_id], fields=MergedFields.from_candidate(candidate))
            )
            continue
        group.fields.absorb(candidate)
        if candidate.article_id not in group.article_ids:
            group.article_ids.append(candidate.article_id)
    return [group for groups in new_by_key.values() for group in groups], list(merges.values())


def match_filings(filings: Sequence[FilingRow], reported_at: date) -> list[int]:
    """|filed_at − reported_at| ≤ 14일이고 제목에 FILING_KEYWORDS 가 있는 공시 id (best-effort)."""
    return [
        filing.id
        for filing in filings
        if abs((filing.filed_at - reported_at).days) <= FILING_WINDOW_DAYS
        and any(keyword in filing.title for keyword in FILING_KEYWORDS)
    ]


def event_values(company_id: int, group: NewEvent, filing_ids: Sequence[int]) -> dict:
    """새 Event 행 값. 제목·요약·인용·분류는 대표 후보(가장 이른 기사)의 것, 병합 규칙 필드는 group.fields 의 것."""
    first, fields = group.first, group.fields
    return {
        "company_id": company_id,
        "category": first.category,
        "sub_tags": first.sub_tags,
        "event_type": first.event_type,
        "title": first.title,
        "summary": first.summary,
        "event_date": fields.event_date,
        "date_precision": fields.date_precision,
        "reported_at": fields.reported_at,
        "severity_signals": fields.severity_signals,
        "is_subject": True,
        "via_subsidiary": first.via_subsidiary,
        "confirmed": fields.confirmed,
        "confirmed_basis": fields.confirmed_basis,
        "is_retrospective": fields.is_retrospective,
        "thin_source": len(group.article_ids) == 1,
        "evidence_quote": first.evidence_quote,
        "sources": list(group.article_ids),
        "filing_ids": list(filing_ids),
        "source_count": len(group.article_ids),
        "prompt_version": PROMPT_VERSION,
    }


# 병합이 기존 행에서 갱신하는 컬럼. title · summary · evidence_quote · sub_tags · via_subsidiary 는 여기 없다
MERGED_COLUMNS = (
    "sources",
    "source_count",
    "reported_at",
    "thin_source",
    "filing_ids",
    "event_date",
    "date_precision",
    "severity_signals",
    "confirmed",
    "confirmed_basis",
    "is_retrospective",
)


def merged_values(op: MergeOp, filings: Sequence[FilingRow]) -> dict:
    """기존 사건에 합칠 때 갱신하는 컬럼(MERGED_COLUMNS)만. 기존 행의 title · summary · evidence_quote 는 — 씨앗이든 아니든 — 건드리지 않는다."""
    fields = op.fields
    sources = [*op.event.sources, *op.article_ids]
    matched = match_filings(filings, fields.reported_at) if fields.reported_at is not None else []
    return {
        "sources": sources,
        "source_count": len(sources),
        "reported_at": fields.reported_at,
        "thin_source": len(sources) == 1,
        "filing_ids": list(dict.fromkeys([*op.event.filing_ids, *matched])),
        "event_date": fields.event_date,
        "date_precision": fields.date_precision,
        "severity_signals": fields.severity_signals,
        "confirmed": fields.confirmed,
        "confirmed_basis": fields.confirmed_basis,
        "is_retrospective": fields.is_retrospective,
    }


def batches_for(count: int) -> int:
    return -(-count // BATCH_SIZE) if count else 0


# --------------------------------------------------------------------------- DB
@dataclass
class DetectResult:
    run_id: int | None
    status: str
    stats: dict = field(default_factory=dict)


def _new_stats(
    company: CompanyTarget,
    articles: int,
    pending: int,
    prefiltered: int,
    *,
    window: dict | None = None,
    coverage: tuple[date, date] | None = None,
) -> dict:
    return {
        "company": company.stock_code,
        "pending": pending,
        "prefiltered": prefiltered,
        "articles": articles,
        "batches": batches_for(articles),
        "window": window or {},
        "coverage": {"from": coverage[0].isoformat(), "to": coverage[1].isoformat()} if coverage else None,
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


def _articles_stmt(company_id: int, lower: datetime, upper: datetime | None):
    """pending 기사 SELECT — published_at ≥ lower, (upper 가 있으면) < upper. 오래된 것부터."""
    from sqlalchemy import select

    from esg_watchdog.models import Article, ArticleCompany

    stmt = (
        select(Article)
        .join(ArticleCompany, ArticleCompany.article_id == Article.id)
        .where(ArticleCompany.company_id == company_id, Article.status == "pending", Article.published_at >= lower)
    )
    if upper is not None:
        stmt = stmt.where(Article.published_at < upper)
    return stmt.order_by(Article.published_at, Article.id)


def _load_articles(company_id: int, lower: datetime, upper: datetime | None) -> list[ArticleInput]:
    """그 회사의 pending 기사 전부(처리 창 안). limit 은 사전 필터 뒤에 파이썬에서 적용한다."""
    from esg_watchdog.db import SessionLocal

    with SessionLocal() as session:
        stmt = _articles_stmt(company_id, lower, upper)
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
            category=event.category,
            event_type=event.event_type,
            event_date=event.event_date,
            sources=list(event.sources or []),
            reported_at=event.reported_at,
            prompt_version=event.prompt_version,
            filing_ids=list(event.filing_ids or []),
            date_precision=event.date_precision or DAY,
            severity_signals=dict(event.severity_signals or {}),
            confirmed=bool(event.confirmed),
            confirmed_basis=event.confirmed_basis,
            is_retrospective=bool(event.is_retrospective),
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

    new_events, merges = merge_candidates(outcome.candidates, existing, company.id)
    for group in new_events:
        values = event_values(company.id, group, match_filings(filings, group.fields.reported_at))
        event = Event(**values)
        session.add(event)
        session.flush()
        existing.append(
            ExistingEvent(
                id=event.id,
                title=values["title"],
                category=values["category"],
                event_type=values["event_type"],
                event_date=values["event_date"],
                sources=list(values["sources"]),
                reported_at=values["reported_at"],
                prompt_version=PROMPT_VERSION,
                filing_ids=list(values["filing_ids"]),
                date_precision=values["date_precision"],
                severity_signals=dict(values["severity_signals"]),
                confirmed=values["confirmed"],
                confirmed_basis=values["confirmed_basis"],
                is_retrospective=values["is_retrospective"],
            )
        )
        stats["inserted"] += 1
    for op in merges:
        values = merged_values(op, filings)
        session.execute(update(Event).where(Event.id == op.event.id).values(**values))
        # 다음 배치가 병합된 상태와 대조하도록 메모리의 기존 사건도 같이 갱신한다
        for column in MERGED_COLUMNS:
            if hasattr(op.event, column):
                value = values[column]
                setattr(op.event, column, list(value) if isinstance(value, list) else dict(value) if isinstance(value, dict) else value)
        stats["merged"] += len(op.article_ids)

    ids = [article.id for article in articles]
    session.execute(update(Article).where(Article.id.in_(ids)).values(status="processed"))
    stats["processed"] += len(ids)


def detect_events(
    *,
    stock_code: str,
    limit: int | None = None,
    yes: bool = False,
    all_articles: bool = False,
    since: date | None = None,
    until: date | None = None,
    client: LLMClient | None = None,
    model: str | None = None,
    now=None,
    log: Log = print,
) -> DetectResult:
    """사건 탐지 한 번 실행. 배치 단위로 예외를 격리하고 pipeline_runs 에 기록한다. all_articles=True 면 사전 필터를 끈다.
    since · until 은 published_at(KST 날짜) 창 — 기본은 최근 12개월 전체."""
    from sqlalchemy.exc import SQLAlchemyError

    from esg_watchdog.db import SessionLocal

    if client is None:
        client = LLMClient()
    if model is None:
        from esg_watchdog.config import settings

        model = settings.llm_model_extract
    # 키·공급자 설정 오류는 pipeline_runs 를 만들기 전에 낸다
    provider_name = client.provider.name

    lower, upper = window_bounds(since, until, now or now_kst())
    window = {"since": lower.date().isoformat(), "until": until.isoformat() if until is not None else None}
    company = _load_company(stock_code)
    pending = _load_articles(company.id, lower, upper)
    if all_articles:
        passed, excluded = list(pending), []
    else:
        passed, excluded = prefilter_articles(pending, keywords_for(company.stock_code))
    articles = passed[:limit] if limit is not None else passed
    batches = batches_for(len(articles))
    coverage = coverage_of(articles)
    limit_text = f" → --limit {len(articles)}건" if limit is not None and len(articles) < len(passed) else ""
    log(
        f"[detect] {company.label}: pending {len(pending)}건 → 사전 필터 통과 {len(passed)}건{limit_text} → 배치 {batches}회 "
        f"· 처리 구간: {coverage_text(coverage)} "
        f"(since={window['since']}, until={window['until'] or '-'}, limit={limit or '-'}, 필터={'끔(--all)' if all_articles else '켬'})"
    )
    if limit is None and len(articles) > CONFIRM_LIMIT and not yes:
        log(f"필터 통과 기사가 {CONFIRM_LIMIT}건을 넘는다. --limit N 으로 줄이거나 --yes 로 확인하고 다시 실행하라. (실행하지 않음)")
        skipped = _new_stats(company, len(articles), len(pending), len(excluded), window=window, coverage=coverage)
        return DetectResult(
            run_id=None,
            status="skipped",
            stats={key: skipped[key] for key in ("company", "pending", "prefiltered", "articles", "batches", "window", "coverage")},
        )

    run_id = start_run(STAGE)
    stats = _new_stats(company, len(articles), len(pending), len(excluded), window=window, coverage=coverage)
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
