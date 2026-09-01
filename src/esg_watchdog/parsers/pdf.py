from dataclasses import dataclass
from io import BytesIO

from pypdf import PdfReader


@dataclass(frozen=True)
class PageText:
    page_number: int
    text: str


def extract_pages(pdf_data: bytes) -> list[PageText]:
    reader = PdfReader(BytesIO(pdf_data))

    pages = []

    for page_number, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()

        pages.append(
            PageText(
                page_number=page_number,
                text=text,
            )
        )

    return pages