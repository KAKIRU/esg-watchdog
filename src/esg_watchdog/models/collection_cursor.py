from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from esg_watchdog.models.base import Base


class CollectionCursor(Base):
    __tablename__ = "collection_cursors"

    company_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("companies.id"), primary_key=True)
    source: Mapped[str] = mapped_column(Text, primary_key=True)
    cursor_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint("source IN ('dart_periodic', 'krx_esg_report', 'naver_news')", name="ck_collection_cursors_source"),
    )