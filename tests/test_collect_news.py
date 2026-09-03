"""services/collect/news.py — 네트워크·DB 없이 순수 함수와 슬라이스 수집 로직을 검사한다 (D-01 · D-26)."""

from datetime import datetime, timedelta
from email.utils import format_datetime
from zoneinfo import ZoneInfo

import pytest

from esg_watchdog.knowledge.keywords import ALL_KEYWORDS, COMPANIES
from esg_watchdog.services.collect import news

KST = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 9, 3, 12, 0, tzinfo=KST)
SINCE = NOW - timedelta(days=365)


# --------------------------------------------------------------------------- 텍스트 · URL
def test_clean_text_strips_tags_then_unescapes_entities():
    assert news.clean_text("<b>KT</b> &quot;해킹&quot; &lt;속보&gt;   사과") == 'KT "해킹" <속보> 사과'
    assert news.clean_text(None) == ""
    assert news.clean_text("  ") == ""


def test_normalize_url_lowercases_host_and_drops_fragment_tracking_and_trailing_slash():
    url = "HTTPS://WWW.FNnews.com/news/202607301023124019/?utm_source=naver&utm_medium=rss#top"
    assert news.normalize_url(url) == "https://www.fnnews.com/news/202607301023124019"


def test_normalize_url_keeps_article_id_query_params_in_canonical_order():
    a = news.normalize_url("https://www.sisajournal-e.com/news/articleView.html?idxno=422766&fbclid=abc")
    b = news.normalize_url("https://www.sisajournal-e.com/news/articleView.html?idxno=422766")
    other = news.normalize_url("https://www.sisajournal-e.com/news/articleView.html?idxno=422767")
    assert a == b == "https://www.sisajournal-e.com/news/articleView.html?idxno=422766"
    assert other != a
    assert news.normalize_url("https://x.kr/a?b=2&a=1") == news.normalize_url("https://x.kr/a?a=1&b=2")


def test_make_url_hash_is_sha256_of_normalized_url():
    normalized = news.normalize_url("https://www.example.com/news/1/?utm_campaign=x")
    assert news.make_url_hash(normalized) == news.make_url_hash("https://www.example.com/news/1")
    assert len(news.make_url_hash(normalized)) == 64


def test_pick_url_prefers_originallink():
    assert news.pick_url({"originallink": "https://a.kr/1", "link": "https://n.news.naver.com/1"}) == "https://a.kr/1"
    assert news.pick_url({"originallink": "", "link": "https://n.news.naver.com/1"}) == "https://n.news.naver.com/1"


def test_press_from_url_uses_host_without_www():
    assert news.press_from_url("https://www.fnnews.com/news/1") == "fnnews.com"
    assert news.press_from_url("https://n.news.naver.com/article/1") == "n.news.naver.com"


# --------------------------------------------------------------------------- 제외어 · 별칭
KT = next(spec for spec in COMPANIES if spec["stock_code"] == "030200")


def test_is_excluded_is_case_insensitive_over_title_and_description():
    assert news.is_excluded("KT&G 담배 가격 인상", "", KT["exclude_terms"])
    assert news.is_excluded("프로야구", "kt WIZ 승리", KT["exclude_terms"])
    assert not news.is_excluded("KT 해킹 사과", "개인정보 유출", KT["exclude_terms"])
    assert not news.is_excluded("KT 해킹", "", [])


def test_find_matched_alias_requires_alias_and_returns_first_match():
    assert news.find_matched_alias("KT는 해킹 사고에 사과했다", "", KT["aliases"]) == "KT"
    assert news.find_matched_alias("통신사 사고", "케이티 서버 감염", KT["aliases"]) == "케이티"
    assert news.find_matched_alias("SPC삼립 시화공장", "", ["SPC삼립", "삼립"]) == "SPC삼립"
    assert news.find_matched_alias("삼립 시화공장", "", ["SPC삼립", "삼립"]) == "삼립"
    assert news.find_matched_alias("통신 3사 해킹 점검", "정부 조사", KT["aliases"]) is None


def test_find_matched_alias_does_not_match_kt_inside_skt_or_ktx():
    assert news.find_matched_alias("SKT 해킹 사고 조사", "", ["KT"]) is None
    assert news.find_matched_alias("KTX 탈선", "", ["KT"]) is None
    assert news.find_matched_alias("(KT) 과징금", "", ["KT"]) == "KT"
    assert news.find_matched_alias("kt 소액결제", "", ["KT"]) == "KT"


# --------------------------------------------------------------------------- 쿼리 생성
def test_keywords_for_uses_knowledge_length_plus_extra_keywords():
    assert news.keywords_for("030200") == [*ALL_KEYWORDS, *KT["extra_keywords"]]
    assert news.keywords_for("007310") == list(ALL_KEYWORDS)
    assert news.keywords_for("999999") == list(ALL_KEYWORDS)


def test_build_queries_is_alias_times_keyword():
    queries = news.build_queries(["KT", "케이티"], ["해킹", "유출"])
    assert queries == ["KT 해킹", "KT 유출", "케이티 해킹", "케이티 유출"]
    total = sum(len(spec["aliases"]) * (len(ALL_KEYWORDS) + len(spec["extra_keywords"])) for spec in COMPANIES)
    assert total == sum(len(news.build_queries(s["aliases"], news.keywords_for(s["stock_code"]))) for s in COMPANIES)


# --------------------------------------------------------------------------- 슬라이스 수집 (가짜 client)
def make_item(n: int, published: datetime, title: str = "KT 해킹 속보") -> dict:
    return {
        "title": f"<b>{title}</b> {n}",
        "originallink": f"https://www.example.com/news/{n}",
        "link": f"https://n.news.naver.com/article/{n}",
        "description": "설명",
        "pubDate": format_datetime(published),
    }


class FakeNaver:
    """sort 별로 미리 정한 페이지를 돌려준다. calls 로 호출 횟수·start·sort 를 확인한다."""

    def __init__(self, pages: dict[str, list[list[dict]]], total: int):
        self.pages = pages
        self.total = total
        self.calls: list[tuple[str, int, str]] = []

    def search_news(self, query: str, display: int = 100, start: int = 1, sort: str = "date") -> dict:
        self.calls.append((query, start, sort))
        index = (start - 1) // display
        pages = self.pages.get(sort, [])
        items = pages[index] if index < len(pages) else []
        return {"total": self.total, "start": start, "display": len(items), "items": items}


def page_of(count: int, newest: datetime, step_hours: int = 1, offset: int = 0) -> list[dict]:
    return [make_item(offset + i, newest - timedelta(hours=step_hours * i)) for i in range(count)]


def no_sleep(_: float) -> None:
    return None


def test_fetch_slice_stops_when_last_item_of_page_is_before_since():
    # 1페이지 마지막 항목이 since 이전 → 2페이지를 읽지 않는다
    page1 = page_of(99, NOW) + [make_item(999, SINCE - timedelta(days=1))]
    page2 = page_of(100, SINCE - timedelta(days=2), offset=1000)
    client = FakeNaver({"date": [page1, page2]}, total=5000)

    result = news.fetch_slice(client, "KT 해킹", SINCE, max_pages=10, sleep=no_sleep)

    assert result.calls == 1
    assert result.fetched == 100
    assert not result.is_capped(SINCE)
    assert client.calls == [("KT 해킹", 1, "date")]


def test_fetch_slice_respects_start_limit_and_display():
    pages = [page_of(100, NOW - timedelta(days=i), offset=i * 100) for i in range(12)]  # 12 페이지 다 최근
    client = FakeNaver({"date": pages}, total=5000)

    result = news.fetch_slice(client, "KT 해킹", SINCE, max_pages=20, sleep=no_sleep)

    assert result.calls == 10  # start 1 → 901 까지만
    assert [start for _, start, _ in client.calls] == [1 + 100 * i for i in range(10)]
    assert result.fetched == 1000
    assert result.is_capped(SINCE)


def test_fetch_slice_sleeps_between_calls_and_stops_on_short_page():
    slept: list[float] = []
    client = FakeNaver({"date": [page_of(100, NOW), page_of(30, NOW - timedelta(days=5), offset=100)]}, total=130)

    result = news.fetch_slice(client, "KT 해킹", SINCE, max_pages=10, sleep=slept.append)

    assert result.calls == 2
    assert slept == [news.REQUEST_INTERVAL_SECONDS] * 2
    assert result.fetched == 130
    assert not result.is_capped(SINCE)  # total == fetched


def test_collect_query_retries_with_sim_when_capped_and_merges_by_normalized_url():
    date_pages = [page_of(100, NOW, offset=i * 100) for i in range(2)]
    # sim 은 겹치는 50건 + 새 50건(그중 하나는 since 이전) 을 준다
    sim_page = date_pages[0][:50] + page_of(49, NOW - timedelta(days=30), offset=5000) + [make_item(6000, SINCE - timedelta(days=3))]
    client = FakeNaver({"date": date_pages, "sim": [sim_page]}, total=3000)

    outcome = news.collect_query(client, "KT 해킹", SINCE, max_pages=2, sleep=no_sleep)

    assert outcome.sim_retried
    assert [sort for _, _, sort in client.calls] == ["date", "date", "sim", "sim"]
    assert len(outcome.items) == 250  # 200 + 50 (겹친 50 은 URL 로 제거)
    assert outcome.calls == 4
    assert not outcome.capped  # sim 이 since 이전 기사를 찾았다 → 12개월 도달


def test_collect_query_stays_capped_when_sim_cannot_reach_since():
    date_pages = [page_of(100, NOW, offset=i * 100) for i in range(2)]
    client = FakeNaver({"date": date_pages, "sim": [page_of(100, NOW - timedelta(days=10), offset=7000)]}, total=3000)

    outcome = news.collect_query(client, "KT 해킹", SINCE, max_pages=2, sleep=no_sleep)

    assert outcome.sim_retried
    assert outcome.capped
    entry = outcome.capped_entry()
    assert entry["query"] == "KT 해킹"
    assert entry["total"] == 3000
    assert entry["fetched"] == 300
    assert entry["oldest"] == (NOW - timedelta(days=10, hours=99)).date().isoformat()


def test_collect_query_does_not_retry_when_not_capped():
    client = FakeNaver({"date": [page_of(20, NOW)], "sim": [page_of(100, NOW)]}, total=20)

    outcome = news.collect_query(client, "오뚜기 리콜", SINCE, max_pages=10, sleep=no_sleep)

    assert not outcome.sim_retried
    assert not outcome.capped
    assert client.calls == [("오뚜기 리콜", 1, "date")]


def test_parse_pub_date_returns_kst_aware_datetime():
    parsed = news.parse_pub_date("Wed, 03 Sep 2026 10:00:00 +0900")
    assert parsed == datetime(2026, 9, 3, 10, 0, tzinfo=KST)
    assert parsed.utcoffset() == timedelta(hours=9)


@pytest.mark.parametrize("max_pages", [1, 10])
def test_default_max_pages_matches_api_start_limit(max_pages: int):
    assert news.DEFAULT_MAX_PAGES == 10
    assert (max_pages - 1) * news.DISPLAY + 1 <= news.START_LIMIT
