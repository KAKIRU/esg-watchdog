"""DB 없이 Base.metadata 만으로 D-36 스키마 계약을 검사한다."""

import json
from pathlib import Path

import pytest
from sqlalchemy.dialects.postgresql import JSONB

from esg_watchdog.models import Base

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "app" / "fixtures"

EXPECTED_TABLES = {
    # 기존 7
    "articles",
    "article_companies",
    "collection_cursors",
    "companies",
    "documents",
    "filings",
    "pipeline_runs",
    # 신규 5 (D-36)
    "document_pages",
    "commitments",
    "events",
    "matches",
    "alerts",
}


def test_tables_are_exactly_12():
    assert set(Base.metadata.tables) == EXPECTED_TABLES
    assert len(Base.metadata.tables) == 12


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("matches", "scores"),
        ("matches", "evidence_quotes"),
        ("events", "severity_signals"),
        ("commitments", "source"),
    ],
)
def test_contract_json_columns_are_jsonb(table: str, column: str):
    assert isinstance(Base.metadata.tables[table].c[column].type, JSONB)


def test_events_has_category():
    # E/S/G 필터는 events.category 로 건다 (D-36)
    events = Base.metadata.tables["events"]
    assert "category" in events.c
    assert events.c.category.nullable is False


def test_no_similarity_and_no_embedding_columns():
    # 임베딩·유사도 검색은 만들지 않는다 (D-28)
    assert "similarity" not in Base.metadata.tables["matches"].c
    for table in Base.metadata.tables.values():
        for column in table.c:
            assert "embedding" not in column.name, f"{table.name}.{column.name}"


@pytest.mark.parametrize(
    "name",
    ["alerts", "articles", "commitments", "companies", "documents", "events", "matches"],
)
def test_fixture_keys_are_table_columns(name: str):
    # 화면이 SQL 로 직접 읽으므로 fixture 키 철자 = 컬럼명 (D-36)
    rows = json.loads((FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))
    columns = set(Base.metadata.tables[name].c.keys())
    for row in rows:
        unknown = set(row) - columns
        assert not unknown, f"{name}.json id={row.get('id')}: {sorted(unknown)}"
