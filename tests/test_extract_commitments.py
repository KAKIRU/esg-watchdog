"""services/extract/commitments.py — 가짜 LLM 으로 인용 실패→재생성→폐기 · is_gray→quarantined · 중복 skip 을 DB 없이 검사한다 (D-04 · D-06)."""

from datetime import date

from esg_watchdog.llm.client import LLMClient
from esg_watchdog.llm.providers.fake_provider import FakeProvider
from esg_watchdog.prompts.commitment_extract import (
    PROMPT_VERSION,
    CommitmentItem,
    CommitmentPage,
    build_user_prompt,
)
from esg_watchdog.services.extract import commitments as svc

PAGE_TEXT = (
    "환경 목표\n\n"
    "당사는 2030년까지 온실가스 배출량을\n2020년 대비 20% 감축한다.\n"
    "또한 전 사업장의 “안전 점검”을 정기적으로 실시할 계획이다.\n"
    "2024년 배출량은 12만 톤이었다.\n"
)
PAGE = svc.PageInput(document_id=7, page_no=18, text=PAGE_TEXT)
DOC = svc.DocumentTarget(id=7, company_id=3, company_name="오뚜기", stock_code="007310", title="보고서", published_at=date(2025, 7, 31))


def item(text: str, normalized: str, **overrides) -> dict:
    base = {
        "commitment_text": text,
        "normalized_text": normalized,
        "category": "E",
        "sub_tags": ["온실가스·에너지"],
        "is_gray": False,
        "gray_reason": None,
        "metric": None,
        "target_value": None,
        "target_year": None,
        "baseline": None,
    }
    base.update(overrides)
    return base


GOOD = item("2030년까지 온실가스 배출량을 2020년 대비 20% 감축한다", "2030년까지 온실가스 20% 감축", metric="감축률(%)", target_value=20, target_year=2030, baseline="2020년")
GOOD_QUOTED = item('전 사업장의 "안전 점검"을 정기적으로 실시할 계획이다', "전 사업장 안전 점검 정기 실시", category="S", sub_tags=["산업안전보건"], is_gray=True, gray_reason="주체가 계열사인지 불명확")
BAD = item("2030년까지 온실가스 배출량을 30% 감축한다", "온실가스 30% 감축")
BAD_AGAIN = item("2035년까지 탄소중립을 달성한다", "2035년 탄소중립")


def make_client(tmp_path, responses):
    fake = FakeProvider(responses)
    return LLMClient(provider=fake, cache_dir=tmp_path, sleep=lambda _seconds: None), fake


# --------------------------------------------------------------------------- (a) 인용 실패 → 재생성 1회 → 폐기
def test_quote_failure_regenerates_once_with_hint_then_discards(tmp_path):
    client, fake = make_client(tmp_path, [{"items": [GOOD, BAD]}, {"items": [GOOD, BAD_AGAIN]}])
    logs: list[str] = []

    outcome = svc.extract_page(client, "m", "오뚜기", PAGE, log=logs.append)

    assert len(fake.calls) == 2
    assert outcome.attempts == 2 and outcome.regenerated
    assert "다음 문장은 원문에 없습니다" in fake.calls[1]["user"]
    assert BAD["commitment_text"] in fake.calls[1]["user"]
    assert "원문에 없습니다" not in fake.calls[0]["user"]
    assert [v.item.normalized_text for v in outcome.items] == [GOOD["normalized_text"]]
    assert outcome.discarded == [BAD["commitment_text"], BAD_AGAIN["commitment_text"]]
    # span 은 원문 오프셋 — 줄바꿈이 낀 원문 구간을 가리킨다
    start, end = outcome.items[0].span
    assert PAGE_TEXT[start:end] == "2030년까지 온실가스 배출량을\n2020년 대비 20% 감축한다"


def test_all_quotes_pass_makes_no_second_call(tmp_path):
    client, fake = make_client(tmp_path, [{"items": [GOOD, GOOD_QUOTED]}])
    outcome = svc.extract_page(client, "m", "오뚜기", PAGE, log=lambda _line: None)
    assert len(fake.calls) == 1
    assert not outcome.regenerated and outcome.discarded == []
    assert len(outcome.items) == 2


def test_schema_failure_regenerates_once_then_discards(tmp_path):
    client, fake = make_client(tmp_path, [{"items": [{"commitment_text": 1}]}, {"items": [GOOD]}])
    outcome = svc.extract_page(client, "m", "오뚜기", PAGE, log=lambda _line: None)
    assert len(fake.calls) == 2
    assert "형식에 맞지 않았습니다" in fake.calls[1]["user"]
    assert [v.item.normalized_text for v in outcome.items] == [GOOD["normalized_text"]]

    broken, fake = make_client(tmp_path / "b", [{"items": [{"commitment_text": 1}]}, {"items": "no"}])
    outcome = svc.extract_page(broken, "m", "오뚜기", PAGE, log=lambda _line: None)
    assert len(fake.calls) == 2
    assert outcome.items == []
    assert outcome.discarded and outcome.discarded[0].startswith("(스키마 실패)")


def test_regeneration_keeps_first_pass_items_and_merges_second(tmp_path):
    client, _ = make_client(tmp_path, [{"items": [GOOD, BAD]}, {"items": [GOOD_QUOTED]}])
    outcome = svc.extract_page(client, "m", "오뚜기", PAGE, log=lambda _line: None)
    assert sorted(v.item.normalized_text for v in outcome.items) == sorted([GOOD["normalized_text"], GOOD_QUOTED["normalized_text"]])
    assert outcome.discarded == [BAD["commitment_text"]]


# --------------------------------------------------------------------------- (b) is_gray → quarantined
def test_gray_item_is_quarantined_and_quantitative_type_needs_value_and_year():
    passed, failed = svc.verify_items([CommitmentItem(**GOOD), CommitmentItem(**GOOD_QUOTED)], PAGE_TEXT)
    assert failed == []
    quantitative = svc.commitment_values(DOC, PAGE, passed[0])
    gray = svc.commitment_values(DOC, PAGE, passed[1])

    assert quantitative["status"] == "active" and quantitative["quarantine_reason"] is None
    assert quantitative["commitment_type"] == "정량"
    assert quantitative["source"] == {"doc_id": 7, "page": 18, "span": list(passed[0].span)}
    assert quantitative["filed_at"] == date(2025, 7, 31)
    assert quantitative["prompt_version"] == PROMPT_VERSION
    assert quantitative["commitment_text"] == "2030년까지 온실가스 배출량을 2020년 대비 20% 감축한다"

    assert gray["status"] == "quarantined"
    assert gray["quarantine_reason"] == "주체가 계열사인지 불명확"
    assert gray["commitment_type"] == "정성"
    # 원문 글자 그대로(둥근따옴표 유지), 공백만 합친다
    assert gray["commitment_text"] == "전 사업장의 “안전 점검”을 정기적으로 실시할 계획이다"

    silent = CommitmentItem(**item("2024년 배출량은 12만 톤이었다", "x", is_gray=True, gray_reason=None))
    values = svc.commitment_values(DOC, PAGE, svc.VerifiedItem(item=silent, span=(0, 1)))
    assert values["status"] == "quarantined" and values["quarantine_reason"] == svc.GRAY_REASON


# --------------------------------------------------------------------------- (c) 중복 skip
def test_dedup_key_ignores_whitespace_only():
    assert svc.dedup_key(3, "온실가스  20%\n감축") == svc.dedup_key(3, "온실가스 20% 감축")
    assert svc.dedup_key(3, "온실가스 20% 감축") != svc.dedup_key(4, "온실가스 20% 감축")
    assert svc.dedup_key(3, "온실가스 20% 감축") != svc.dedup_key(3, "온실가스 20% 감축.")


def test_duplicate_normalized_text_is_skipped_not_merged(tmp_path):
    client, _ = make_client(tmp_path, [{"items": [GOOD, dict(GOOD, commitment_text="2020년 대비 20% 감축한다")]}])
    outcome = svc.extract_page(client, "m", "오뚜기", PAGE, log=lambda _line: None)
    # 페이지 안에서는 commitment_text 가 다르면 둘 다 살아 있고, 적재 단계의 (company_id, normalized_text) 대조가 뒤의 것을 건너뛴다
    assert len(outcome.items) == 2
    seen = {svc.dedup_key(DOC.company_id, "2030년까지 온실가스 20% 감축")}
    kept = [v for v in outcome.items if svc.dedup_key(DOC.company_id, v.item.normalized_text) not in seen]
    assert kept == []


# --------------------------------------------------------------------------- 프롬프트
def test_prompt_and_schema_shape():
    assert PROMPT_VERSION == "f02-v1"
    prompt = build_user_prompt("KT", 3, "본문", failed_quotes=["없는 문장"])
    assert "회사: KT" in prompt and "페이지: 3" in prompt and "본문" in prompt
    assert "- 없는 문장" in prompt and "원문 그대로 인용하세요" in prompt
    schema = CommitmentPage.model_json_schema()
    assert schema["title"] == "CommitmentPage"
    assert schema["$defs"]["CommitmentItem"]["properties"]["sub_tags"]["items"]["enum"][0] == "온실가스·에너지"
    assert schema["$defs"]["CommitmentItem"]["required"] == list(schema["$defs"]["CommitmentItem"]["properties"])
