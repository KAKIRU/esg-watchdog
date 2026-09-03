"""clients/dart.py · services/collect/filings.py — 네트워크·DB 없이 날짜·URL 빌더와 '013' 처리를 검사한다 (D-09)."""

from datetime import date

import pytest

from esg_watchdog.clients.dart import DartClient, parse_list_response
from esg_watchdog.services.collect import filings


# --------------------------------------------------------------------------- 날짜 · URL 빌더
def test_dart_date_roundtrip():
    assert filings.to_dart_date(date(2026, 9, 3)) == "20260903"
    assert filings.parse_dart_date("20250903") == date(2025, 9, 3)


def test_window_is_365_days_before_today():
    assert filings.window(date(2026, 9, 3)) == ("20250903", "20260903")
    assert filings.window(date(2026, 3, 1)) == ("20250301", "20260301")


def test_build_filing_url_points_to_dart_viewer():
    assert filings.build_filing_url("20260115000123") == "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260115000123"


def test_filing_values_maps_list_item_to_followup_row():
    item = {"rcept_no": "20260115000123", "report_nm": "기타경영사항(자율공시)", "rcept_dt": "20260115", "corp_cls": "Y"}
    values = filings.filing_values(7, item)
    assert values == {
        "company_id": 7,
        "dart_rcept_no": "20260115000123",
        "report_type": "followup",
        "pblntf_ty": "I",
        "title": "기타경영사항(자율공시)",
        "filed_at": date(2026, 1, 15),
        "url": "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260115000123",
        "status": "pending",
    }


def test_filing_values_keeps_item_pblntf_ty_when_present():
    item = {"rcept_no": "1", "report_nm": "x", "rcept_dt": "20260101", "pblntf_ty": "B"}
    assert filings.filing_values(1, item)["pblntf_ty"] == "B"


# --------------------------------------------------------------------------- 응답 status 처리
def test_parse_list_response_treats_013_as_empty():
    assert parse_list_response({"status": "013", "message": "조회된 데이타가 없습니다."}) == ([], 0)


def test_parse_list_response_returns_items_and_total_page():
    data = {"status": "000", "message": "정상", "total_page": 3, "list": [{"rcept_no": "1"}, {"rcept_no": "2"}]}
    assert parse_list_response(data) == ([{"rcept_no": "1"}, {"rcept_no": "2"}], 3)


def test_parse_list_response_raises_with_message_on_other_status():
    with pytest.raises(RuntimeError, match="020.*요청 제한"):
        parse_list_response({"status": "020", "message": "요청 제한을 초과하였습니다."})


# --------------------------------------------------------------------------- 가짜 client (HTTP 만 바꿔 끼움)
class FakeDart(DartClient):
    def __init__(self, responses: list[dict]):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def _request(self, params: dict) -> dict:
        self.calls.append(params)
        return self.responses.pop(0)


def test_list_filings_returns_empty_list_on_013_with_one_call():
    client = FakeDart([{"status": "013", "message": "조회된 데이타가 없습니다."}])

    assert client.list_filings("00190321", "20250903", "20260903", "I") == []
    assert len(client.calls) == 1
    assert client.calls[0] == {
        "corp_code": "00190321",
        "bgn_de": "20250903",
        "end_de": "20260903",
        "pblntf_ty": "I",
        "page_no": 1,
        "page_count": 100,
    }


def test_list_filings_pages_through_total_page():
    client = FakeDart(
        [
            {"status": "000", "total_page": 3, "list": [{"rcept_no": "1"}]},
            {"status": "000", "total_page": 3, "list": [{"rcept_no": "2"}]},
            {"status": "000", "total_page": 3, "list": [{"rcept_no": "3"}]},
        ]
    )

    items = client.list_filings("00190321", "20250903", "20260903", "I")

    assert [item["rcept_no"] for item in items] == ["1", "2", "3"]
    assert [call["page_no"] for call in client.calls] == [1, 2, 3]


def test_list_filings_raises_runtime_error_on_api_error():
    client = FakeDart([{"status": "010", "message": "등록되지 않은 키입니다."}])

    with pytest.raises(RuntimeError, match="010"):
        client.list_filings("00190321", "20250903", "20260903", "I")
