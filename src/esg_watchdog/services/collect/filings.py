"""F-01 DART 후속공시 목록 수집 — pblntf_ty=I · 12개월 · 원문 미수집 (D-09).

- 회사별 corp_code × bgn_de=오늘−365일 × end_de=오늘 × pblntf_ty='I'(거래소공시). 정기공시(A)는 수집하지 않는다.
- Filing(report_type='followup', pblntf_ty=item['pblntf_ty'] 또는 'I', dart_rcept_no=rcept_no(unique — 있으면 skip),
  title=report_nm, filed_at=rcept_dt, url=DART 뷰어 링크, status='pending'). 원문 ZIP 은 받지 않는다.
- 기존 filings 행은 건드리지 않는다(INSERT … ON CONFLICT DO NOTHING).
- 회사 단위 예외 격리 · pipeline_runs(stage='collect_filings', trigger='manual').
- DB·설정·클라이언트 import 는 함수 안에서만 — .env 없이 import 가능. client 는 인자로 주입한다(기본 DartClient).
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Protocol
from urllib.error import HTTPError, URLError

from esg_watchdog.services.runs import (
    decide_status,
    failure_note,
    finish_run,
    now_kst,
    start_run,
)

STAGE = "collect_filings"
PBLNTF_TY = "I"  # 거래소공시
REPORT_TYPE = "followup"
WINDOW_DAYS = 365
DART_VIEWER_URL = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"

Log = Callable[[str], None]


class FilingListClient(Protocol):
    def list_filings(
        self,
        corp_code: str,
        bgn_de: str,
        end_de: str,
        pblntf_ty: str,
        page_no: int = ...,
        page_count: int = ...,
    ) -> list[dict]: ...


# --------------------------------------------------------------------------- 순수 함수
def to_dart_date(value: date) -> str:
    return value.strftime("%Y%m%d")


def parse_dart_date(value: str) -> date:
    """'YYYYMMDD' → date."""
    return date(int(value[:4]), int(value[4:6]), int(value[6:8]))


def window(today: date, days: int = WINDOW_DAYS) -> tuple[str, str]:
    """(bgn_de, end_de) = (오늘−days, 오늘)."""
    return to_dart_date(today - timedelta(days=days)), to_dart_date(today)


def build_filing_url(rcept_no: str) -> str:
    return DART_VIEWER_URL.format(rcept_no=rcept_no)


def filing_values(company_id: int, item: dict) -> dict:
    """list.json 항목 → filings 행 값. 항목에 pblntf_ty 가 없으면(공식 응답에는 없다) 요청값 'I'."""
    rcept_no = item["rcept_no"]
    return {
        "company_id": company_id,
        "dart_rcept_no": rcept_no,
        "report_type": REPORT_TYPE,
        "pblntf_ty": item.get("pblntf_ty") or PBLNTF_TY,
        "title": item["report_nm"],
        "filed_at": parse_dart_date(item["rcept_dt"]),
        "url": build_filing_url(rcept_no),
        "status": "pending",
    }


# --------------------------------------------------------------------------- DB 적재
@dataclass
class CompanyTarget:
    id: int
    stock_code: str
    name: str
    corp_code: str

    @property
    def label(self) -> str:
        return f"{self.name}({self.stock_code})"


@dataclass
class CollectFilingsResult:
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
            CompanyTarget(id=company.id, stock_code=company.stock_code, name=company.name, corp_code=company.corp_code)
            for company in session.scalars(stmt).all()
        ]


def store_filings(session, company: CompanyTarget, items: list[dict], stats: dict) -> None:
    """dart_rcept_no unique — 이미 있으면 건너뛴다(기존 행은 수정하지 않는다)."""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from esg_watchdog.models import Filing

    for item in items:
        # rowcount 는 ORM 경유 INSERT 에서 -1 이 나올 수 있어 RETURNING id 로 판단한다
        inserted_id = session.execute(
            pg_insert(Filing)
            .values(**filing_values(company.id, item))
            .on_conflict_do_nothing(index_elements=["dart_rcept_no"])
            .returning(Filing.id)
        ).scalar()
        if inserted_id is None:
            stats["dup"] += 1
        else:
            stats["inserted"] += 1


def _describe(exc: BaseException) -> str:
    if isinstance(exc, HTTPError):
        return f"HTTP {exc.code}: {exc.reason}"
    return f"{type(exc).__name__}: {exc}"


def collect_filings(
    *,
    client: FilingListClient | None = None,
    stock_code: str | None = None,
    today: date | None = None,
    log: Log = print,
) -> CollectFilingsResult:
    """후속공시 목록 수집 한 번 실행. 회사 단위로 예외를 격리하고 pipeline_runs 에 기록한다."""
    from sqlalchemy.exc import SQLAlchemyError

    from esg_watchdog.db import SessionLocal

    if client is None:
        from esg_watchdog.clients.dart import DartClient

        client = DartClient()

    bgn_de, end_de = window(today or now_kst().date())
    targets = _load_targets(stock_code)
    if not targets:
        raise LookupError(
            f"companies 에 is_active 인 stock_code={stock_code} 가 없다" if stock_code else "companies 에 is_active 회사가 없다 — 먼저 `esg-watchdog seed-companies`"
        )

    run_id = start_run(STAGE)
    stats: dict = {"companies": len(targets), "fetched": 0, "inserted": 0, "dup": 0, "errors": []}
    failed: list[str] = []
    log(f"[collect_filings] run={run_id} {bgn_de}~{end_de} pblntf_ty={PBLNTF_TY} companies={len(targets)}")

    for company in targets:
        before = dict(stats)
        try:
            items = client.list_filings(corp_code=company.corp_code, bgn_de=bgn_de, end_de=end_de, pblntf_ty=PBLNTF_TY)
            stats["fetched"] += len(items)
            with SessionLocal() as session:
                store_filings(session, company, items, stats)
                session.commit()
        except (HTTPError, URLError, OSError, ValueError, KeyError, RuntimeError, SQLAlchemyError) as exc:
            failed.append(company.label)
            stats["errors"].append({"company": company.stock_code, "error": _describe(exc)})
            log(f"ERROR {company.label}: {_describe(exc)}")
            continue
        log(
            f"{company.label}: fetched={stats['fetched'] - before['fetched']} "
            f"inserted={stats['inserted'] - before['inserted']} dup={stats['dup'] - before['dup']}"
        )

    status = decide_status(len(targets) - len(failed), len(failed))
    finish_run(run_id, status, stats, failure_note(failed))
    return CollectFilingsResult(run_id=run_id, status=status, stats=stats)
