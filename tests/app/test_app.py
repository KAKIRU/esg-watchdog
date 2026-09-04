"""streamlit_app.py — DATABASE_URL 없이 AppTest 로 3화면을 렌더한다 (fixture 모드)."""

import os
import sys
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

# 이 테스트는 fixture 모드여야 한다. 앱이 lib/data.py 를 import 하기 전에 지운다
os.environ.pop("DATABASE_URL", None)
APP_DIR = Path(__file__).resolve().parents[2] / "app"
APP_FILE = APP_DIR / "streamlit_app.py"
# 앱 스크립트가 `from lib import data` 로 읽는 모듈과 같은 객체를 잡아 monkeypatch 한다
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from lib import data

TEXT_ACCESSORS = ("title", "subheader", "markdown", "caption", "info", "warning", "error", "success", "text")


def page_text(at: AppTest) -> str:
    parts = []
    for name in TEXT_ACCESSORS:
        parts.extend(str(element.value) for element in getattr(at, name))
    return "\n".join(parts)


@pytest.fixture
def app() -> AppTest:
    return AppTest.from_file(str(APP_FILE), default_timeout=30)


def test_feed_renders_without_exception(app: AppTest):
    at = app.run()
    assert not at.exception
    text = page_text(at)
    assert "공시와 현실 사이 — ESG 조기경보" in text
    assert "fixture 모드" in text
    # 페이지 크기 5 (D-19): 카드 5장 + 더보기 버튼
    assert len([b for b in at.button if b.key and b.key.startswith("alert-")]) == 5
    assert at.button(key="more")
    assert at.session_state["view"] == "feed"


def test_feed_more_button_shows_sixth_alert(app: AppTest):
    at = app.run()
    at.button(key="more").click().run()
    assert not at.exception
    assert len([b for b in at.button if b.key and b.key.startswith("alert-")]) == 6
    assert not [b for b in at.button if b.key == "more"]


def test_feed_filters_category_g(app: AppTest):
    at = app.run()
    at.multiselect(key="f_category").set_value(["G"]).run()
    assert not at.exception
    keys = sorted(b.key for b in at.button if b.key and b.key.startswith("alert-"))
    assert keys == ["alert-4001", "alert-4005"]


def test_ottogi_company_view_shows_no_risk_control_group(app: AppTest):
    app.session_state["view"] = "company"
    app.session_state["stock_code"] = "007310"
    at = app.run()
    assert not at.exception
    text = page_text(at)
    assert "관측된 리스크 없음 · 대조군" in text
    assert "오뚜기" in text
    assert "관측 없음" in text  # 공약 2건 모두 관측 없음 배지
    assert "관측된 사건 없음" in text


def test_kt_company_view_has_positive_badge_and_timeline(app: AppTest):
    app.session_state["view"] = "company"
    app.session_state["stock_code"] = "030200"
    at = app.run()
    assert not at.exception
    text = page_text(at)
    assert "이행긍정" in text  # matches 3004 (D-20)
    assert "관련 경보 · 심각" in text  # commitment 1002 → alerts 4001(심각) · 4005(주의) 중 최고
    assert "2024-03" in text  # events 2001 date_precision=month
    assert "소급 확인" in text
    assert "관측된 리스크 없음" not in text


def test_alert_4003_detail_renders_without_fallback(app: AppTest):
    app.session_state["view"] = "alert"
    app.session_state["alert_id"] = 4003
    at = app.run()
    assert not at.exception
    text = page_text(at)
    assert "보안 관제 고도화 공약 이후 침해사고 발생" in text
    assert "판단의 한계" in text
    assert "다르게 읽힐 여지" not in text  # fallback None → 섹션 생략
    assert "공시 3개월 후" in text
    assert "① 공약" in text and "② 사건" in text
    assert "KT ESG 보고서 2025" in text
    assert "보도 2건" in text
    assert "이 서비스는 투자 판단을 대신하지 않으며" in text


def test_alert_4001_detail_paragraphs_and_links(app: AppTest):
    app.session_state["view"] = "alert"
    app.session_state["alert_id"] = 4001
    at = app.run()
    assert not at.exception
    text = page_text(at)
    assert "다르게 읽힐 여지" in text
    assert "https://www.sisajournal-e.com/news/articleView.html?idxno=422766" in text
    assert "https://www.huffingtonpost.kr/article/259149" in text
    assert "구성 요소" in text
    # explanation 4단락이 각각 별도 markdown 으로
    explanation_blocks = [m for m in at.markdown if str(m.value).startswith("KT는 ESG 보고서에서")]
    assert len(explanation_blocks) == 1


def test_alert_4005_shows_retrospective_and_month_precision(app: AppTest):
    app.session_state["view"] = "alert"
    app.session_state["alert_id"] = 4005
    at = app.run()
    assert not at.exception
    text = page_text(at)
    assert "소급 확인" in text
    assert "사건일 2024-03 ·" in text


def test_unknown_alert_shows_warning_not_exception(app: AppTest):
    app.session_state["view"] = "alert"
    app.session_state["alert_id"] = 9999
    at = app.run()
    assert not at.exception
    assert "경보를 찾을 수 없습니다" in page_text(at)


def test_buttons_navigate_feed_to_alert_to_company_and_back(app: AppTest):
    at = app.run()
    at.button(key="alert-4005").click().run()
    assert not at.exception
    assert at.session_state["view"] == "alert"
    assert at.session_state["alert_id"] == 4005
    at.button(key="alert-company").click().run()
    assert not at.exception
    assert at.session_state["view"] == "company"
    assert at.session_state["stock_code"] == "030200"
    at.button(key="back-feed").click().run()
    assert not at.exception
    assert at.session_state["view"] == "feed"


def test_query_param_codes_filters_feed(app: AppTest):
    app.query_params["codes"] = "007310"
    at = app.run()
    assert not at.exception
    text = page_text(at)
    assert "보유 종목 필터 적용중: 오뚜기" in text
    assert "조건에 맞는 경보가 없습니다" in text
    at.button(key="clear-codes").click().run()
    assert not at.exception
    assert "보유 종목 필터 적용중" not in page_text(at)


def test_alert_components_render_in_fixed_order_regardless_of_dict_order(app: AppTest, monkeypatch):
    """jsonb 는 키 순서를 보존하지 않는다 — 뒤섞인 dict 도 industry_weight → severity → relation_coef → confirmed_coef, 모르는 키는 뒤에 알파벳순."""
    real_detail = data.get_alert_detail

    def shuffled_detail(alert_id):
        detail = real_detail(alert_id)
        detail["match"]["scores"] = {
            **detail["match"]["scores"],
            "components": {"zeta_extra": 0.5, "confirmed_coef": 1.0, "severity": 53, "alpha_extra": 0.3, "relation_coef": 1.0, "industry_weight": 30},
        }
        return detail

    monkeypatch.setattr(data, "get_alert_detail", shuffled_detail)
    app.session_state["view"] = "alert"
    app.session_state["alert_id"] = 4001
    at = app.run()
    assert not at.exception
    labels = [str(node.proto.text).split(" · ")[0] for node in at.get("progress")]
    assert labels == ["중대성", "신뢰도", "industry_weight", "severity", "relation_coef", "confirmed_coef", "alpha_extra", "zeta_extra"]
