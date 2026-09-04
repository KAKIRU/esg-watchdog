"""F-04 후보 선정 — 같은 기업 · 같은 category · 0 < gap_months ≤ 24 (D-10 · D-11).

- 후보 = commitments(status 'active') × events(is_subject true) where 같은 company_id AND commitments.category = events.category.
  조인·category·기존 matches 제외는 SQL(ORM) 이 하고, gap 계산은 순수 함수가 한다. LLM 은 여기 없다.
- 기준일 = events.event_date, 단 is_retrospective 면 reported_at(없으면 event_date) — D-10.
- gap_months = (기준일 − filed_at) 을 (연차×12 + 월차) 로. 일 단위는 무시한다(6/30 → 7/1 도 1개월).
- 0 < gap_months ≤ MAX_GAP_MONTHS(24) 만 채택. 이미 matches 에 (commitment_id, event_id) 가 있으면 제외 — '무관' 도 저장돼 있어
  재판정하지 않는다.
- DB import 는 함수 안에서만 — .env 없이 import 가능.
"""

from dataclasses import dataclass
from datetime import date

from esg_watchdog.prompts.relation_judge import CommitmentInput, EventInput

MAX_GAP_MONTHS = 24


@dataclass
class Candidate:
    commitment: CommitmentInput
    event: EventInput
    gap_months: int

    @property
    def label(self) -> str:
        return f"commitment {self.commitment.id} × event {self.event.id}"


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


def to_candidate(commitment: CommitmentInput, event: EventInput) -> Candidate | None:
    """같은 회사·같은 category 이고 gap 이 창 안이면 후보. 아니면 None."""
    if commitment.company_id != event.company_id or commitment.category != event.category:
        return None
    gap = gap_months(commitment.filed_at, reference_date(event.event_date, event.reported_at, event.is_retrospective))
    if not in_window(gap):
        return None
    return Candidate(commitment=commitment, event=event, gap_months=gap)


def pair_candidates(
    commitments: list[CommitmentInput], events: list[EventInput], existing_pairs: set[tuple[int, int]]
) -> list[Candidate]:
    """DB 없이 같은 규칙으로 후보를 만든다(테스트·검증용). 순서: commitment.id, event.id."""
    result: list[Candidate] = []
    for commitment in sorted(commitments, key=lambda c: c.id):
        for event in sorted(events, key=lambda e: e.id):
            if (commitment.id, event.id) in existing_pairs:
                continue
            candidate = to_candidate(commitment, event)
            if candidate is not None:
                result.append(candidate)
    return result


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
    )


# --------------------------------------------------------------------------- DB
def load_candidates(company_id: int) -> list[Candidate]:
    """SQL 조인(같은 회사·같은 category · active · is_subject · matches 에 없음) → gap 창 필터."""
    from sqlalchemy import and_, exists, select

    from esg_watchdog.db import SessionLocal
    from esg_watchdog.models import Commitment, Event, Match

    already = exists().where(Match.commitment_id == Commitment.id, Match.event_id == Event.id)
    stmt = (
        select(Commitment, Event)
        .join(Event, and_(Event.company_id == Commitment.company_id, Event.category == Commitment.category))
        .where(Commitment.company_id == company_id, Commitment.status == "active", Event.is_subject.is_(True), ~already)
        .order_by(Commitment.id, Event.id)
    )
    with SessionLocal() as session:
        rows = session.execute(stmt).all()
    candidates = [to_candidate(commitment_input(commitment), event_input(event)) for commitment, event in rows]
    return [candidate for candidate in candidates if candidate is not None]
