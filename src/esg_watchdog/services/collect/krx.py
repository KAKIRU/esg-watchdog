"""KRX ESG 보고서 자동 수집 — 옵션 경로 (collect-krx 서브커맨드 전용, D-28).

- F-01 기본 경로가 아니다. 보고서는 사람이 PDF 와 페이지 번호를 주는 services/collect/reports.py 로 적재한다.
- KRX ESG 포털에서 지속가능경영보고서 공시 목록을 읽고 PDF 를 S3 에 올린 뒤 documents 에 등록한다.
  documents.storage_path(S3 key) 로 idempotent — 있으면 건너뛴다.
- 기간은 오늘−365일 ~ 오늘. CollectionCursor 는 쓰지 않는다(테이블은 남긴다).
- 회사 단위 예외 격리 · pipeline_runs(stage='collect_krx', trigger='manual').
- boto3(storage/s3.py)·DB import 는 함수 안에서만 — cli 모듈 import 시 boto3 가 로드되지 않는다.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlparse

from esg_watchdog.services.runs import (
    decide_status,
    failure_note,
    finish_run,
    now_kst,
    start_run,
)

STAGE = "collect_krx"
WINDOW_DAYS = 365

Log = Callable[[str], None]


def get_pdf_file_name(pdf_url: str) -> str:
    path = urlparse(pdf_url).path
    return unquote(path.rsplit("/", 1)[-1])


def build_storage_key(stock_code: str, acpt_no: str, file_name: str) -> str:
    return f"krx-esg/{stock_code}/{acpt_no}/{file_name}"


def window(today: date, days: int = WINDOW_DAYS) -> tuple[str, str]:
    return (today - timedelta(days=days)).strftime("%Y%m%d"), today.strftime("%Y%m%d")


@dataclass
class CompanyTarget:
    id: int
    stock_code: str
    name: str

    @property
    def label(self) -> str:
        return f"{self.name}({self.stock_code})"


@dataclass
class CollectKrxResult:
    run_id: int
    status: str
    stats: dict = field(default_factory=dict)


def _load_targets(stock_code: str | None) -> list[CompanyTarget]:
    from sqlalchemy import select

    from esg_watchdog.db import SessionLocal
    from esg_watchdog.models import Company

    with SessionLocal() as session:
        stmt = select(Company).where(Company.is_active.is_(True)).order_by(Company.id)
        if stock_code:
            stmt = stmt.where(Company.stock_code == stock_code)
        return [
            CompanyTarget(id=company.id, stock_code=company.stock_code, name=company.name)
            for company in session.scalars(stmt).all()
        ]


def collect_krx_company(company: CompanyTarget, from_date: str, to_date: str, stats: dict) -> None:
    from sqlalchemy import select

    from esg_watchdog.clients.krx import KrxClient
    from esg_watchdog.db import SessionLocal
    from esg_watchdog.models import Document
    from esg_watchdog.storage.s3 import S3Storage  # boto3 는 여기서만

    client = KrxClient()
    storage = S3Storage()

    data = client.get_sustainability_reports(
        stock_code=company.stock_code,
        company_name=company.name,
        from_date=from_date,
        to_date=to_date,
    )

    for item in data.get("result", []):
        acpt_no = item["acpt_no"]
        stats["fetched"] += 1

        pdf_url = client.get_attachment_pdf_url(acpt_no)
        storage_key = build_storage_key(company.stock_code, acpt_no, get_pdf_file_name(pdf_url))

        with SessionLocal() as session:
            if session.scalar(select(Document.id).where(Document.storage_path == storage_key)) is not None:
                stats["skipped"] += 1
                continue

        storage.upload_pdf(key=storage_key, data=client.download_pdf(pdf_url))

        with SessionLocal() as session:
            session.add(
                Document(
                    company_id=company.id,
                    fiscal_year=None,
                    title=get_pdf_file_name(pdf_url),
                    storage_path=storage_key,
                    source_url=pdf_url,
                    status="pending",
                )
            )
            session.commit()
        stats["inserted"] += 1


def _describe(exc: BaseException) -> str:
    if isinstance(exc, HTTPError):
        return f"HTTP {exc.code}: {exc.reason}"
    return f"{type(exc).__name__}: {exc}"


def collect_krx(*, stock_code: str | None = None, today: date | None = None, log: Log = print) -> CollectKrxResult:
    """KRX 보고서 수집 한 번 실행. 회사 단위로 예외를 격리하고 pipeline_runs 에 기록한다."""
    from sqlalchemy.exc import SQLAlchemyError

    from_date, to_date = window(today or now_kst().date())
    targets = _load_targets(stock_code)
    if not targets:
        raise LookupError(
            f"companies 에 is_active 인 stock_code={stock_code} 가 없다" if stock_code else "companies 에 is_active 회사가 없다 — 먼저 `esg-watchdog seed-companies`"
        )

    run_id = start_run(STAGE)
    stats: dict = {"companies": len(targets), "fetched": 0, "inserted": 0, "skipped": 0, "errors": []}
    failed: list[str] = []
    log(f"[collect_krx] run={run_id} {from_date}~{to_date} companies={len(targets)}")

    for company in targets:
        before = dict(stats)
        try:
            collect_krx_company(company, from_date, to_date, stats)
        except (HTTPError, URLError, OSError, ValueError, KeyError, RuntimeError, SQLAlchemyError) as exc:
            failed.append(company.label)
            stats["errors"].append({"company": company.stock_code, "error": _describe(exc)})
            log(f"ERROR {company.label}: {_describe(exc)}")
            continue
        log(
            f"{company.label}: fetched={stats['fetched'] - before['fetched']} "
            f"inserted={stats['inserted'] - before['inserted']} skipped={stats['skipped'] - before['skipped']}"
        )

    status = decide_status(len(targets) - len(failed), len(failed))
    finish_run(run_id, status, stats, failure_note(failed))
    return CollectKrxResult(run_id=run_id, status=status, stats=stats)
