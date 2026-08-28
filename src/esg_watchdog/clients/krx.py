import json
import re
from html import unescape
from urllib.parse import quote, urlencode, urljoin
from urllib.request import Request, urlopen


class KrxClient:
    BASE_URL = "https://esg.krx.co.kr/contents/99/ESG99000001.jspx"

    def get_sustainability_reports(
        self,
        stock_code: str,
        company_name: str,
        from_date: str,
        to_date: str,
    ) -> dict:
        payload = [
            ("isu_cd", stock_code),
            ("sch_com_nm", f"({stock_code}) {company_name}"),
            ("sch_com_nm", ""),
            ("fr_work_dt", from_date),
            ("to_work_dt", to_date),
            ("sch_tp", "N"),
            ("pagePath", "/contents/02/02030200/ESG02030200.jsp"),
            ("code", "02/02030200/esg02030200_01"),
        ]

        request = Request(
            self.BASE_URL,
            data=urlencode(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )

        with urlopen(request) as response:
            return json.loads(response.read().decode("utf-8"))
        
    def download_pdf(self, url: str) -> bytes:
        request = Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0",
            },
        )

        with urlopen(request) as response:
            return response.read()

    def get_attachment_pdf_url(self, acpt_no: str) -> str:
        # 1. KIND 공시 상세페이지 조회
        detail_url = (
            "https://kind.krx.co.kr/common/disclsviewer.do"
            f"?method=search&acptno={acpt_no}"
        )

        detail_request = Request(
            detail_url,
            headers={
                "User-Agent": "Mozilla/5.0",
            },
        )

        with urlopen(detail_request) as response:
            detail_html = response.read().decode("utf-8", errors="ignore")

        # 2. 첨부문서 번호(docNo) 추출
        doc_no_match = re.search(
            r'id=["\']attachedDoc["\'][\s\S]*?'
            r'<option\s+value=["\'](\d+)["\']',
            detail_html,
            re.IGNORECASE,
        )

        if doc_no_match is None:
            raise RuntimeError(
                f"KRX attachment docNo not found: acpt_no={acpt_no}"
            )

        doc_no = doc_no_match.group(1)

        # 3. 첨부문서 위치 조회
        contents_url = (
            "https://kind.krx.co.kr/common/disclsviewer.do"
            f"?method=searchContents&docNo={doc_no}"
        )

        contents_request = Request(
            contents_url,
            headers={
                "User-Agent": "Mozilla/5.0",
            },
        )

        with urlopen(contents_request) as response:
            contents_html = response.read().decode("euc-kr", errors="ignore")

        # parent.setPath('', 'https://.../99998.htm', ...)
        document_url_match = re.search(
            r"parent\.setPath\(\s*'[^']*'\s*,\s*'([^']+)'",
            contents_html,
        )

        if document_url_match is None:
            raise RuntimeError(
                f"KRX attachment document URL not found: doc_no={doc_no}"
            )

        document_url = document_url_match.group(1)

        # 4. 99998.htm에서 실제 PDF 파일명 추출
        document_request = Request(
            document_url,
            headers={
                "User-Agent": "Mozilla/5.0",
            },
        )

        with urlopen(document_request) as response:
            document_html = response.read().decode("euc-kr", errors="ignore")

        pdf_match = re.search(
            r'href=["\']([^"\']+\.pdf)["\']',
            document_html,
            re.IGNORECASE,
        )

        if pdf_match is None:
            raise RuntimeError(
                f"KRX PDF link not found: document_url={document_url}"
            )

        pdf_path = unescape(pdf_match.group(1))

        pdf_url = urljoin(document_url, pdf_path)

        return quote(
            pdf_url,
            safe=":/?&=%",
        )
