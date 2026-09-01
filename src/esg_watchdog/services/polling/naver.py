import hashlib
import html
import re
from datetime import datetime
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from esg_watchdog.clients.naver import NaverClient
from esg_watchdog.db import SessionLocal
from esg_watchdog.models import (
    Article,
    ArticleCompany,
    CollectionCursor,
    Company,
    PipelineRun,
)

HTML_TAG_PATTERN = re.compile(r"<[^>]+>")

NAVER_SOURCE = "naver_news"
KST = ZoneInfo("Asia/Seoul")

NAVER_DISPLAY = 100
NAVER_MAX_PAGES = 3

def clean_text(value: str) -> str:
    value = html.unescape(value)
    return HTML_TAG_PATTERN.sub("", value).strip()


def make_url_hash(url: str) -> str:
    return hashlib.sha256(
        url.encode("utf-8")
    ).hexdigest()


def poll_naver_company(
    company: Company,
    query: str,
    display: int = 100,
    max_pages: int = 3,
    since: datetime | None = None,
) -> int:
    client = NaverClient()

    inserted = 0

    for page in range(max_pages):
        start = page * display + 1

        if start > 1000:
            break

        data = client.search_news(
            query=query,
            display=display,
            start=start,
            sort="date",
        )

        items = data.get("items", [])

        if not items:
            break

        for item in items:
            url = (
                item.get("originallink")
                or item["link"]
            )

            url_hash = make_url_hash(url)

            title = clean_text(item["title"])
            description = clean_text(
                item.get("description", "")
            )

            published_at = parsedate_to_datetime(
                item["pubDate"]
            )

            if since is not None and published_at <= since:
                return inserted

            with SessionLocal() as session:
                article = session.scalar(
                    select(Article).where(
                        Article.url_hash == url_hash
                    )
                )

                if article is None:
                    article = Article(
                        url=url,
                        url_hash=url_hash,
                        press=None,
                        title=title,
                        description=description,
                        body_text=None,
                        body_status="partial",
                        published_at=published_at,
                        status="pending",
                    )

                    session.add(article)
                    session.flush()

                    inserted += 1

                association = session.scalar(
                    select(ArticleCompany).where(
                        ArticleCompany.article_id
                        == article.id,
                        ArticleCompany.company_id
                        == company.id,
                    )
                )

                if association is None:
                    session.add(
                        ArticleCompany(
                            article_id=article.id,
                            company_id=company.id,
                        )
                    )

                session.commit()

    return inserted

def get_naver_since(
    company_id: int,
) -> datetime | None:
    with SessionLocal() as session:
        cursor = session.scalar(
            select(CollectionCursor).where(
                CollectionCursor.company_id == company_id,
                CollectionCursor.source == NAVER_SOURCE,
            )
        )

        if cursor is None:
            return None

        return cursor.cursor_at


def update_naver_cursor(
    company_id: int,
    cursor_at: datetime,
) -> None:
    with SessionLocal() as session:
        cursor = session.scalar(
            select(CollectionCursor).where(
                CollectionCursor.company_id == company_id,
                CollectionCursor.source == NAVER_SOURCE,
            )
        )

        if cursor is None:
            cursor = CollectionCursor(
                company_id=company_id,
                source=NAVER_SOURCE,
                cursor_at=cursor_at,
            )
            session.add(cursor)
        else:
            cursor.cursor_at = cursor_at

        session.commit()

def poll_naver_companies(
    trigger: str = "manual",
) -> int:
    run_started_at = datetime.now(KST)

    success_count = 0
    failed_count = 0
    total_inserted = 0

    # 이번 NAVER polling 실행 기록
    with SessionLocal() as session:
        pipeline_run = PipelineRun(
            trigger=trigger,
            status="running",
            stage_stats={},
        )

        session.add(pipeline_run)
        session.commit()
        session.refresh(pipeline_run)

        pipeline_run_id = pipeline_run.id

    # 활성 회사 ID 조회
    with SessionLocal() as session:
        companies = session.scalars(
            select(Company).where(
                Company.is_active.is_(True)
            )
        ).all()

        company_ids = [
            company.id
            for company in companies
        ]

    # 회사별 polling
    for company_id in company_ids:
        try:
            with SessionLocal() as session:
                company = session.get(
                    Company,
                    company_id,
                )

                if company is None:
                    continue

                # 필요한 값은 session 닫기 전에 읽어둠
                company_name = company.name

            since = get_naver_since(company_id)

            inserted = poll_naver_company(
                company=company,
                query=company_name,
                display=NAVER_DISPLAY,
                max_pages=NAVER_MAX_PAGES,
                since=since,
            )

            # 회사 전체가 성공했을 때만 cursor 이동
            update_naver_cursor(
                company_id=company_id,
                cursor_at=run_started_at,
            )

            print(
                f"{company_name}: "
                f"since={since}, "
                f"{inserted} inserted"
            )

            total_inserted += inserted
            success_count += 1

        except (
            RuntimeError,
            OSError,
            ValueError,
            SQLAlchemyError,
        ) as exc:
            failed_count += 1

            print(
                f"ERROR company_id="
                f"{company_id}: {exc}"
            )

    if failed_count == 0:
        run_status = "success"
    elif success_count == 0:
        run_status = "failed"
    else:
        run_status = "partial"

    with SessionLocal() as session:
        pipeline_run = session.get(
            PipelineRun,
            pipeline_run_id,
        )

        if pipeline_run is not None:
            pipeline_run.status = run_status

            pipeline_run.stage_stats = {
                "naver_news": {
                    "success_companies": success_count,
                    "failed_companies": failed_count,
                    "inserted": total_inserted,
                }
            }

            pipeline_run.finished_at = datetime.now(KST)

            session.commit()

    return total_inserted