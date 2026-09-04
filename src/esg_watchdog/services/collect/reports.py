"""F-01 보고서 PDF 로컬 파일 → document_pages (지정 페이지만) (D-03 · D-26 · D-36).

- 사람이 준 페이지 번호만 추출한다. 키워드로 페이지를 고르지 않는다(services/extraction/report_pages.py 삭제). 청킹 없음.
- Document 는 storage_path = PDF 상대경로(예: data/reports/030200_SR_2025.pdf)로 idempotent — 있으면 재사용.
  published_at · source_url · page_count 저장. status 는 텍스트 있는 페이지가 하나라도 적재되면 'processed'.
- DocumentPage(document_id, page_no, text) 는 UNIQUE(document_id, page_no) 로 upsert(다시 돌리면 text 갱신).
- 추출 텍스트가 전부 빈 문자열이면 needs_ocr=True 로 표시하고 경고(중단하지 않는다).
- published_at 이 2025-09-01 이후면 D-10 경고(뉴스 창 12개월 시작보다 늦은 보고서). 중단하지 않는다.
- pipeline_runs(stage='collect_reports', trigger='manual'). DB import 는 함수 안에서만.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from esg_watchdog.parsers.pdf import count_pages, extract_pages
from esg_watchdog.services.runs import finish_run, start_run

STAGE = "collect_reports"
# D-10: 뉴스 창(오늘−12개월) 시작. 이보다 늦게 발간된 보고서는 공약 이후 사건이 거의 없어 매칭이 0~1건으로 붕괴할 수 있다
NEWS_WINDOW_START = date(2025, 9, 1)
D10_WARNING = "D-10: 뉴스 창(12개월) 시작보다 늦은 보고서 — 매칭이 0~1건으로 붕괴할 수 있음. 한 해 앞선 판(2025년 발간)을 쓸 것"
OCR_WARNING = "추출한 텍스트가 전부 비어 있음 — 스캔 PDF 일 수 있다. needs_ocr=True 로 표시했다"

Log = Callable[[str], None]


# --------------------------------------------------------------------------- 순수 함수
def parse_pages(spec: str) -> list[int]:
    """'17,31,33,35,81' 또는 '3-5,9' → 정렬·중복 제거한 1-based 페이지 번호."""
    pages: set[int] = set()
    for raw in spec.split(","):
        token = raw.strip()
        if not token:
            continue
        if "-" in token:
            low_text, high_text = token.split("-", 1)
            low, high = int(low_text), int(high_text)
            if low > high:
                raise ValueError(f"페이지 범위가 거꾸로다: {token}")
            pages.update(range(low, high + 1))
        else:
            pages.add(int(token))
    if not pages:
        raise ValueError("페이지 목록이 비어 있다 (예: --pages 17,31,33)")
    if min(pages) < 1:
        raise ValueError("페이지 번호는 1 이상이어야 한다")
    return sorted(pages)


def storage_path_for(pdf_path: str | Path, base: Path | None = None) -> str:
    """documents.storage_path — 프로젝트 루트(cwd) 기준 상대경로(posix). 루트 밖이면 준 경로 그대로(posix)."""
    path = Path(pdf_path)
    root = (base or Path.cwd()).resolve()
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def is_after_news_window(published_at: date | None, window_start: date = NEWS_WINDOW_START) -> bool:
    return published_at is not None and published_at >= window_start


# --------------------------------------------------------------------------- 적재
@dataclass
class IngestReportResult:
    run_id: int
    status: str
    storage_path: str
    document_id: int | None = None
    page_count: int | None = None
    pages_loaded: int = 0
    pages_empty: int = 0
    needs_ocr: bool = False
    warnings: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)


def ingest_report(
    company_id: int,
    pdf_path: str | Path,
    pages: Sequence[int],
    title: str,
    fiscal_year: int | None,
    published_at: date | None,
    source_url: str | None = None,
    *,
    log: Log = print,
) -> IngestReportResult:
    """로컬 PDF 의 지정 페이지를 documents · document_pages 에 적재한다. 같은 storage_path 는 재사용(idempotent)."""
    from pypdf.errors import PyPdfError
    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.exc import SQLAlchemyError

    from esg_watchdog.db import SessionLocal
    from esg_watchdog.models import Company, Document, DocumentPage

    path = Path(pdf_path)
    if not path.is_file():
        raise FileNotFoundError(f"PDF 가 없다: {path}")
    if not pages:
        raise ValueError("pages 가 비어 있다")
    storage_path = storage_path_for(path)

    with SessionLocal() as session:
        if session.get(Company, company_id) is None:
            raise LookupError(f"companies 에 id={company_id} 가 없다")

    warnings: list[str] = []
    if is_after_news_window(published_at):
        warnings.append(D10_WARNING)
        log(f"WARN published_at={published_at}: {D10_WARNING}")

    run_id = start_run(STAGE)
    result = IngestReportResult(run_id=run_id, status="running", storage_path=storage_path, warnings=warnings)
    stats: dict = {
        "storage_path": storage_path,
        "pages_requested": sorted(set(pages)),
        "page_count": None,
        "pages_loaded": 0,
        "pages_empty": 0,
        "needs_ocr": False,
        "warnings": warnings,
        "errors": [],
    }

    try:
        pdf_data = path.read_bytes()
        page_count = count_pages(pdf_data)
        extracted = extract_pages(pdf_data, pages)
        loaded = [page for page in extracted if page.text]
        needs_ocr = not loaded

        with SessionLocal() as session:
            document = session.scalar(select(Document).where(Document.storage_path == storage_path))
            if document is None:
                document = Document(company_id=company_id, storage_path=storage_path, title=title)
                session.add(document)
            elif document.company_id != company_id:
                raise ValueError(f"{storage_path} 는 이미 company_id={document.company_id} 의 문서다 (요청 {company_id})")

            document.title = title
            document.fiscal_year = fiscal_year
            document.published_at = published_at
            document.source_url = source_url
            document.page_count = page_count
            document.needs_ocr = needs_ocr
            if loaded:
                document.status = "processed"
            session.flush()

            for page in loaded:
                session.execute(
                    pg_insert(DocumentPage)
                    .values(document_id=document.id, page_no=page.page_number, text=page.text)
                    .on_conflict_do_update(constraint="uq_document_pages_document_page", set_={"text": page.text})
                )
            session.commit()
            result.document_id = document.id

        result.page_count = page_count
        result.pages_loaded = len(loaded)
        result.pages_empty = len(extracted) - len(loaded)
        result.needs_ocr = needs_ocr
        stats.update(page_count=page_count, pages_loaded=len(loaded), pages_empty=result.pages_empty, needs_ocr=needs_ocr)
        if needs_ocr:
            warnings.append(OCR_WARNING)
            log(f"WARN {storage_path}: {OCR_WARNING}")
        result.status = "success"
        log(
            f"{storage_path}: document_id={result.document_id} page_count={page_count} "
            f"pages_loaded={len(loaded)} pages_empty={result.pages_empty} needs_ocr={needs_ocr}"
        )
    except (OSError, ValueError, PyPdfError, SQLAlchemyError) as exc:
        stats["errors"].append(f"{type(exc).__name__}: {exc}")
        log(f"ERROR {storage_path}: {type(exc).__name__}: {exc}")
        result.status = "failed"

    result.stats = stats
    finish_run(run_id, result.status, stats, note="; ".join(warnings) or None)
    return result
