"""OpenDART 공시검색(list.json) 클라이언트.

- list_filings(corp_code, bgn_de, end_de, pblntf_ty, page_no=1, page_count=100): total_page 만큼 페이징해 합친다.
- status '013'(조회된 데이타가 없습니다) 은 빈 목록, 그 외 status != '000' 은 RuntimeError(message 포함).
- 설정(settings)은 호출 시점에 읽는다 — .env 없이 import 할 수 있고, 테스트는 _request 를 바꿔 끼운다.
"""

import json
from urllib.parse import urlencode
from urllib.request import urlopen

DART_LIST_URL = "https://opendart.fss.or.kr/api/list.json"
STATUS_OK = "000"
STATUS_NO_RESULT = "013"
DEFAULT_PAGE_COUNT = 100  # API 최대
REQUEST_TIMEOUT_SECONDS = 30


def parse_list_response(data: dict) -> tuple[list[dict], int]:
    """list.json 응답 → (list, total_page). '013' 은 ([], 0). 그 외 오류는 RuntimeError."""
    status = str(data.get("status", ""))
    if status == STATUS_NO_RESULT:
        return [], 0
    if status != STATUS_OK:
        raise RuntimeError(f"DART API error {status}: {data.get('message')}")
    return list(data.get("list") or []), int(data.get("total_page") or 1)


class DartClient:
    def _request(self, params: dict) -> dict:
        from esg_watchdog.config import settings

        if not settings.dart_api_key:
            raise RuntimeError("DART_API_KEY 가 .env 에 없다")
        query = urlencode({"crtfc_key": settings.dart_api_key, **params})
        with urlopen(f"{DART_LIST_URL}?{query}", timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return json.loads(response.read().decode("utf-8"))

    def list_filings(
        self,
        corp_code: str,
        bgn_de: str,
        end_de: str,
        pblntf_ty: str,
        page_no: int = 1,
        page_count: int = DEFAULT_PAGE_COUNT,
    ) -> list[dict]:
        """공시 목록. page_no 부터 total_page 까지 읽어 한 리스트로 돌려준다."""
        items: list[dict] = []
        page = page_no
        while True:
            data = self._request(
                {
                    "corp_code": corp_code,
                    "bgn_de": bgn_de,
                    "end_de": end_de,
                    "pblntf_ty": pblntf_ty,
                    "page_no": page,
                    "page_count": page_count,
                }
            )
            page_items, total_page = parse_list_response(data)
            items.extend(page_items)
            if not page_items or page >= total_page:
                break
            page += 1
        return items
