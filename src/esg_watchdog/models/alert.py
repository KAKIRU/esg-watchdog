from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from esg_watchdog.knowledge.taxonomy import ALERT_STATUS, GRADES
from esg_watchdog.models.base import Base, check_in


class Alert(Base):
    """발행된 경보 (D-36). 매칭 하나당 최대 하나 (match_id UNIQUE)."""

    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    match_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("matches.id"), unique=True, nullable=False)
    company_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("companies.id"), nullable=False)
    grade: Mapped[str] = mapped_column(Text, nullable=False)
    headline: Mapped[str] = mapped_column(Text, nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    limitation: Mapped[str] = mapped_column(Text, nullable=False)
    fallback: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'published'"))
    prompt_version: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        check_in("grade", GRADES, "ck_alerts_grade"),
        check_in("status", ALERT_STATUS, "ck_alerts_status"),
    )


# 회사별 최신 경보 조회용. DESC 정렬 인덱스는 __table_args__ 문자열로 못 쓰므로 클래스 밖에서 선언
Index("idx_alerts_company_published", Alert.company_id, Alert.published_at.desc())
