from datetime import date, datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from esg_watchdog.clients.dart import DartClient
from esg_watchdog.db import SessionLocal
from esg_watchdog.models import CollectionCursor, Company, Filing
from esg_watchdog.models.pipeline_run import PipelineRun

DART_SOURCE = "dart_periodic"
DART_INITIAL_DATE = date(2025, 1, 1)
KST = ZoneInfo("Asia/Seoul")

def get_report_type(report_name: str) -> str:
    for report_type in ("사업보고서", "반기보고서", "분기보고서"):
        if report_type in report_name:
            return report_type

    return report_name

def get_dart_begin_date(company_id: int) -> date:
    with SessionLocal() as session:
        cursor = session.scalar(
            select(CollectionCursor).where(
                CollectionCursor.company_id == company_id,
                CollectionCursor.source == DART_SOURCE,
            )
        )

        if cursor is None:
            return DART_INITIAL_DATE

        return cursor.cursor_at.astimezone(KST).date()

def update_dart_cursor(company_id: int) -> None:
    now = datetime.now(KST)

    with SessionLocal() as session:
        cursor = session.scalar(
            select(CollectionCursor).where(
                CollectionCursor.company_id == company_id,
                CollectionCursor.source == DART_SOURCE,
            )
        )

        if cursor is None:
            cursor = CollectionCursor(
                company_id=company_id,
                source=DART_SOURCE,
                cursor_at=now,
            )
            session.add(cursor)
        else:
            cursor.cursor_at = now

        session.commit()

def poll_dart_company(
    company: Company,
    bgn_de: str,
    end_de: str,
) -> int:
    client = DartClient()
    data = client.get_periodic_filings(
        corp_code=company.corp_code,
        bgn_de=bgn_de,
        end_de=end_de,
    )

    status = data.get("status")

    if status == "013":
        return 0

    if status != "000":
        raise RuntimeError(
            f"DART API error: {status} {data.get('message')}"
        )

    inserted = 0

    with SessionLocal() as session:
        for item in data.get("list", []):
            rcept_no = item["rcept_no"]

            existing = session.scalar(
                select(Filing).where(Filing.dart_rcept_no == rcept_no)
            )

            if existing:
                continue

            filing = Filing(
                company_id=company.id,
                dart_rcept_no=rcept_no,
                report_type=get_report_type(item["report_nm"]),
                title=item["report_nm"],
                filed_at=date(
                    int(item["rcept_dt"][:4]),
                    int(item["rcept_dt"][4:6]),
                    int(item["rcept_dt"][6:8]),
                ),
                status="pending",
            )

            session.add(filing)
            inserted += 1

        session.commit()

    return inserted

def poll_dart_companies(trigger: str = "manual") -> int:
    today = datetime.now(KST).date()

    success_count = 0
    failed_count = 0
    total_inserted = 0

    # 1. 이번 polling 실행 이력 생성
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

    # 2. polling 대상 회사 조회
    with SessionLocal() as session:
        companies = session.scalars(
            select(Company).where(Company.is_active.is_(True))
        ).all()

        company_ids = [company.id for company in companies]

    # 3. 회사별 DART polling
    for company_id in company_ids:
        try:
            with SessionLocal() as session:
                company = session.get(Company, company_id)

                if company is None:
                    continue

                begin_date = get_dart_begin_date(company_id)

                inserted = poll_dart_company(
                    company=company,
                    bgn_de=begin_date.strftime("%Y%m%d"),
                    end_de=today.strftime("%Y%m%d"),
                )

                update_dart_cursor(company_id)

                print(
                    f"{company.name}: "
                    f"{begin_date} ~ {today}, "
                    f"{inserted} inserted"
                )

                total_inserted += inserted
                success_count += 1

        except (RuntimeError, OSError, ValueError, SQLAlchemyError) as exc:
            failed_count += 1
            print(f"ERROR company_id={company_id}: {exc}")

    # 4. 전체 실행 결과 판정
    if failed_count == 0:
        run_status = "success"
    elif success_count == 0:
        run_status = "failed"
    else:
        run_status = "partial"

    # 5. pipeline_runs 실행 결과 기록
    with SessionLocal() as session:
        pipeline_run = session.get(PipelineRun, pipeline_run_id)

        if pipeline_run is not None:
            pipeline_run.status = run_status
            pipeline_run.stage_stats = {
                "dart": {
                    "success_companies": success_count,
                    "failed_companies": failed_count,
                    "inserted": total_inserted,
                }
            }
            pipeline_run.finished_at = datetime.now(KST)

            session.commit()

    return total_inserted