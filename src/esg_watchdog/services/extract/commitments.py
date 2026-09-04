"""F-02 공약 추출 — document_pages → commitments (D-04 · D-05 · D-06).

- 대상: document 하나(--document) 또는 한 회사의 documents 전부(--company). 페이지별로 LLM(llm_model_extract) 을 부른다.
- 인용 검사: 항목마다 quote_in(commitment_text, page.text). 실패 항목이 있으면 그 페이지만 "원문에 없습니다" 힌트를 붙여 1회 재생성.
  두 번의 통과 항목을 합치고(normalized_text 기준 중복 제거), 재생성 후에도 실패한 문장은 폐기하고 pipeline_runs.note 에 남긴다.
  스키마 검증 실패(LLMClient 의 ValueError)도 같은 재생성 1회.
- 적재 값: commitment_text 는 페이지 원문 구간(span)을 공백만 합친 것(글자는 원문 그대로) · source = {"doc_id", "page", "span"} ·
  filed_at = documents.published_at(없으면 그 문서는 실패) · status = 'quarantined' if is_gray else 'active' · prompt_version.
- 중복: (company_id, normalized_text) 가 같으면 skip — 병합하지 않는다(D-06). DB 기존 행과 이번 실행 안 모두 대조.
- pipeline_runs(stage='extract', trigger='manual'). 문서 단위 예외 격리. 페이지마다 commit(캐시가 있어 다시 돌려도 싸다).
- 순수 함수(verify_items · extract_page · commitment_values · dedup_key)는 DB 없이 가짜 provider 로 시험한다.
- DB·settings import 는 함수 안에서만 — .env 없이 import 가능.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date

from esg_watchdog.llm.client import LLMClient
from esg_watchdog.llm.quotes import find_span, normalize, quote_in
from esg_watchdog.prompts.commitment_extract import (
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    CommitmentItem,
    CommitmentPage,
    build_user_prompt,
    commitment_type_for,
)
from esg_watchdog.services.runs import (
    decide_status,
    failure_note,
    finish_run,
    start_run,
)

STAGE = "extract"
GRAY_REASON = "회색지대(D-04) — LLM 이 is_gray 로 표시"

Log = Callable[[str], None]


# --------------------------------------------------------------------------- 순수 함수
@dataclass
class PageInput:
    document_id: int
    page_no: int
    text: str


@dataclass
class DocumentTarget:
    id: int
    company_id: int
    company_name: str
    stock_code: str
    title: str
    published_at: date | None

    @property
    def label(self) -> str:
        return f"document {self.id} ({self.company_name} · {self.title})"


@dataclass
class VerifiedItem:
    item: CommitmentItem
    span: tuple[int, int]


@dataclass
class PageOutcome:
    page_no: int
    items: list[VerifiedItem] = field(default_factory=list)
    discarded: list[str] = field(default_factory=list)
    attempts: int = 0
    regenerated: bool = False


def verify_items(items: list[CommitmentItem], text: str) -> tuple[list[VerifiedItem], list[CommitmentItem]]:
    """(원문에 있는 항목 → span 포함, 원문에 없는 항목)."""
    passed: list[VerifiedItem] = []
    failed: list[CommitmentItem] = []
    for item in items:
        span = find_span(item.commitment_text, text) if quote_in(item.commitment_text, text) else None
        if span is None:
            failed.append(item)
        else:
            passed.append(VerifiedItem(item=item, span=span))
    return passed, failed


def dedup_key(company_id: int, normalized_text: str) -> tuple[int, str]:
    """(company_id, normalized_text) — 공백만 합쳐 비교한다 (D-06)."""
    return company_id, " ".join((normalized_text or "").split())


def _merge(first: list[VerifiedItem], second: list[VerifiedItem]) -> list[VerifiedItem]:
    merged: dict[str, VerifiedItem] = {}
    for verified in [*first, *second]:
        merged.setdefault(normalize(verified.item.commitment_text), verified)
    return list(merged.values())


def extract_page(client: LLMClient, model: str, company_name: str, page: PageInput, *, log: Log = print) -> PageOutcome:
    """한 페이지 추출. 인용 실패·스키마 실패 → 힌트를 붙여 1회 재생성 → 재실패 항목 폐기."""
    outcome = PageOutcome(page_no=page.page_no)
    passed: list[VerifiedItem] = []
    failed_quotes: list[str] = []
    schema_error: str | None = None

    outcome.attempts += 1
    try:
        result = client.complete(
            CommitmentPage,
            SYSTEM_PROMPT,
            build_user_prompt(company_name, page.page_no, page.text),
            model=model,
            prompt_version=PROMPT_VERSION,
        )
        passed, failed = verify_items(result.items, page.text)
        failed_quotes = [item.commitment_text for item in failed]
    except ValueError as exc:
        schema_error = str(exc)

    if failed_quotes or schema_error:
        outcome.regenerated = True
        outcome.attempts += 1
        log(
            f"  page {page.page_no}: 재생성 — "
            + (f"원문에 없는 문장 {len(failed_quotes)}건" if failed_quotes else f"스키마 실패: {schema_error[:120]}")
        )
        second_failed: list[str] = []
        try:
            result = client.complete(
                CommitmentPage,
                SYSTEM_PROMPT,
                build_user_prompt(
                    company_name, page.page_no, page.text, failed_quotes=failed_quotes, schema_error=schema_error
                ),
                model=model,
                prompt_version=PROMPT_VERSION,
            )
            passed_again, failed_again = verify_items(result.items, page.text)
            passed = _merge(passed, passed_again)
            second_failed = [item.commitment_text for item in failed_again]
        except ValueError as exc:
            log(f"  page {page.page_no}: 재생성도 스키마 실패 — 폐기: {str(exc)[:120]}")
            if not failed_quotes:
                second_failed = [f"(스키마 실패) {str(exc)[:200]}"]

        kept = {normalize(verified.item.commitment_text) for verified in passed}
        outcome.discarded = list(
            dict.fromkeys(quote for quote in [*failed_quotes, *second_failed] if normalize(quote) not in kept)
        )

    outcome.items = passed
    return outcome


def commitment_values(doc: DocumentTarget, page: PageInput, verified: VerifiedItem) -> dict:
    """Commitment 행 값. commitment_text 는 원문 구간의 공백만 합친다(글자는 원문 그대로)."""
    item = verified.item
    start, end = verified.span
    return {
        "company_id": doc.company_id,
        "category": item.category,
        "sub_tags": list(dict.fromkeys(item.sub_tags)),
        "commitment_type": commitment_type_for(item),
        "commitment_text": " ".join(page.text[start:end].split()),
        "normalized_text": " ".join(item.normalized_text.split()),
        "metric": item.metric,
        "target_value": item.target_value,
        "target_year": item.target_year,
        "baseline": item.baseline,
        "source": {"doc_id": doc.id, "page": page.page_no, "span": [start, end]},
        "filed_at": doc.published_at,
        "status": "quarantined" if item.is_gray else "active",
        "quarantine_reason": (item.gray_reason or GRAY_REASON) if item.is_gray else None,
        "prompt_version": PROMPT_VERSION,
    }


# --------------------------------------------------------------------------- DB 적재
@dataclass
class ExtractResult:
    run_id: int
    status: str
    stats: dict = field(default_factory=dict)


def _new_stats(documents: int) -> dict:
    return {
        "documents": documents,
        "pages": 0,
        "attempts": 0,
        "regenerated": 0,
        "extracted": 0,
        "inserted": 0,
        "quarantined": 0,
        "dup_skipped": 0,
        "discarded": 0,
        "llm_calls": 0,
        "cache_hits": 0,
        "errors": [],
    }


def _load_targets(document_id: int | None, stock_code: str | None) -> list[DocumentTarget]:
    from sqlalchemy import select

    from esg_watchdog.db import SessionLocal
    from esg_watchdog.models import Company, Document

    with SessionLocal() as session:
        stmt = select(Document, Company).join(Company, Company.id == Document.company_id).order_by(Document.id)
        if document_id is not None:
            stmt = stmt.where(Document.id == document_id)
        elif stock_code:
            stmt = stmt.where(Company.stock_code == stock_code)
        else:
            raise ValueError("document_id 또는 stock_code 가 필요하다")
        return [
            DocumentTarget(
                id=document.id,
                company_id=company.id,
                company_name=company.name,
                stock_code=company.stock_code,
                title=document.title,
                published_at=document.published_at,
            )
            for document, company in session.execute(stmt).all()
        ]


def _load_pages(session, document_id: int) -> list[PageInput]:
    from sqlalchemy import select

    from esg_watchdog.models import DocumentPage

    rows = session.scalars(
        select(DocumentPage).where(DocumentPage.document_id == document_id).order_by(DocumentPage.page_no)
    ).all()
    return [PageInput(document_id=row.document_id, page_no=row.page_no, text=row.text) for row in rows]


def _existing_keys(session, company_id: int) -> set[tuple[int, str]]:
    from sqlalchemy import select

    from esg_watchdog.models import Commitment

    texts = session.scalars(select(Commitment.normalized_text).where(Commitment.company_id == company_id)).all()
    return {dedup_key(company_id, text) for text in texts if text}


def extract_commitments(
    *,
    document_id: int | None = None,
    stock_code: str | None = None,
    client: LLMClient | None = None,
    model: str | None = None,
    log: Log = print,
) -> ExtractResult:
    """공약 추출 한 번 실행. 문서 단위로 예외를 격리하고 pipeline_runs 에 기록한다."""
    from sqlalchemy.exc import SQLAlchemyError

    from esg_watchdog.db import SessionLocal
    from esg_watchdog.models import Commitment

    if client is None:
        client = LLMClient()
    if model is None:
        from esg_watchdog.config import settings

        model = settings.llm_model_extract
    # 키·공급자 설정 오류는 pipeline_runs 를 만들기 전에 낸다
    provider_name = client.provider.name

    targets = _load_targets(document_id, stock_code)
    if not targets:
        raise LookupError(
            f"documents 에 id={document_id} 가 없다" if document_id is not None else f"stock_code={stock_code} 의 documents 가 없다 — 먼저 `esg-watchdog collect --stage reports`"
        )

    run_id = start_run(STAGE)
    stats = _new_stats(len(targets))
    notes: list[str] = []
    failed: list[str] = []
    calls_before, hits_before = client.stats["calls"], client.stats["cache_hits"]
    log(f"[extract] run={run_id} provider={provider_name} model={model or '-'} prompt={PROMPT_VERSION} documents={len(targets)}")

    for doc in targets:
        before = dict(stats)
        try:
            if doc.published_at is None:
                raise ValueError("documents.published_at 이 없다 — filed_at 을 정할 수 없다 (collect --stage reports --published-at)")
            with SessionLocal() as session:
                pages = _load_pages(session, doc.id)
                if not pages:
                    notes.append(f"{doc.label}: document_pages 없음")
                    log(f"WARN {doc.label}: document_pages 가 없다 — 건너뜀")
                    continue
                seen = _existing_keys(session, doc.company_id)
                for page in pages:
                    outcome = extract_page(client, model, doc.company_name, page, log=log)
                    stats["pages"] += 1
                    stats["attempts"] += outcome.attempts
                    stats["regenerated"] += int(outcome.regenerated)
                    stats["extracted"] += len(outcome.items)
                    stats["discarded"] += len(outcome.discarded)
                    for quote in outcome.discarded:
                        notes.append(f"폐기(doc {doc.id} p{page.page_no}): {quote[:80]}")
                    inserted_here = 0
                    for verified in outcome.items:
                        key = dedup_key(doc.company_id, verified.item.normalized_text)
                        if key in seen:
                            stats["dup_skipped"] += 1
                            continue
                        seen.add(key)
                        values = commitment_values(doc, page, verified)
                        session.add(Commitment(**values))
                        stats["inserted"] += 1
                        inserted_here += 1
                        if values["status"] == "quarantined":
                            stats["quarantined"] += 1
                    session.commit()
                    log(
                        f"  page {page.page_no}: items={len(outcome.items)} inserted={inserted_here} "
                        f"discarded={len(outcome.discarded)} regenerated={outcome.regenerated}"
                    )
        except (ValueError, LookupError, RuntimeError, SQLAlchemyError) as exc:
            failed.append(doc.label)
            stats["errors"].append({"document": doc.id, "error": f"{type(exc).__name__}: {exc}"})
            log(f"ERROR {doc.label}: {type(exc).__name__}: {exc}")
            continue
        log(
            f"{doc.label}: pages={stats['pages'] - before['pages']} inserted={stats['inserted'] - before['inserted']} "
            f"quarantined={stats['quarantined'] - before['quarantined']} dup={stats['dup_skipped'] - before['dup_skipped']} "
            f"discarded={stats['discarded'] - before['discarded']}"
        )

    stats["llm_calls"] = client.stats["calls"] - calls_before
    stats["cache_hits"] = client.stats["cache_hits"] - hits_before
    status = decide_status(len(targets) - len(failed), len(failed))
    note_parts = [part for part in (failure_note(failed), *notes) if part]
    finish_run(run_id, status, stats, "; ".join(note_parts) or None)
    return ExtractResult(run_id=run_id, status=status, stats=stats)
