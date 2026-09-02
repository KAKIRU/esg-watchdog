from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from esg_watchdog.knowledge.taxonomy import CATEGORIES, DATE_PRECISIONS
from esg_watchdog.models.base import Base, check_in


class Event(Base):
    """뉴스·공시에서 탐지한 사건 (D-36).

    severity_signals 키: fine_amount · casualties · lawsuit · is_repeat · regulator.
    sources 는 article_id 배열, filing_ids 는 filing id 배열.
    """

    __tablename__ = "events"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    company_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("companies.id"), nullable=False)
    category: Mapped[str] = mapped_column(String(1), nullable=False)
    sub_tags: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default=text("'{}'::text[]"))
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    event_date: Mapped[date] = mapped_column(Date, nullable=False)
    date_precision: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'day'"))
    # 최초 보도일. 소급 사건(is_retrospective)의 gap_months 기준 (D-10)
    reported_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    severity_signals: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    is_subject: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    via_subsidiary: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    confirmed: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    confirmed_basis: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_retrospective: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    thin_source: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    evidence_quote: Mapped[str] = mapped_column(Text, nullable=False)
    sources: Mapped[list[int]] = mapped_column(ARRAY(BigInteger), nullable=False, server_default=text("'{}'::bigint[]"))
    filing_ids: Mapped[list[int]] = mapped_column(ARRAY(BigInteger), nullable=False, server_default=text("'{}'::bigint[]"))
    source_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        check_in("category", CATEGORIES, "ck_events_category"),
        check_in("date_precision", DATE_PRECISIONS, "ck_events_date_precision"),
        Index("idx_events_company_category_date", "company_id", "category", "event_date"),
    )
