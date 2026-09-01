import json
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from esg_watchdog.config import settings

NAVER_NEWS_URL = "https://naverapihub.apigw.ntruss.com/search/v1/news"

class NaverClient:
    def search_news(self, query: str, display: int = 100, start: int = 1, sort: str = "date") -> dict:
        params = urlencode(
            {
                "query": query,
                "display": display,
                "start": start,
                "sort": sort,
                "format": "json",
            }
        )

        request = Request(
            f"{NAVER_NEWS_URL}?{params}",
            headers={
                "X-NCP-APIGW-API-KEY-ID": settings.naver_client_id,
                "X-NCP-APIGW-API-KEY": settings.naver_client_secret,
            },
        )

        with urlopen(request, timeout=10) as response:
            body = response.read().decode("utf-8")

        return json.loads(body)