"""F-05 점수 적용 — 회사(또는 전체) matches 를 전부 다시 계산해 scores JSONB 에 저장 (D-15: 부분 재계산 없음).

- 대상: matches 전부(status 무관) — 회사를 주면 그 회사 공약의 matches 만. 가중치가 바뀌면 이 명령으로 전체 재계산한다.
- 입력은 matches × commitments(company → industry_key) × events(event_type · severity_signals · confirmed · source_count · thin_source).
- 산식은 scoring.score_match(순수). 여기서는 읽고 쓰기만 한다. LLM 없음.
- pipeline_runs(stage='score', trigger='manual'). 매칭 단위 예외 격리. DB import 는 함수 안에서만.
"""

from collections.abc import Callable
from dataclasses import dataclass, field

from esg_watchdog.services.runs import (
    decide_status,
    failure_note,
    finish_run,
    start_run,
)
from esg_watchdog.services.score.scoring import score_match

STAGE = "score"

Log = Callable[[str], None]


@dataclass
class ScoreInput:
    match_id: int
    industry_key: str
    event_type: str
    severity_signals: dict
    relation: str
    confirmed: bool
    llm_confidence: int
    source_count: int
    thin_source: bool

    def scores(self) -> dict:
        return score_match(
            industry_key=self.industry_key,
            event_type=self.event_type,
            severity_signals=self.severity_signals,
            relation=self.relation,
            confirmed=self.confirmed,
            llm_confidence=self.llm_confidence,
            source_count=self.source_count,
            thin_source=self.thin_source,
        )


@dataclass
class ScoreResult:
    run_id: int
    status: str
    stats: dict = field(default_factory=dict)


def _load_inputs(stock_code: str | None) -> tuple[str, list[ScoreInput]]:
    from sqlalchemy import select

    from esg_watchdog.db import SessionLocal
    from esg_watchdog.models import Commitment, Company, Event, Match

    stmt = (
        select(Match, Company.industry_key, Event)
        .join(Commitment, Commitment.id == Match.commitment_id)
        .join(Company, Company.id == Commitment.company_id)
        .join(Event, Event.id == Match.event_id)
        .order_by(Match.id)
    )
    label = "전체"
    with SessionLocal() as session:
        if stock_code:
            company = session.scalar(select(Company).where(Company.stock_code == stock_code))
            if company is None:
                raise LookupError(f"companies 에 없는 stock_code: {stock_code} — 먼저 `esg-watchdog seed-companies`")
            label = f"{company.name}({company.stock_code})"
            stmt = stmt.where(Company.id == company.id)
        rows = session.execute(stmt).all()
    inputs = [
        ScoreInput(
            match_id=match.id,
            industry_key=industry_key,
            event_type=event.event_type,
            severity_signals=dict(event.severity_signals or {}),
            relation=match.relation,
            confirmed=bool(event.confirmed),
            llm_confidence=match.llm_confidence,
            source_count=event.source_count,
            thin_source=bool(event.thin_source),
        )
        for match, industry_key, event in rows
    ]
    return label, inputs


def apply_scores(*, stock_code: str | None = None, log: Log = print) -> ScoreResult:
    """점수 전체 재계산 한 번 실행. 매칭 단위로 예외를 격리하고 pipeline_runs 에 기록한다."""
    from sqlalchemy import update
    from sqlalchemy.exc import SQLAlchemyError

    from esg_watchdog.db import SessionLocal
    from esg_watchdog.models import Match

    label, inputs = _load_inputs(stock_code)
    run_id = start_run(STAGE)
    stats: dict = {"company": stock_code or "all", "matches": len(inputs), "scored": 0, "materiality_zero": 0, "errors": []}
    failed: list[str] = []
    log(f"[score] run={run_id} {label}: matches {len(inputs)}건 전체 재계산 (D-15)")

    with SessionLocal() as session:
        for item in inputs:
            try:
                scores = item.scores()
                session.execute(update(Match).where(Match.id == item.match_id).values(scores=scores))
                stats["scored"] += 1
                if scores["materiality"] == 0:
                    stats["materiality_zero"] += 1
                log(
                    f"  match {item.match_id}: {item.relation} materiality={scores['materiality']} confidence={scores['confidence']} "
                    f"components={scores['components']}"
                )
            except (ValueError, TypeError, KeyError, SQLAlchemyError) as exc:
                failed.append(f"match {item.match_id}")
                stats["errors"].append({"match": item.match_id, "error": f"{type(exc).__name__}: {exc}"})
                log(f"ERROR match {item.match_id}: {type(exc).__name__}: {exc}")
        session.commit()

    status = decide_status(len(inputs) - len(failed), len(failed)) if inputs else "success"
    finish_run(run_id, status, stats, failure_note(failed))
    return ScoreResult(run_id=run_id, status=status, stats=stats)
