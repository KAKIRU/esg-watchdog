from collections.abc import Sequence

from sqlalchemy import CheckConstraint
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


def check_in(column: str, values: Sequence[str], name: str) -> CheckConstraint:
    """`column IN ('a', 'b', ...)` CHECK 제약. 값 목록은 knowledge/taxonomy.py 상수를 넘긴다 (D-36)."""
    quoted = ", ".join(f"'{value}'" for value in values)
    return CheckConstraint(f"{column} IN ({quoted})", name=name)
