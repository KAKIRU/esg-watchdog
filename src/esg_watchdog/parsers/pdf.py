"""PDF 텍스트 추출(pypdf). 페이지 부분집합만 읽을 수 있다 — 전체를 통으로 넣지 않는다 (D-03)."""

from collections.abc import Sequence
from dataclasses import dataclass
from io import BytesIO

from pypdf import PdfReader


@dataclass(frozen=True)
class PageText:
    page_number: int  # 1-based
    text: str


def count_pages(pdf_data: bytes) -> int:
    return len(PdfReader(BytesIO(pdf_data)).pages)


def extract_pages(pdf_data: bytes, pages: Sequence[int] | None = None) -> list[PageText]:
    """pages(1-based)만 읽는다 — 중복 제거·오름차순. None 이면 전체. 범위 밖 번호는 ValueError."""
    reader = PdfReader(BytesIO(pdf_data))
    page_count = len(reader.pages)
    numbers = list(range(1, page_count + 1)) if pages is None else sorted(set(pages))

    out_of_range = [number for number in numbers if number < 1 or number > page_count]
    if out_of_range:
        raise ValueError(f"페이지 범위 밖: {out_of_range} (총 {page_count}쪽)")

    return [
        PageText(page_number=number, text=(reader.pages[number - 1].extract_text() or "").strip())
        for number in numbers
    ]
