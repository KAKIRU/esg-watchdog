"""F-04 후보 선정 — 같은 기업 · 같은 category · sub_tags 교집합 · 0 < gap_months ≤ 24 · 공약당 상한 (D-10 · D-11).

실측(2026-09-02, 실데이터): 카테고리 일치만으로 3사 2,563건(KT S 1647 · SPC삼립 S 588 · 오뚜기 S 150 · KT G 112 · 오뚜기 E 66) —
"정보보호 공약 × 산업재해 사건" 같은 무의미한 쌍이 전부 들어왔다. 판정 품질 기준(D-11)은 그대로 두고 "무엇을 판정 대상으로 삼을지"만
3단계 깔때기로 좁힌다:
  ① 카테고리 일치: commitments(status 'active') × events(is_subject) where 같은 company_id · 같은 category · 0 < gap ≤ 24 ·
     matches 에 (commitment_id, event_id) 없음('무관' 도 저장돼 있어 재판정하지 않는다). 이 단계는 SQL count 로만 센다.
  ② sub_tags 교집합: commitments.sub_tags 와 events.sub_tags 가 한 개 이상 겹칠 때만. 둘 중 하나가 빈 배열이면 ① 만으로 후보
     (태그 누락으로 사건을 잃지 않기 위함). 판정은 SQL 배열 연산자(&&)·cardinality 로 한다 — ①을 파이썬으로 끌어오면 2,500행을
     메모리에 올린다. 행은 ② 를 통과한 것만 가져온다.
  ③ 공약당 상한(per_commitment, 기본 5): 한 공약의 후보가 상한을 넘으면 confirmed=true 우선 → source_count 내림차순 → 기준일 최신순
     으로 남기고 나머지를 자른다(최신순만 쓰면 보도가 많고 확정된 핵심 사건이 잘려나간다). 잘린 수는 truncated.
     limit(전체 상한)은 같은 우선순위로 회사 전체에서 자른다. 잘린 수는 limited.
- 기준일 = events.event_date, 단 is_retrospective 면 reported_at(없으면 event_date) — D-10.
- gap_months = (기준일 − filed_at) 을 (연차×12 + 월차) 로. 일 단위는 무시한다(6/30 → 7/1 도 1개월). SQL(gap_sql) 과 파이썬(gap_months) 이
  같은 식이다 — ① 을 세는 count 와 ② 의 행 조회가 같은 창을 본다.
- DB import 는 함수 안에서만 — .env 없이 import 가능. 순수 함수(pair_candidates · select_candidates)는 같은 규칙을 DB 없이 적용한다(테스트).
"""

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date

from esg_watchdog.prompts.relation_judge import CommitmentInput, EventInput

MAX_GAP_MONTHS = 24
DEFAULT_PER_COMMITMENT = 5


@dataclass
class Candidate:
    commitment: CommitmentInput
    event: EventInput
    gap_months: int

    @property
    def label(self) -> str:
        return f"commitment {self.commitment.id} × event {self.event.id}"

    @property
    def reference(self) -> date:
        return reference_date(self.event.event_date, self.event.reported_at, self.event.is_retrospective)


@dataclass
class Funnel:
    """3단계 후보 수. by_category(①) → by_tags(②) → kept(③). truncated 는 공약당 상한, limited 는 전체 상한에 잘린 수."""

    by_category: int = 0
    by_tags: int = 0
    truncated: int = 0
    limited: int = 0
    kept: int = 0

    def add(self, other: "Funnel") -> None:
        self.by_category += other.by_category
        self.by_tags += other.by_tags
        self.truncated += other.truncated
        self.limited += other.limited
        self.kept += other.kept


@dataclass
class CandidateSet:
    """한 회사의 최종 후보(③ 통과)와 카테고리별 깔때기 수."""

    candidates: list[Candidate] = field(default_factory=list)
    per_category: dict[str, Funnel] = field(default_factory=dict)

    @property
    def total(self) -> Funnel:
        total = Funnel()
        for funnel in self.per_category.values():
            total.add(funnel)
        return total


# --------------------------------------------------------------------------- 순수 함수
def reference_date(event_date: date, reported_at: date | None, is_retrospective: bool) -> date:
    """gap 의 기준일. 소급 건은 최초 보도일(없으면 사건일)."""
    if is_retrospective and reported_at is not None:
        return reported_at
    return event_date


def gap_months(filed_at: date, reference: date) -> int:
    """(연차×12 + 월차). 일 단위 무시."""
    return (reference.year - filed_at.year) * 12 + (reference.month - filed_at.month)


def in_window(gap: int) -> bool:
    return 0 < gap <= MAX_GAP_MONTHS


def tags_overlap(commitment_tags: Sequence[str], event_tags: Sequence[str]) -> bool:
    """② 규칙. 한쪽이 비어 있으면 통과(태그 누락으로 사건을 잃지 않음), 둘 다 있으면 한 개 이상 겹쳐야 한다."""
    if not commitment_tags or not event_tags:
        return True
    return not set(commitment_tags).isdisjoint(event_tags)


def category_candidate(commitment: CommitmentInput, event: EventInput) -> Candidate | None:
    """① 규칙만: 같은 회사·같은 category 이고 gap 이 창 안이면 후보. 아니면 None."""
    if commitment.company_id != event.company_id or commitment.category != event.category:
        return None
    gap = gap_months(commitment.filed_at, reference_date(event.event_date, event.reported_at, event.is_retrospective))
    if not in_window(gap):
        return None
    return Candidate(commitment=commitment, event=event, gap_months=gap)


def to_candidate(commitment: CommitmentInput, event: EventInput) -> Candidate | None:
    """① + ②: category_candidate 에 sub_tags 교집합 규칙을 더한 것."""
    candidate = category_candidate(commitment, event)
    if candidate is None or not tags_overlap(commitment.sub_tags, event.sub_tags):
        return None
    return candidate


def pair_candidates(
    commitments: list[CommitmentInput], events: list[EventInput], existing_pairs: set[tuple[int, int]]
) -> list[Candidate]:
    """DB 없이 ①·② 규칙으로 후보를 만든다(테스트·검증용). 순서: commitment.id, event.id. ③ 은 select_candidates 가 한다."""
    result: list[Candidate] = []
    for commitment in sorted(commitments, key=lambda c: c.id):
        for event in sorted(events, key=lambda e: e.id):
            if (commitment.id, event.id) in existing_pairs:
                continue
            candidate = to_candidate(commitment, event)
            if candidate is not None:
                result.append(candidate)
    return result


def rank_key(candidate: Candidate) -> tuple[bool, int, int, int]:
    """③ 우선순위(작을수록 먼저 남는다): confirmed=true → source_count 내림차순 → 기준일 최신순 → event.id."""
    return (not candidate.event.confirmed, -candidate.event.source_count, -candidate.reference.toordinal(), candidate.event.id)


def cap_per_commitment(candidates: Iterable[Candidate], per_commitment: int | None) -> tuple[list[Candidate], int]:
    """공약마다 rank_key 순으로 per_commitment 개만 남긴다. 결과 순서: commitment.id, rank_key. (남긴 후보, 잘린 수)."""
    if per_commitment is not None and per_commitment < 1:
        raise ValueError(f"per_commitment 는 1 이상이어야 한다: {per_commitment}")
    groups: dict[int, list[Candidate]] = defaultdict(list)
    for candidate in candidates:
        groups[candidate.commitment.id].append(candidate)
    kept: list[Candidate] = []
    truncated = 0
    for commitment_id in sorted(groups):
        ranked = sorted(groups[commitment_id], key=rank_key)
        if per_commitment is not None and len(ranked) > per_commitment:
            truncated += len(ranked) - per_commitment
            ranked = ranked[:per_commitment]
        kept.extend(ranked)
    return kept, truncated


def apply_limit(candidates: Sequence[Candidate], limit: int | None) -> tuple[list[Candidate], int]:
    """전체 상한. 같은 우선순위(rank_key, 동률은 commitment.id · event.id)로 limit 개만 남기고 commitment.id · rank_key 순으로 되돌린다."""
    if limit is not None and limit < 1:
        raise ValueError(f"limit 은 1 이상이어야 한다: {limit}")
    if limit is None or len(candidates) <= limit:
        return list(candidates), 0
    top = sorted(candidates, key=lambda c: (*rank_key(c), c.commitment.id))[:limit]
    ordered = sorted(top, key=lambda c: (c.commitment.id, *rank_key(c)))
    return ordered, len(candidates) - limit


def select_candidates(
    candidates: Iterable[Candidate], *, per_commitment: int | None = DEFAULT_PER_COMMITMENT, limit: int | None = None
) -> tuple[list[Candidate], int, int]:
    """③: 공약당 상한 → 전체 상한. (남긴 후보, truncated, limited)."""
    capped, truncated = cap_per_commitment(candidates, per_commitment)
    kept, limited = apply_limit(capped, limit)
    return kept, truncated, limited


def funnel_by_category(
    by_category: dict[str, int], tagged: Sequence[Candidate], kept: Sequence[Candidate], *, per_commitment: int | None
) -> dict[str, Funnel]:
    """카테고리별 깔때기. by_category(①)는 SQL count, by_tags(②)·kept(③)는 후보 목록에서 센다. 공약은 카테고리 하나에 속하므로
    truncated 는 카테고리 그룹에 cap_per_commitment 를 다시 적용해 세고, limited = by_tags − truncated − kept."""
    funnels: dict[str, Funnel] = {category: Funnel(by_category=count) for category, count in by_category.items()}
    groups: dict[str, list[Candidate]] = defaultdict(list)
    for candidate in tagged:
        groups[candidate.commitment.category].append(candidate)
    for category, group in groups.items():
        funnel = funnels.setdefault(category, Funnel())
        funnel.by_tags = len(group)
        _, funnel.truncated = cap_per_commitment(group, per_commitment)
    for candidate in kept:
        funnels.setdefault(candidate.commitment.category, Funnel()).kept += 1
    for funnel in funnels.values():
        funnel.limited = funnel.by_tags - funnel.truncated - funnel.kept
    return dict(sorted(funnels.items()))


def commitment_input(row) -> CommitmentInput:
    return CommitmentInput(
        id=row.id,
        company_id=row.company_id,
        category=row.category,
        commitment_text=row.commitment_text,
        normalized_text=row.normalized_text,
        commitment_type=row.commitment_type,
        metric=row.metric,
        target_value=float(row.target_value) if row.target_value is not None else None,
        target_year=row.target_year,
        filed_at=row.filed_at,
        sub_tags=tuple(row.sub_tags or ()),
    )


def event_input(row) -> EventInput:
    return EventInput(
        id=row.id,
        company_id=row.company_id,
        category=row.category,
        title=row.title,
        summary=row.summary,
        evidence_quote=row.evidence_quote,
        event_date=row.event_date,
        reported_at=row.reported_at,
        is_retrospective=bool(row.is_retrospective),
        confirmed=bool(row.confirmed),
        confirmed_basis=row.confirmed_basis,
        sub_tags=tuple(row.sub_tags or ()),
        source_count=int(row.source_count or 0),
    )


# --------------------------------------------------------------------------- SQL
def gap_sql(filed_at, reference):
    """gap_months 와 같은 식의 SQL 버전: (연차×12 + 월차). EXTRACT 는 정수값을 돌려준다."""
    from sqlalchemy import extract

    return (extract("year", reference) - extract("year", filed_at)) * 12 + (extract("month", reference) - extract("month", filed_at))


def _category_conditions(company_id: int):
    """① 의 조인 조건과 WHERE 조건. (onclause, where 목록)."""
    from sqlalchemy import and_, case, exists

    from esg_watchdog.models import Commitment, Event, Match

    reference = case(
        (and_(Event.is_retrospective.is_(True), Event.reported_at.is_not(None)), Event.reported_at), else_=Event.event_date
    )
    gap = gap_sql(Commitment.filed_at, reference)
    already = exists().where(Match.commitment_id == Commitment.id, Match.event_id == Event.id)
    onclause = and_(Event.company_id == Commitment.company_id, Event.category == Commitment.category)
    where = [
        Commitment.company_id == company_id,
        Commitment.status == "active",
        Event.is_subject.is_(True),
        gap > 0,
        gap <= MAX_GAP_MONTHS,
        ~already,
    ]
    return onclause, where, reference


def _tags_condition():
    """② 의 SQL: 한쪽이 빈 배열이거나(cardinality = 0) 배열이 겹친다(&&)."""
    from sqlalchemy import func, or_

    from esg_watchdog.models import Commitment, Event

    return or_(
        func.cardinality(Commitment.sub_tags) == 0,
        func.cardinality(Event.sub_tags) == 0,
        Commitment.sub_tags.overlap(Event.sub_tags),
    )


def count_by_category_stmt(company_id: int):
    """① 카테고리 일치 후보 수를 category 별로 세는 SELECT (행을 가져오지 않는다)."""
    from sqlalchemy import func, select

    from esg_watchdog.models import Commitment, Event

    onclause, where, _reference = _category_conditions(company_id)
    return (
        select(Commitment.category, func.count())
        .select_from(Commitment)
        .join(Event, onclause)
        .where(*where)
        .group_by(Commitment.category)
        .order_by(Commitment.category)
    )


def candidate_stmt(company_id: int):
    """②(sub_tags 교집합)까지 통과한 (Commitment, Event) 행. 정렬은 commitment.id → rank_key 와 같은 순서(파이썬이 다시 정렬한다)."""
    from sqlalchemy import select

    from esg_watchdog.models import Commitment, Event

    onclause, where, reference = _category_conditions(company_id)
    return (
        select(Commitment, Event)
        .join(Event, onclause)
        .where(*where, _tags_condition())
        .order_by(Commitment.id, Event.confirmed.desc(), Event.source_count.desc(), reference.desc(), Event.id)
    )


def load_candidates(
    company_id: int, *, per_commitment: int | None = DEFAULT_PER_COMMITMENT, limit: int | None = None
) -> CandidateSet:
    """① 은 count 로만 세고, ② 를 통과한 행만 가져와 ③(공약당 상한 → 전체 상한)을 적용한다."""
    from esg_watchdog.db import SessionLocal

    with SessionLocal() as session:
        by_category = {category: int(count) for category, count in session.execute(count_by_category_stmt(company_id)).all()}
        rows = session.execute(candidate_stmt(company_id)).all()
    tagged = [to_candidate(commitment_input(commitment), event_input(event)) for commitment, event in rows]
    tagged = [candidate for candidate in tagged if candidate is not None]  # SQL 과 같은 규칙 — 항상 통과해야 한다
    kept, _truncated, _limited = select_candidates(tagged, per_commitment=per_commitment, limit=limit)
    return CandidateSet(
        candidates=kept, per_category=funnel_by_category(by_category, tagged, kept, per_commitment=per_commitment)
    )
