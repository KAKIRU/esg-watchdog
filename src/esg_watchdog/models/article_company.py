from sqlalchemy import BigInteger, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column

from esg_watchdog.models.base import Base


class ArticleCompany(Base):
    __tablename__ = "article_companies"

    article_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("articles.id", ondelete="CASCADE"), primary_key=True)
    company_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("companies.id"), primary_key=True)
    # 기사에서 실제로 맞은 회사명·별칭 (D-36)
    matched_alias: Mapped[str | None] = mapped_column(Text, nullable=True)
