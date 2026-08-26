import json
from urllib.parse import urlencode
from urllib.request import urlopen

from esg_watchdog.config import settings


class DartClient:
    BASE_URL = "https://opendart.fss.or.kr/api/list.json"

    def get_periodic_filings(
            self,
            corp_code: str,
            bgn_de: str,
            end_de: str,
    ) -> dict:
        params = {
            "crtfc_key": settings.dart_api_key,
            "corp_code": corp_code,
            "bgn_de": bgn_de,
            "end_de": end_de,
            "pblntf_ty": "A",
            "page_count": 100,
        }

        url = f"{self.BASE_URL}?{urlencode(params)}"

        with urlopen(url) as response:
            data = json.loads(response.read().decode("utf-8"))
            return data