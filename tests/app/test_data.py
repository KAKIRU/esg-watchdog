"""app/lib/data.py — DB 없이 fixture 모드로 조회 계약 v1 을 검사한다 (D-31 · D-35). 근거 링크 정렬(기업 언급 제목 우선 → 날짜순)도 여기서."""

import json
import os
import sys
from pathlib import Path

import pandas as pd
import pytest

# DATABASE_URL 이 있어도 이 테스트는 fixture 모드여야 한다. import 전에 지운다
os.environ.pop("DATABASE_URL", None)
APP_DIR = Path(__file__).resolve().parents[2] / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from lib import data


def test_fixture_mode_without_database_url():
    assert data.MODE == "fixture"
    assert data.engine is None
    assert data.status() == {"mode": "fixture", "error": None}


def test_load_rejects_unknown_table():
    with pytest.raises(ValueError):
        data._load("pg_shadow")
    with pytest.raises(ValueError):
        data._table("alerts; DROP TABLE alerts")


def test_load_keeps_only_col_columns_and_normalizes_dates():
    for table in data.TABLES:
        df = data._load(table)
        assert list(df.columns) == list(data.COL[table]), table
    alerts = data._load("alerts")
    assert str(alerts["published_at"].dt.tz) == "Asia/Seoul"
    events = data._load("events")
    assert pd.api.types.is_datetime64_any_dtype(events["event_date"])
    assert pd.api.types.is_datetime64_any_dtype(events["reported_at"])
    assert pd.api.types.is_datetime64_any_dtype(data._load("commitments")["filed_at"])
    assert pd.api.types.is_float_dtype(data._load("commitments")["target_value"])
    # JSONB · ARRAY 는 dict · list 그대로
    assert isinstance(data._load("matches")["scores"].iloc[0], dict)
    assert isinstance(events["sources"].iloc[0], list)


def test_cache_data_decorator_works_in_tests(monkeypatch):
    # 캐시가 살아 있으면 같은 인자로 두 번 불러도 파일은 한 번만 읽는다
    calls = {"n": 0}
    real_load = json.load

    def counting_load(handle):
        calls["n"] += 1
        return real_load(handle)

    monkeypatch.setattr(data.json, "load", counting_load)
    data._load.clear()
    first = data._load("companies")
    second = data._load("companies")
    assert calls["n"] == 1
    assert first is not second  # cache_data 는 복사본을 준다
    assert first.equals(second)


def test_ottogi_is_control_group_with_no_alerts():
    info = data.get_company("007310")
    assert info is not None
    assert info["company"] == {
        "stock_code": "007310",
        "name": "오뚜기",
        "industry_key": "food",
        "is_control_group": True,
    }
    assert info["company"]["is_control_group"] is True
    assert len(info["alerts"]) == 0
    assert len(info["positive_matches"]) == 0
    assert len(info["events"]) == 0
    assert len(info["commitments"]) == 2
    # 정량 먼저
    assert info["commitments"]["commitment_type"].tolist() == ["정량", "정성"]


def test_unknown_company_is_none():
    assert data.get_company("000000") is None


def test_alert_4003_fallback_none_does_not_break_detail():
    detail = data.get_alert_detail(4003)
    assert detail is not None
    assert detail["alert"]["fallback"] is None
    assert detail["alert"]["grade"] == "경고"
    assert detail["match"]["relation"] == "이행지연"
    assert detail["commitment"]["id"] == 1001
    assert detail["commitment"]["target_value"] is None
    assert detail["event"]["id"] == 2002
    assert detail["document"] == {
        "title": "KT ESG 보고서 2025",
        "fiscal_year": 2024,
        "published_at": pd.Timestamp("2025-06-30"),
    }
    assert detail["company"]["stock_code"] == "030200"


def test_get_alerts_category_g_returns_4001_and_4005():
    df = data.get_alerts({"category": ["G"]})
    assert sorted(df["id"].tolist()) == [4001, 4005]
    assert list(df.columns) == list(data.ALERT_COLUMNS)


def test_get_alerts_sorted_desc_and_all_published():
    df = data.get_alerts({})
    assert len(df) == 6
    published = df["published_at"].tolist()
    assert published == sorted(published, reverse=True)
    assert df["id"].tolist()[:2] == [4005, 4001]


def test_get_alerts_offset_limit_pages_six_as_five_plus_one():
    first = data.get_alerts({"offset": 0, "limit": 5})
    second = data.get_alerts({"offset": 5, "limit": 5})
    assert len(first) == 5
    assert len(second) == 1
    assert set(first["id"]) | set(second["id"]) == {4001, 4002, 4003, 4004, 4005, 4006}


def test_get_alerts_filters_grade_and_stock_codes():
    assert set(data.get_alerts({"grade": ["심각"]})["id"]) == {4001}
    assert set(data.get_alerts({"stock_codes": ["005610"]})["id"]) == {4002, 4004, 4006}
    assert data.get_alerts({"stock_codes": ["007310"]}).empty


def test_alert_4001_has_two_articles_both_mentioning_kt_in_date_order():
    detail = data.get_alert_detail(4001)
    assert detail is not None
    articles = detail["articles"]
    assert len(articles) == 2
    assert articles["id"].tolist() == [103, 104]  # 둘 다 제목에 KT → published_at 오름차순 (sources 순서와 같다)
    assert list(articles.columns) == list(data.ARTICLE_COLUMNS)
    assert detail["match"]["scores"]["components"]["industry_weight"] == 34


# --------------------------------------------------------------------------- 근거 링크 정렬 (D-31)
def test_companies_aliases_column_is_filled_when_fixture_lacks_it():
    companies = data._load("companies")
    assert list(companies.columns) == list(data.COL["companies"]) and "aliases" in companies.columns
    assert companies["aliases"].tolist() == [[], [], []]  # fixture 에는 없다 → 빈 배열. DB 에서는 companies.aliases
    assert data._company_terms({"name": "SPC삼립", "aliases": ["SPC삼립", "삼립", " ", None]}) == ["SPC삼립", "삼립"]
    assert data._company_terms({"name": "KT", "aliases": None}) == ["KT"]
    assert data._company_terms({"name": None}) == []


def articles_df(rows):
    df = pd.DataFrame(rows, columns=["id", "url", "press", "title", "published_at"])
    df["published_at"] = pd.to_datetime(df["published_at"], utc=True).dt.tz_convert(data.KST)
    return df


def test_sort_articles_company_mention_first_then_published_at():
    rows = [
        (1, "u1", "p", "1분기 담합 과징금 6891억원 총계", "2026-03-01T09:00:00+09:00"),
        (2, "u2", "p", "삼립 공장 사고 보도", "2026-03-05T09:00:00+09:00"),
        (3, "u3", "p", "CJ제일제당 설탕 담합 과징금", "2026-02-01T09:00:00+09:00"),
        (4, "u4", "p", "SPC삼립 과징금 부과", "2026-03-02T09:00:00+09:00"),
    ]
    ordered = data._sort_articles(articles_df(rows), ["SPC삼립", "삼립"])
    assert ordered["id"].tolist() == [4, 2, 3, 1]  # 기업 언급(날짜순) → 미언급(날짜순)
    assert list(ordered.columns) == list(data.ARTICLE_COLUMNS)
    # 기업 언급이 하나도 없으면 날짜순
    assert data._sort_articles(articles_df(rows), ["오뚜기"])["id"].tolist() == [3, 1, 4, 2]
    # 대소문자는 가리지 않는다 · 빈 DF 는 그대로
    assert data._sort_articles(articles_df([(9, "u", "p", "kt 침해사고 일지", "2026-01-01T00:00:00+09:00")]), ["KT"])["id"].tolist() == [9]
    assert data._sort_articles(articles_df([]), ["KT"]).empty


def test_alert_detail_puts_company_mentioning_article_first(monkeypatch):
    """event 2003 sources [103, 104]. 103 제목에서 KT 를 빼면 104 가 앞으로 온다. 둘 다 빼면 날짜순 [103, 104]."""
    real_table = data._table

    def without_kt(ids):
        def table(name):
            df = real_table(name)
            if name == "articles":
                df = df.copy()
                df.loc[df["id"].isin(ids), "title"] = "1분기 담합 과징금 총계 (기업 미언급)"
            return df

        return table

    monkeypatch.setattr(data, "_table", without_kt({103}))
    detail = data.get_alert_detail(4001)
    assert detail["articles"]["id"].tolist() == [104, 103]
    monkeypatch.setattr(data, "_table", without_kt({103, 104}))
    detail = data.get_alert_detail(4001)
    assert detail["articles"]["id"].tolist() == [103, 104]  # 날짜순 (14:10 → 16:45)
    assert detail["company"]["aliases"] == []


def test_alert_4005_event_is_retrospective():
    detail = data.get_alert_detail(4005)
    assert detail is not None
    assert detail["event"]["is_retrospective"] is True
    assert detail["event"]["date_precision"] == "month"
    assert detail["match"]["gap_months"] == 13


def test_unknown_alert_is_none():
    assert data.get_alert_detail(9999) is None
    assert data.get_alert_detail(None) is None


def test_company_alerts_and_positive_badge_source():
    info = data.get_company("030200")
    assert info is not None
    assert set(info["alerts"]["id"]) == {4001, 4003, 4005}
    assert "commitment_id" in info["alerts"].columns
    assert info["positive_matches"]["commitment_id"].tolist() == [1003]
    assert info["events"]["event_date"].is_monotonic_decreasing
