from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from esg_watchdog.models.base import Base


class DocumentPage(Base):
    """보고서 페이지 텍스트 (D-36). 임베딩·chunks 는 만들지 않는다 (D-28)."""

    __tablename__ = "document_pages"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    document_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("documents.id", ondelete="CASCADE"), nullable=False)
    page_no: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("document_id", "page_no", name="uq_document_pages_document_page"),
        Index("idx_document_pages_document_page", "document_id", "page_no"),
    )
