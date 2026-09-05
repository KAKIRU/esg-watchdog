"""조회 함수 3개 — fixture(JSON) 와 실DB(DATABASE_URL) 를 같은 코드 경로로 읽는다 (D-31 · D-35).

- app/ 는 파이프라인 패키지(src/)를 import 하지 않는다. 읽기 전용 DATABASE_URL 과 `SELECT *` 만 쓴다.
- 화면은 COL 에 나열한 컬럼만 쓴다. DB 에는 그 밖의 컬럼(created_at · prompt_version …)이 더 있지만
  _load 가 COL 로 잘라내므로 두 모드의 컬럼 집합은 같다. OPTIONAL_COL 은 fixture 에 없어도 기본값으로 채우는 컬럼(companies.aliases).
- 경보 상세의 articles 는 제목에 그 기업의 이름·별칭이 든 기사를 앞에, 그 뒤는 published_at 오름차순 (D-31). 사건 병합 키가
  (기업·카테고리·유형·연-월)이라 별칭으로 걸린 총계 기사가 sources 에 섞여 들어오는데, 화면에서는 기업이 언급된 원문부터 보인다.
- 요청 시점 LLM 호출·배치 실행 없음. 테이블을 통째로 읽어 파이썬(pandas)으로 조인한다 — 3사 규모.
- DB 접속 실패는 fixture 로 폴백하지 않는다(잘못된 데이터가 정답처럼 보이면 안 됨). 빈 DataFrame + 에러 한 줄.
"""

import json
import logging
import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st
from sqlalchemy import create_engine, text
from sqlalchemy.exc import ArgumentError

logger = logging.getLogger("esg_app.data")

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures"
KST = "Asia/Seoul"

# 읽기 대상 7테이블 (D-35). 화이트리스트 밖은 ValueError
TABLES = ("companies", "commitments", "events", "matches", "alerts", "articles", "documents")

# 이 파일이 쓰는 컬럼 전부 (테이블별). 컬럼명은 fixture JSON · 마이그레이션 3006cb2d39be 와 같다
COL: dict[str, tuple[str, ...]] = {
    "companies": ("id", "stock_code", "name", "industry_key", "is_control_group", "aliases"),
    "commitments": (
        "id", "company_id", "category", "sub_tags", "commitment_type", "commitment_text", "normalized_text",
        "metric", "target_value", "target_year", "baseline", "source", "filed_at", "status",
    ),
    "events": (
        "id", "company_id", "category", "sub_tags", "event_type", "title", "summary", "event_date",
        "date_precision", "reported_at", "severity_signals", "is_subject", "via_subsidiary", "confirmed",
        "is_retrospective", "thin_source", "evidence_quote", "sources", "filing_ids", "source_count",
    ),
    "matches": (
        "id", "commitment_id", "event_id", "relation", "rationale", "evidence_quotes", "llm_confidence",
        "gap_months", "status", "scores",
    ),
    "alerts": (
        "id", "match_id", "company_id", "grade", "headline", "explanation", "limitation", "fallback",
        "published_at", "status",
    ),
    "articles": ("id", "url", "press", "title", "published_at"),
    "documents": ("id", "company_id", "title", "fiscal_year", "published_at"),
}

# 원본에 없으면 기본값으로 채우는 컬럼. fixture companies.json 에는 aliases 가 없다(수정 금지) — DB(companies.aliases) 에는 있다
OPTIONAL_COL: dict[str, dict[str, Callable[[], Any]]] = {"companies": {"aliases": list}}
# 날짜 정규화. timestamptz 컬럼은 Asia/Seoul tz-aware, date 컬럼은 naive datetime — 두 모드 모두 같은 dtype 이 된다
TIMESTAMP_COLS = {"alerts": ("published_at",), "articles": ("published_at",)}
DATE_COLS = {"commitments": ("filed_at",), "events": ("event_date", "reported_at"), "documents": ("published_at",)}
# DB 의 Numeric 은 Decimal 로 오므로 float 으로 맞춘다
NUMERIC_COLS = {"commitments": ("target_value",)}

# get_alerts 반환 컬럼 (조회 계약 v1)
ALERT_COLUMNS = (
    "id", "headline", "grade", "published_at", "stock_code", "name", "relation", "category",
    "materiality", "confidence", "gap_months",
)
# get_company()["alerts"] 는 위 컬럼에 공약·사건 id 를 더해 준다 (현황판 배지용)
COMPANY_ALERT_COLUMNS = (*ALERT_COLUMNS, "commitment_id", "event_id")
POSITIVE_MATCH_COLUMNS = ("id", "commitment_id", "event_id", "relation", "rationale", "gap_months")
ARTICLE_COLUMNS = ("id", "url", "press", "title", "published_at")
DOCUMENT_COLUMNS = ("title", "fiscal_year", "published_at")
COMPANY_KEYS = ("stock_code", "name", "industry_key", "is_control_group")

PUBLISHED = "published"
ACTIVE = "active"
QUANTITATIVE = "정량"
POSITIVE_RELATION = "이행긍정"  # D-12: 배지만, 경보 없음


# --------------------------------------------------------------------------- engine · mode
def _psycopg_url(url: str) -> str:
    """Render 가 주는 postgres:// · postgresql:// 스킴을 psycopg 드라이버 스킴으로. 이미 +psycopg 면 그대로."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
MODE = "db" if DATABASE_URL else "fixture"
engine = None
# 마지막 읽기 실패 한 줄 (화면 상단 표시용). 성공하면 None. 스크립트 실행마다 reset_error() 로 비운다
_ERROR: str | None = None

if MODE == "db":
    try:
        engine = create_engine(_psycopg_url(DATABASE_URL), pool_pre_ping=True)
    except (ArgumentError, ImportError) as exc:  # URL 파싱 실패 · 드라이버 없음. 메시지에 URL(비밀번호)이 섞일 수 있어 타입만 남긴다
        logger.error("DATABASE_URL 로 엔진을 만들지 못했다: %s", type(exc).__name__)
        _ERROR = f"DB 엔진 생성 실패 · {type(exc).__name__}"


def status() -> dict[str, Any]:
    """화면 상단 배지용. {"mode": "db" | "fixture", "error": 한 줄 | None}."""
    return {"mode": MODE, "error": _ERROR}


def reset_error() -> None:
    """스크립트 실행 시작 시 호출. 이전 실행의 오류가 남지 않게 한다."""
    global _ERROR
    if engine is not None or MODE == "fixture":
        _ERROR = None


# --------------------------------------------------------------------------- load
def _normalize(table: str, df: pd.DataFrame) -> pd.DataFrame:
    """COL 컬럼만 남기고 날짜·숫자 dtype 을 두 모드에서 같게 맞춘다. OPTIONAL_COL 은 없으면 기본값으로 채운다."""
    for column, factory in OPTIONAL_COL.get(table, {}).items():
        if column not in df.columns:
            df = df.assign(**{column: pd.Series([factory() for _ in range(len(df))], index=df.index, dtype="object")})
    missing = [column for column in COL[table] if column not in df.columns]
    if missing:
        raise KeyError(f"{table}: 컬럼 없음 {missing}")
    df = df[list(COL[table])].copy()
    for column in TIMESTAMP_COLS.get(table, ()):
        df[column] = pd.to_datetime(df[column], utc=True).dt.tz_convert(KST)
    for column in DATE_COLS.get(table, ()):
        df[column] = pd.to_datetime(df[column])
    for column in NUMERIC_COLS.get(table, ()):
        df[column] = pd.to_numeric(df[column], errors="coerce")
    return df.reset_index(drop=True)


def _empty(table: str) -> pd.DataFrame:
    return _normalize(table, pd.DataFrame({column: pd.Series(dtype="object") for column in COL[table]}))


@st.cache_data(ttl=600, show_spinner=False)
def _load(table: str) -> pd.DataFrame:
    """테이블 하나 → DataFrame. 실패는 예외로 올린다 — 실패 결과가 600초 캐시되지 않게 예외 처리는 _table 이 맡는다."""
    if table not in TABLES:
        raise ValueError(f"허용되지 않은 테이블: {table}")
    if MODE == "db":
        if engine is None:
            raise RuntimeError("DB 엔진이 없다 (DATABASE_URL 확인)")
        # table 은 TABLES 화이트리스트에서만 온다
        df = pd.read_sql(text(f"SELECT * FROM {table}"), engine)
    else:
        with (FIXTURE_DIR / f"{table}.json").open(encoding="utf-8") as handle:
            df = pd.DataFrame(json.load(handle))
    return _normalize(table, df)


def _table(table: str) -> pd.DataFrame:
    """_load 에 예외 처리를 씌운 것. 실패하면 빈 DataFrame + _ERROR 한 줄. fixture 폴백 없음."""
    global _ERROR
    if table not in TABLES:
        raise ValueError(f"허용되지 않은 테이블: {table}")
    try:
        return _load(table)
    except Exception as exc:
        logger.exception("%s 읽기 실패 (mode=%s)", table, MODE)
        _ERROR = f"{'DB' if MODE == 'db' else 'fixture'} 읽기 실패 · {table} · {type(exc).__name__}"
        return _empty(table)


# --------------------------------------------------------------------------- helpers
def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, (list, tuple, dict, str)):
        return False
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _native(value: Any) -> Any:
    """numpy 스칼라 → 파이썬 값, 결측 → None. Timestamp 와 list/dict 는 그대로."""
    if isinstance(value, (list, tuple, dict, str)):
        return value
    if _is_missing(value):
        return None
    if isinstance(value, pd.Timestamp):
        return value
    if hasattr(value, "item"):
        return value.item()
    return value


def _row(series: pd.Series) -> dict[str, Any]:
    return {str(key): _native(value) for key, value in series.items()}


def _find(df: pd.DataFrame, row_id: Any) -> dict[str, Any]:
    """id 로 한 행을 dict 로. 없거나 id 가 결측이면 {}."""
    if _is_missing(row_id) or df.empty:
        return {}
    hit = df[df["id"] == int(row_id)]
    return {} if hit.empty else _row(hit.iloc[0])


def _score(scores: Any, key: str) -> Any:
    return scores.get(key) if isinstance(scores, dict) else None


def _alerts_view() -> pd.DataFrame:
    """alerts(published) → matches(relation · scores · gap_months) → events(category) → companies(stock_code · name)."""
    alerts = _table("alerts")
    matches = _table("matches")
    events = _table("events")
    companies = _table("companies")

    alerts = alerts[alerts["status"] == PUBLISHED]
    match_cols = matches[["id", "commitment_id", "event_id", "relation", "scores", "gap_months"]].rename(
        columns={"id": "match_id"}
    )
    event_cols = events[["id", "category"]].rename(columns={"id": "event_id"})
    company_cols = companies[["id", "stock_code", "name"]].rename(columns={"id": "company_id"})

    df = (
        alerts.merge(match_cols, on="match_id", how="left")
        .merge(event_cols, on="event_id", how="left")
        .merge(company_cols, on="company_id", how="left")
    )
    df["materiality"] = df["scores"].map(lambda scores: _score(scores, "materiality"))
    df["confidence"] = df["scores"].map(lambda scores: _score(scores, "confidence"))
    df = df.sort_values(["published_at", "id"], ascending=[False, False], kind="stable")
    return df.reset_index(drop=True)


# --------------------------------------------------------------------------- 조회 계약 v1
def get_alerts(filters: dict[str, Any] | None = None) -> pd.DataFrame:
    """발행된 경보 목록. filters: category · grade · stock_codes (list[str]) · offset · limit (int). published_at desc."""
    filters = filters or {}
    df = _alerts_view()
    for key, column in (("category", "category"), ("grade", "grade"), ("stock_codes", "stock_code")):
        values = filters.get(key)
        if values:
            df = df[df[column].isin(list(values))]
    offset = int(filters.get("offset") or 0)
    limit = filters.get("limit")
    end = offset + int(limit) if limit is not None else None
    return df.iloc[offset:end][list(ALERT_COLUMNS)].reset_index(drop=True)


def get_company(stock_code: str) -> dict[str, Any] | None:
    """기업 상세. company · commitments(active, 정량 먼저 · filed_at desc) · events(event_date desc) · alerts · positive_matches."""
    companies = _table("companies")
    hit = companies[companies["stock_code"] == stock_code]
    if hit.empty:
        return None
    company = _row(hit.iloc[0])
    company_id = company["id"]

    commitments = _table("commitments")
    commitments = commitments[commitments["company_id"] == company_id]
    active = commitments[commitments["status"] == ACTIVE].copy()
    active["_rank"] = (active["commitment_type"] != QUANTITATIVE).astype(int)
    active = active.sort_values(["_rank", "filed_at"], ascending=[True, False], kind="stable").drop(columns="_rank")

    events = _table("events")
    events = events[events["company_id"] == company_id].sort_values("event_date", ascending=False, kind="stable")

    alerts = _alerts_view()
    alerts = alerts[alerts["company_id"] == company_id][list(COMPANY_ALERT_COLUMNS)]

    matches = _table("matches")
    positive = matches[
        (matches["relation"] == POSITIVE_RELATION) & matches["commitment_id"].isin(commitments["id"])
    ][list(POSITIVE_MATCH_COLUMNS)]

    return {
        "company": {key: company.get(key) for key in COMPANY_KEYS},
        "commitments": active.reset_index(drop=True),
        "events": events.reset_index(drop=True),
        "alerts": alerts.reset_index(drop=True),
        "positive_matches": positive.reset_index(drop=True),
    }


def _company_terms(company: Mapping[str, Any]) -> list[str]:
    """제목 매칭에 쓰는 기업명 + 별칭(companies.aliases). 빈 값·중복 제거."""
    aliases = company.get("aliases")
    values = [company.get("name"), *(aliases if isinstance(aliases, (list, tuple)) else [])]
    return list(dict.fromkeys(str(value).strip() for value in values if not _is_missing(value) and str(value).strip()))


def _mentions(title: Any, terms: Sequence[str]) -> bool:
    text = str(title).casefold() if not _is_missing(title) else ""
    return any(term.casefold() in text for term in terms)


def _sort_articles(articles: pd.DataFrame, terms: Sequence[str]) -> pd.DataFrame:
    """① 제목에 기업명·별칭이 든 기사 먼저 ② published_at 오름차순. 같은 값이면 원래(sources) 순서 (D-31)."""
    if articles.empty:
        return articles
    rank = articles["title"].map(lambda title: 0 if _mentions(title, terms) else 1)
    ordered = articles.assign(_rank=rank).sort_values(["_rank", "published_at"], ascending=[True, True], kind="stable")
    return ordered.drop(columns="_rank").reset_index(drop=True)


def _articles_for(sources: Any) -> pd.DataFrame:
    """events.sources(article id 배열) 순서대로 기사 행 — 정렬 전 기준 순서. 없는 id 는 건너뛴다."""
    articles = _table("articles")
    if not isinstance(sources, (list, tuple)) or len(sources) == 0 or articles.empty:
        return articles.iloc[0:0][list(ARTICLE_COLUMNS)].reset_index(drop=True)
    ordered = articles.drop_duplicates("id").set_index("id").reindex([int(source) for source in sources])
    ordered = ordered[ordered["url"].notna()]
    return ordered.reset_index()[list(ARTICLE_COLUMNS)].reset_index(drop=True)


def get_alert_detail(alert_id: Any) -> dict[str, Any] | None:
    """경보 상세. alert · match · commitment · event · company: dict, articles: DF(기업 언급 제목 우선 → 날짜순), document: dict(title · fiscal_year · published_at)."""
    if _is_missing(alert_id):
        return None
    alert = _find(_table("alerts"), alert_id)
    if not alert:
        return None
    match = _find(_table("matches"), alert.get("match_id"))
    commitment = _find(_table("commitments"), match.get("commitment_id"))
    event = _find(_table("events"), match.get("event_id"))
    company = _find(_table("companies"), alert.get("company_id"))
    articles = _sort_articles(_articles_for(event.get("sources")), _company_terms(company))

    source = commitment.get("source")
    doc_id = source.get("doc_id") if isinstance(source, dict) else None
    document = _find(_table("documents"), doc_id)
    return {
        "alert": alert,
        "match": match,
        "commitment": commitment,
        "event": event,
        "articles": articles,
        "document": {key: document.get(key) for key in DOCUMENT_COLUMNS} if document else {},
        "company": company,
    }
