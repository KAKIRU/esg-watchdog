"""parsers/pdf.py · services/collect/reports.py — pypdf 로 만든 합성 PDF 로 페이지 부분집합 추출을 검사한다 (D-03)."""

from datetime import date
from io import BytesIO
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from esg_watchdog.parsers.pdf import PageText, count_pages, extract_pages
from esg_watchdog.services.collect import reports

TEXTS = ["Alpha one", "Bravo two", "", "Delta four"]


def build_pdf(texts: list[str]) -> bytes:
    """페이지마다 한 줄 텍스트가 있는 PDF. pypdf 에는 텍스트 그리기 API 가 없어 콘텐츠 스트림을 직접 넣는다."""
    writer = PdfWriter()
    for text in texts:
        page = writer.add_blank_page(width=300, height=300)
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})}
        )
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 20 150 Td ({text}) Tj ET".encode("latin-1"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


@pytest.fixture(scope="module")
def pdf_data() -> bytes:
    return build_pdf(TEXTS)


# --------------------------------------------------------------------------- parsers/pdf.py
def test_count_pages(pdf_data: bytes):
    assert count_pages(pdf_data) == 4


def test_extract_pages_reads_only_requested_subset_sorted_and_deduped(pdf_data: bytes):
    assert extract_pages(pdf_data, [4, 1, 4]) == [PageText(1, "Alpha one"), PageText(4, "Delta four")]


def test_extract_pages_keeps_empty_text_for_blank_page(pdf_data: bytes):
    assert extract_pages(pdf_data, [3]) == [PageText(3, "")]


def test_extract_pages_without_pages_reads_all(pdf_data: bytes):
    assert [page.text for page in extract_pages(pdf_data)] == TEXTS


@pytest.mark.parametrize("pages", [[0], [5], [1, 9]])
def test_extract_pages_rejects_out_of_range(pdf_data: bytes, pages: list[int]):
    with pytest.raises(ValueError, match="총 4쪽"):
        extract_pages(pdf_data, pages)


# --------------------------------------------------------------------------- reports 순수 함수
def test_parse_pages_accepts_comma_list_and_ranges():
    assert reports.parse_pages("17,31,33,35,81") == [17, 31, 33, 35, 81]
    assert reports.parse_pages("3-5, 9, 3") == [3, 4, 5, 9]


@pytest.mark.parametrize("spec", ["", " , ", "0", "5-3", "a"])
def test_parse_pages_rejects_bad_specs(spec: str):
    with pytest.raises(ValueError):
        reports.parse_pages(spec)


def test_storage_path_is_relative_posix_under_root(tmp_path: Path):
    pdf = tmp_path / "data" / "reports" / "030200_SR_2025.pdf"
    assert reports.storage_path_for(pdf, base=tmp_path) == "data/reports/030200_SR_2025.pdf"
    assert reports.storage_path_for(tmp_path / "data" / "x.pdf", base=tmp_path / "other") == (tmp_path / "data" / "x.pdf").as_posix()


def test_is_after_news_window_uses_2025_09_01_inclusive():
    assert reports.is_after_news_window(date(2025, 9, 1))
    assert reports.is_after_news_window(date(2026, 6, 30))
    assert not reports.is_after_news_window(date(2025, 6, 30))
    assert not reports.is_after_news_window(None)
