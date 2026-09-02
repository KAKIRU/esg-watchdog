from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from esg_watchdog.knowledge.taxonomy import (
    CATEGORIES,
    COMMITMENT_STATUS,
    COMMITMENT_TYPES,
)
from esg_watchdog.models.base import Base, check_in


class Commitment(Base):
    """ESG 보고서에서 추출한 공약 (D-36). source = {"doc_id", "page", "span"}."""

    __tablename__ = "commitments"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    company_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("companies.id"), nullable=False)
    category: Mapped[str] = mapped_column(String(1), nullable=False)
    sub_tags: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default=text("'{}'::text[]"))
    commitment_type: Mapped[str] = mapped_column(Text, nullable=False)
    # 원문 인용
    commitment_text: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    metric: Mapped[str | None] = mapped_column(Text, nullable=True)
    target_value: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True)
    target_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    baseline: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # 보고서 발간일 = D-10 창 기준점
    filed_at: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'active'"))
    quarantine_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        check_in("category", CATEGORIES, "ck_commitments_category"),
        check_in("commitment_type", COMMITMENT_TYPES, "ck_commitments_commitment_type"),
        check_in("status", COMMITMENT_STATUS, "ck_commitments_status"),
        Index("idx_commitments_company_category", "company_id", "category"),
    )
