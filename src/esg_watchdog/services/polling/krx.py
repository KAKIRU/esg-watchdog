from datetime import date, datetime
from urllib.parse import unquote, urlparse
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from esg_watchdog.clients.krx import KrxClient
from esg_watchdog.db import SessionLocal
from esg_watchdog.models import Company, Document
from esg_watchdog.models.collection_cursor import CollectionCursor
from esg_watchdog.models.pipeline_run import PipelineRun
from esg_watchdog.storage.s3 import S3Storage

KRX_SOURCE = "krx_esg_report"
KRX_INITIAL_DATE = date(2025, 1, 1)
KST = ZoneInfo("Asia/Seoul")


def get_pdf_file_name(pdf_url: str) -> str:
    path = urlparse(pdf_url).path
    return unquote(path.rsplit("/", 1)[-1])


def build_storage_key(
    company: Company,
    acpt_no: str,
    file_name: str,
) -> str:
    return (
        f"krx-esg/"
        f"{company.stock_code}/"
        f"{acpt_no}/"
        f"{file_name}"
    )


def poll_krx_company(
    company: Company,
    from_date: str,
    to_date: str,
) -> int:
    client = KrxClient()
    storage = S3Storage()

    data = client.get_sustainability_reports(
        stock_code=company.stock_code,
        company_name=company.name,
        from_date=from_date,
        to_date=to_date,
    )

    inserted = 0

    for item in data.get("result", []):
        acpt_no = item["acpt_no"]

        # 1. 공시번호로 실제 PDF URL 찾기
        pdf_url = client.get_attachment_pdf_url(acpt_no)

        # 2. PDF 파일명 추출
        file_name = get_pdf_file_name(pdf_url)

        # 3. 우리가 사용할 S3 Object Key 생성
        storage_key = build_storage_key(
            company=company,
            acpt_no=acpt_no,
            file_name=file_name,
        )

        # 4. 이미 DB에 저장한 보고서인지 확인
        with SessionLocal() as session:
            existing = session.scalar(
                select(Document).where(
                    Document.storage_path == storage_key
                )
            )

            if existing is not None:
                continue

        # 5. 실제 PDF 다운로드
        pdf_data = client.download_pdf(pdf_url)

        # 6. S3 업로드
        storage.upload_pdf(
            key=storage_key,
            data=pdf_data,
        )

        # 7. documents 저장
        with SessionLocal() as session:
            document = Document(
                company_id=company.id,
                fiscal_year=None,
                title=file_name,
                storage_path=storage_key,
                status="pending",
            )

            session.add(document)
            session.commit()

        inserted += 1

    return inserted

def get_krx_begin_date(company_id: int) -> date:
    with SessionLocal() as session:
        cursor = session.scalar(
            select(CollectionCursor).where(
                CollectionCursor.company_id == company_id,
                CollectionCursor.source == KRX_SOURCE,
            )
        )

        if cursor is None:
            return KRX_INITIAL_DATE

        return cursor.cursor_at.astimezone(KST).date()


def update_krx_cursor(company_id: int) -> None:
    now = datetime.now(KST)

    with SessionLocal() as session:
        cursor = session.scalar(
            select(CollectionCursor).where(
                CollectionCursor.company_id == company_id,
                CollectionCursor.source == KRX_SOURCE,
            )
        )

        if cursor is None:
            cursor = CollectionCursor(
                company_id=company_id,
                source=KRX_SOURCE,
                cursor_at=now,
            )
            session.add(cursor)
        else:
            cursor.cursor_at = now

        session.commit()

def poll_krx_companies(trigger: str = "manual") -> int:
    today = datetime.now(KST).date()

    success_count = 0
    failed_count = 0
    total_inserted = 0

    # 1. 이번 KRX polling 실행 시작 기록
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

    # 2. polling 대상 활성 회사 조회
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

    # 3. 회사별 KRX polling
    for company_id in company_ids:
        try:
            with SessionLocal() as session:
                company = session.get(
                    Company,
                    company_id,
                )

                if company is None:
                    continue

                begin_date = get_krx_begin_date(
                    company_id
                )

                inserted = poll_krx_company(
                    company=company,
                    from_date=begin_date.strftime(
                        "%Y%m%d"
                    ),
                    to_date=today.strftime(
                        "%Y%m%d"
                    ),
                )

                # 해당 회사가 끝까지 성공했을 때만 cursor 이동
                update_krx_cursor(company_id)

                print(
                    f"{company.name}: "
                    f"{begin_date} ~ {today}, "
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

    # 4. 전체 실행 상태 판정
    if failed_count == 0:
        run_status = "success"
    elif success_count == 0:
        run_status = "failed"
    else:
        run_status = "partial"

    # 5. pipeline_runs에 최종 결과 기록
    with SessionLocal() as session:
        pipeline_run = session.get(
            PipelineRun,
            pipeline_run_id,
        )

        if pipeline_run is not None:
            pipeline_run.status = run_status

            pipeline_run.stage_stats = {
                "krx": {
                    "success_companies": (
                        success_count
                    ),
                    "failed_companies": (
                        failed_count
                    ),
                    "inserted": total_inserted,
                }
            }

            pipeline_run.finished_at = (
                datetime.now(KST)
            )

            session.commit()

    return total_inserted