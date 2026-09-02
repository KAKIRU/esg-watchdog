from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Integer,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from esg_watchdog.knowledge.taxonomy import MATCH_STATUS, RELATIONS
from esg_watchdog.models.base import Base, check_in


class Match(Base):
    """공약-사건 매칭 (D-36). 유사도·임베딩 컬럼 없음 (D-28).

    evidence_quotes = {"commitment_quote", "event_quote"}.
    scores = {"materiality", "confidence",
              "components": {"industry_weight", "relation_coef", "severity", "confirmed_coef"}}.
    """

    __tablename__ = "matches"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    commitment_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("commitments.id"), nullable=False)
    event_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("events.id"), nullable=False)
    relation: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_quotes: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # 0~100 정수
    llm_confidence: Mapped[int] = mapped_column(Integer, nullable=False)
    gap_months: Mapped[int] = mapped_column(Integer, nullable=False)
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    scores: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        check_in("relation", RELATIONS, "ck_matches_relation"),
        check_in("status", MATCH_STATUS, "ck_matches_status"),
        CheckConstraint("llm_confidence BETWEEN 0 AND 100", name="ck_matches_llm_confidence"),
        UniqueConstraint("commitment_id", "event_id", name="uq_matches_commitment_event"),
    )
