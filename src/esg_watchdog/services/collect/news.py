"""F-01 뉴스 수집 — 별칭×키워드 · 제외어 · 12개월 · URL 정규화 (D-01 · D-26).

- 대상: companies.is_active 전부, 또는 stock_code 하나.
- 쿼리 = 별칭 1개 × (ALL_KEYWORDS + 그 회사의 extra_keywords) = "{alias} {keyword}". display=100 · sort=date ·
  start 1 → max_pages(기본 10 = API 상한 start≤1000. 이 API 는 쿼리당 최대 1,000건만 준다). 호출 사이 0.1s.
- 12개월: API 에 기간 파라미터가 없어 pubDate 로 거른다. since = 오늘 − 365일.
  sort=date 에서 한 페이지의 마지막 항목이 since 이전이면 다음 페이지를 읽지 않는다.
- 상한 대응: total 이 fetched 보다 큰데 가장 오래된 pubDate 가 since 보다 뒤면 그 슬라이스는 capped(12개월을 못 채움).
  capped 슬라이스만 sort=sim 으로 같은 페이지 수를 한 번 더 돌려 합집합을 취한다. 그래도 capped 면 stage_stats.capped 에 남긴다.
- 제외: exclude_terms 가 title·description 에 있으면 버린다(대소문자 무시). 채택: alias 가 title·description 에 있어야 한다.
- 중복: 정규화 URL 의 sha256(url_hash). 같은 기사가 두 회사에 걸리면 article_companies 두 행.
- 회사 단위 예외 격리 · pipeline_runs(stage='collect_news', trigger='manual').
- 순수 함수(clean_text · normalize_url · make_url_hash · is_excluded · find_matched_alias · fetch_slice · collect_query)는
  네트워크·DB 없이 시험한다. client 는 인자로 주입한다(기본은 NaverClient).
- DB·설정·클라이언트 import 는 함수 안에서만 한다 — .env 없이 import 가능.
"""

import hashlib
import html
import re
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from functools import cache
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from esg_watchdog.knowledge.keywords import ALL_KEYWORDS, COMPANIES
from esg_watchdog.services.collect import (
    KST,
    decide_status,
    failure_note,
    finish_run,
    now_kst,
    start_run,
)

STAGE = "collect_news"

DISPLAY = 100
START_LIMIT = 1000  # API 상한: start ≤ 1000 → 쿼리당 최대 1,000건
DEFAULT_MAX_PAGES = START_LIMIT // DISPLAY  # 10
REQUEST_INTERVAL_SECONDS = 0.1  # 키당 50 RPS 한도
WINDOW_DAYS = 365

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_ASCII_WORD = re.compile(r"[A-Za-z0-9]")
# 추적용 쿼리 파라미터만 뗀다. 기사 식별자(articleView.html?idxno=…, article.html?no=…)는 남겨야 한다
_TRACKING_PARAM_PREFIXES = ("utm_",)
_TRACKING_PARAMS = frozenset({"fbclid", "gclid", "dclid", "igshid", "mc_cid", "mc_eid", "_ga", "ref_src"})

Sleep = Callable[[float], None]
Log = Callable[[str], None]


class NewsSearchClient(Protocol):
    def search_news(self, query: str, display: int = ..., start: int = ..., sort: str = ...) -> dict: ...


# --------------------------------------------------------------------------- 순수 함수
def clean_text(value: str | None) -> str:
    """HTML 태그를 벗기고 엔티티를 풀고 공백을 하나로 합친다(태그 → 엔티티 순: &lt;속보&gt; 가 태그로 먹히지 않게)."""
    if not value:
        return ""
    return _WS_RE.sub(" ", html.unescape(_TAG_RE.sub("", value))).strip()


def normalize_url(url: str) -> str:
    """소문자 host · fragment 제거 · 추적 파라미터 제거(나머지 쿼리는 정렬해 유지) · 끝 슬래시 제거.

    쿼리스트링을 통째로 지우지 않는 이유: 국내 언론사 URL 은 기사 번호가 쿼리에 있는 경우가 많다
    (…/articleView.html?idxno=422766, …/article.html?no=260513). 통째로 지우면 한 언론사의 기사가 전부 한 해시로 뭉친다.
    """
    parts = urlsplit(url.strip())
    kept = sorted(
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in _TRACKING_PARAMS and not key.lower().startswith(_TRACKING_PARAM_PREFIXES)
    )
    path = parts.path.rstrip("/")
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(kept), ""))


def make_url_hash(normalized_url: str) -> str:
    return hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()


def pick_url(item: dict) -> str:
    """originallink 우선, 없으면 link."""
    return item.get("originallink") or item["link"]


def press_from_url(normalized_url: str) -> str:
    """항목에 언론사가 없어 정규화 URL 의 host 를 쓴다. 앞의 www. 만 뗀다(www.fnnews.com → fnnews.com)."""
    host = urlsplit(normalized_url).netloc
    return host.removeprefix("www.")


def is_excluded(title: str, description: str, exclude_terms: Iterable[str]) -> bool:
    """exclude_terms 가 title 이나 description 에 있으면 True(대소문자 무시)."""
    haystack = f"{title}\n{description}".casefold()
    return any(term and term.casefold() in haystack for term in exclude_terms)


@cache
def _alias_pattern(alias: str) -> re.Pattern[str]:
    # 로마자·숫자로 시작/끝나는 별칭은 그 쪽에 로마자·숫자가 붙어 있으면 다른 낱말로 본다 (KT ≠ SKT · KTX).
    # 한글 쪽은 조사·붙여쓰기가 흔해 경계를 두지 않는다 (SPC삼립 ⊃ 삼립, "KT는" 은 KT 로 본다).
    prefix = r"(?<![A-Za-z0-9])" if _ASCII_WORD.match(alias[0]) else ""
    suffix = r"(?![A-Za-z0-9])" if _ASCII_WORD.match(alias[-1]) else ""
    return re.compile(f"{prefix}{re.escape(alias)}{suffix}", re.IGNORECASE)


def find_matched_alias(title: str, description: str, aliases: Sequence[str]) -> str | None:
    """title 또는 description 에 있는 첫 별칭(aliases 순서). 없으면 None — 채택하지 않는다."""
    haystack = f"{title}\n{description}"
    for alias in aliases:
        if alias and _alias_pattern(alias).search(haystack):
            return alias
    return None


def parse_pub_date(value: str) -> datetime:
    """RFC 2822 pubDate → KST aware datetime."""
    return parsedate_to_datetime(value).astimezone(KST)


def build_queries(aliases: Sequence[str], keywords: Sequence[str]) -> list[str]:
    return [f"{alias} {keyword}" for alias in aliases for keyword in keywords]


def keywords_for(stock_code: str) -> list[str]:
    """ALL_KEYWORDS + 그 회사의 extra_keywords(knowledge.COMPANIES — DB 컬럼이 아니다). 길이는 knowledge 의 실제 길이."""
    extra = next((spec["extra_keywords"] for spec in COMPANIES if spec["stock_code"] == stock_code), [])
    return [*ALL_KEYWORDS, *extra]


# --------------------------------------------------------------------------- 슬라이스 수집 (네트워크는 client 뒤)
@dataclass
class SliceResult:
    query: str
    sort: str
    items: list[dict]
    total: int
    calls: int
    oldest: datetime | None

    @property
    def fetched(self) -> int:
        return len(self.items)

    def is_capped(self, since: datetime) -> bool:
        """total 이 fetched 보다 크고 가장 오래된 pubDate 가 since 보다 뒤 → 12개월을 못 채웠다."""
        return self.total > self.fetched and self.oldest is not None and self.oldest > since


def fetch_slice(
    client: NewsSearchClient,
    query: str,
    since: datetime,
    max_pages: int,
    sort: str = "date",
    sleep: Sleep = time.sleep,
) -> SliceResult:
    """한 쿼리를 start 1 → max_pages 로 읽는다. sort=date 는 페이지 마지막 항목이 since 이전이면 멈춘다."""
    items: list[dict] = []
    total = 0
    calls = 0
    oldest: datetime | None = None

    for page in range(max_pages):
        start = page * DISPLAY + 1
        if start > START_LIMIT:
            break
        data = client.search_news(query=query, display=DISPLAY, start=start, sort=sort)
        calls += 1
        sleep(REQUEST_INTERVAL_SECONDS)

        total = int(data.get("total") or 0)
        page_items = data.get("items") or []
        if not page_items:
            break
        items.extend(page_items)

        page_dates = [parse_pub_date(item["pubDate"]) for item in page_items if item.get("pubDate")]
        if page_dates:
            page_oldest = min(page_dates)
            oldest = page_oldest if oldest is None else min(oldest, page_oldest)
            if sort == "date" and page_dates[-1] < since:
                break
        if len(page_items) < DISPLAY:
            break

    return SliceResult(query=query, sort=sort, items=items, total=total, calls=calls, oldest=oldest)


@dataclass
class QueryOutcome:
    query: str
    items: list[dict]  # date ∪ sim, 정규화 URL 기준 중복 제거
    total: int
    calls: int
    oldest: datetime | None
    capped: bool
    sim_retried: bool

    def capped_entry(self) -> dict:
        return {
            "query": self.query,
            "total": self.total,
            "fetched": len(self.items),
            "oldest": self.oldest.date().isoformat() if self.oldest else None,
        }


def collect_query(
    client: NewsSearchClient,
    query: str,
    since: datetime,
    max_pages: int,
    sleep: Sleep = time.sleep,
) -> QueryOutcome:
    """sort=date 로 읽고, capped 면 sort=sim 으로 한 번 더 읽어 합집합. 합쳐도 since 이전 기사가 없으면 capped 로 남긴다."""
    passes = [fetch_slice(client, query, since, max_pages, "date", sleep)]
    if passes[0].is_capped(since):
        passes.append(fetch_slice(client, query, since, max_pages, "sim", sleep))

    merged: dict[str, dict] = {}
    for result in passes:
        for item in result.items:
            merged.setdefault(normalize_url(pick_url(item)), item)

    dates = [parse_pub_date(item["pubDate"]) for item in merged.values() if item.get("pubDate")]
    oldest = min(dates) if dates else None
    total = max(result.total for result in passes)
    return QueryOutcome(
        query=query,
        items=list(merged.values()),
        total=total,
        calls=sum(result.calls for result in passes),
        oldest=oldest,
        capped=total > len(merged) and oldest is not None and oldest > since,
        sim_retried=len(passes) > 1,
    )


# --------------------------------------------------------------------------- DB 적재
@dataclass
class CompanyTarget:
    id: int
    stock_code: str
    name: str
    aliases: list[str]
    exclude_terms: list[str]

    @property
    def label(self) -> str:
        return f"{self.name}({self.stock_code})"


@dataclass
class CollectNewsResult:
    run_id: int
    status: str
    stats: dict = field(default_factory=dict)


def _new_stats() -> dict:
    return {
        "queries": 0,
        "calls": 0,
        "fetched": 0,
        "inserted": 0,
        "dup": 0,
        "excluded": 0,
        "no_alias": 0,
        "old": 0,
        "sim_retries": 0,
        "capped": [],
        "errors": [],
    }


def _load_targets(stock_code: str | None) -> list[CompanyTarget]:
    from sqlalchemy import select

    from esg_watchdog.db import SessionLocal
    from esg_watchdog.models import Company

    with SessionLocal() as session:
        stmt = select(Company).where(Company.is_active.is_(True)).order_by(Company.id)
        if stock_code:
            stmt = stmt.where(Company.stock_code == stock_code)
        companies = session.scalars(stmt).all()
        return [
            CompanyTarget(
                id=company.id,
                stock_code=company.stock_code,
                name=company.name,
                aliases=list(company.aliases) or [company.name],
                exclude_terms=list(company.exclude_terms or []),
            )
            for company in companies
        ]


def store_items(
    session,
    company: CompanyTarget,
    items: Iterable[dict],
    since: datetime,
    stats: dict,
    seen_links: set[tuple[int, int]],
) -> None:
    """항목을 articles · article_companies 에 넣는다. 오래됨 → 제외어 → 별칭 → url_hash 중복 순으로 거른다."""
    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from esg_watchdog.models import Article, ArticleCompany

    for item in items:
        pub_raw = item.get("pubDate")
        if not pub_raw:
            continue
        published_at = parse_pub_date(pub_raw)
        if published_at < since:
            stats["old"] += 1
            continue

        title = clean_text(item.get("title"))
        description = clean_text(item.get("description"))
        if not title:
            continue
        if is_excluded(title, description, company.exclude_terms):
            stats["excluded"] += 1
            continue
        alias = find_matched_alias(title, description, company.aliases)
        if alias is None:
            stats["no_alias"] += 1
            continue

        url = normalize_url(pick_url(item))
        url_hash = make_url_hash(url)
        article_id = session.scalar(select(Article.id).where(Article.url_hash == url_hash))
        if article_id is None:
            article = Article(
                url=url,
                url_hash=url_hash,
                press=press_from_url(url),
                title=title,
                description=description or None,
                body_text=None,
                body_status="partial",
                published_at=published_at,
                status="pending",
            )
            session.add(article)
            session.flush()
            article_id = article.id
            stats["inserted"] += 1
        else:
            stats["dup"] += 1

        link = (article_id, company.id)
        if link in seen_links:
            continue
        seen_links.add(link)
        session.execute(
            pg_insert(ArticleCompany)
            .values(article_id=article_id, company_id=company.id, matched_alias=alias)
            .on_conflict_do_nothing(index_elements=["article_id", "company_id"])
        )


def _describe(exc: BaseException) -> str:
    if isinstance(exc, HTTPError):
        return f"HTTP {exc.code}: {exc.reason}"
    return f"{type(exc).__name__}: {exc}"


def collect_news(
    *,
    client: NewsSearchClient | None = None,
    stock_code: str | None = None,
    max_pages: int = DEFAULT_MAX_PAGES,
    sleep: Sleep = time.sleep,
    now: datetime | None = None,
    log: Log = print,
) -> CollectNewsResult:
    """뉴스 수집 한 번 실행. 회사 단위로 예외를 격리하고 pipeline_runs 에 기록한다."""
    from sqlalchemy.exc import SQLAlchemyError

    from esg_watchdog.db import SessionLocal

    if client is None:
        from esg_watchdog.clients.naver import NaverClient

        client = NaverClient()

    max_pages = max(1, min(int(max_pages), DEFAULT_MAX_PAGES))
    since = (now or now_kst()) - timedelta(days=WINDOW_DAYS)

    targets = _load_targets(stock_code)
    if not targets:
        raise LookupError(
            f"companies 에 is_active 인 stock_code={stock_code} 가 없다" if stock_code else "companies 에 is_active 회사가 없다 — 먼저 `esg-watchdog seed-companies`"
        )

    run_id = start_run(STAGE)
    stats = _new_stats()
    seen_links: set[tuple[int, int]] = set()
    failed: list[str] = []
    log(f"[collect_news] run={run_id} since={since.date()} max_pages={max_pages} companies={len(targets)}")

    for company in targets:
        queries = build_queries(company.aliases, keywords_for(company.stock_code))
        before = dict(stats)
        current_query = ""
        try:
            for current_query in queries:
                outcome = collect_query(client, current_query, since, max_pages, sleep)
                stats["queries"] += 1
                stats["calls"] += outcome.calls
                stats["fetched"] += len(outcome.items)
                if outcome.sim_retried:
                    stats["sim_retries"] += 1
                if outcome.capped:
                    stats["capped"].append(outcome.capped_entry())
                with SessionLocal() as session:
                    store_items(session, company, outcome.items, since, stats, seen_links)
                    session.commit()
        except (HTTPError, URLError, OSError, ValueError, KeyError, RuntimeError, SQLAlchemyError) as exc:
            failed.append(company.label)
            stats["errors"].append({"company": company.stock_code, "query": current_query, "error": _describe(exc)})
            log(f"ERROR {company.label} query={current_query!r}: {_describe(exc)}")

        log(
            f"{company.label}: queries={stats['queries'] - before['queries']}/{len(queries)} "
            f"inserted={stats['inserted'] - before['inserted']} dup={stats['dup'] - before['dup']} "
            f"excluded={stats['excluded'] - before['excluded']} no_alias={stats['no_alias'] - before['no_alias']} "
            f"old={stats['old'] - before['old']} capped={len(stats['capped']) - len(before['capped'])}"
        )

    status = decide_status(len(targets) - len(failed), len(failed))
    finish_run(run_id, status, stats, failure_note(failed))
    return CollectNewsResult(run_id=run_id, status=status, stats=stats)
