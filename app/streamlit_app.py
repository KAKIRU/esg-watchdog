"""공시와 현실 사이 — ESG 조기경보. 피드 → 기업 → 경보 상세 3화면 (D-19 · D-20 · D-29 · D-31).

- 화면은 테이블만 읽는다(lib/data.py). 요청 시점 LLM 호출·배치 실행 없음.
- session_state 에는 UI 상태만: view · stock_code · alert_id · page (+ 필터 위젯 키).
- 예외는 화면에 한 줄, 스택트레이스는 로그로.
- 실행: uv run streamlit run app/streamlit_app.py  (DATABASE_URL 이 없으면 fixture 모드)
"""

import logging
import re
from typing import Any

import pandas as pd
import streamlit as st

# streamlit 이 스크립트 폴더(app/)를 sys.path 에 넣으므로 lib 를 바로 import 한다
from lib import data

logger = logging.getLogger("esg_app")

TITLE = "공시와 현실 사이 — ESG 조기경보"
PAGE_SIZE = 5  # 더보기 페이지 크기 (D-19)
ARTICLE_PREVIEW = 5  # 근거 카드 ② 에 바로 보이는 원문 링크 수. 나머지는 "원문 N건 더 보기" 로 접는다 (D-31)
CATEGORIES = ("E", "S", "G")
GRADES = ("주의", "경고", "심각")
GRADE_RANK = {grade: rank for rank, grade in enumerate(GRADES, start=1)}
GRADE_COLOR = {"주의": "blue", "경고": "orange", "심각": "red"}
CATEGORY_COLOR = {"E": "green", "S": "violet", "G": "gray"}
DISCLAIMER = (
    "이 서비스는 투자 판단을 대신하지 않으며 공개된 공시와 보도만을 근거로 합니다. "
    "원문 링크에서 직접 확인하시기 바랍니다."
)
VIEWS = ("feed", "company", "alert")
UNCONFIRMED_BADGE = "조사·의혹 단계 — 등급 상한 '주의'"
# scores.components 표시 순서 (D-31: 파이프라인 패키지(src/)를 import 하지 않으므로 여기 둔다). jsonb 는 키 순서를 보존하지 않는다 —
# DB 모드에서 막대가 매번 다른 순서로 그려지지 않게 고정한다. 목록에 없는 키는 뒤에 알파벳순.
COMPONENT_ORDER = ("industry_weight", "severity", "relation_coef", "confirmed_coef")
COMPONENT_RANK = {key: rank for rank, key in enumerate(COMPONENT_ORDER)}


# --------------------------------------------------------------------------- 표시 도우미
def is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, (list, tuple, dict, str)):
        return False
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def text_or(value: Any, default: str = "—") -> str:
    return default if is_missing(value) else str(value)


def fmt_number(value: Any, default: str = "—") -> str:
    if is_missing(value):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return str(int(number)) if number.is_integer() else f"{number:.2f}"


def fmt_date(value: Any, precision: Any = "day") -> str:
    """date_precision 이 month 면 YYYY-MM, 아니면 YYYY-MM-DD."""
    if is_missing(value):
        return "날짜 미상"
    stamp = pd.Timestamp(value)
    return stamp.strftime("%Y-%m") if precision == "month" else stamp.strftime("%Y-%m-%d")


def fmt_datetime(value: Any) -> str:
    if is_missing(value):
        return "날짜 미상"
    return pd.Timestamp(value).strftime("%Y-%m-%d %H:%M")


def grade_badge(grade: Any) -> str:
    grade_text = text_or(grade, "등급 미상")
    return f":{GRADE_COLOR.get(grade_text, 'gray')}-badge[{grade_text}]"


def category_badge(category: Any) -> str:
    category_text = text_or(category, "?")
    return f":{CATEGORY_COLOR.get(category_text, 'gray')}-badge[{category_text}]"


def paragraphs(value: Any) -> list[str]:
    """빈 줄 기준 단락. explanation 은 4단락이 계약이지만 개수를 강제하지 않는다."""
    if is_missing(value):
        return []
    return [part.strip() for part in re.split(r"\n\s*\n", str(value)) if part.strip()]


def ratio(value: Any) -> float | None:
    """점수 막대용 0~1. 1 이하의 값은 계수(그대로), 그보다 크면 0~100 점수로 본다."""
    if is_missing(value):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    number = number if number <= 1 else number / 100
    return min(max(number, 0.0), 1.0)


def score_bar(label: str, value: Any) -> None:
    bar = ratio(value)
    if bar is None:
        st.markdown(f"{label} · —")
        return
    st.progress(bar, text=f"{label} · {fmt_number(value)}")


def article_line(row: Any) -> str:
    return f"- {text_or(row.press)} · [{text_or(row.title)}]({text_or(row.url)})"


def ordered_components(components: dict) -> list[tuple[str, Any]]:
    """COMPONENT_ORDER 순서로, 목록에 없는 키는 뒤에 알파벳순으로."""
    return sorted(
        ((str(key), value) for key, value in components.items()),
        key=lambda item: (COMPONENT_RANK.get(item[0], len(COMPONENT_ORDER)), item[0]),
    )


# --------------------------------------------------------------------------- 라우팅 (session_state 는 UI 상태만)
def init_state() -> None:
    if st.session_state.get("view") not in VIEWS:
        st.session_state["view"] = "feed"
    for key, default in (("stock_code", None), ("alert_id", None), ("page", 1)):
        if key not in st.session_state:
            st.session_state[key] = default


def go_feed() -> None:
    st.session_state["view"] = "feed"


def go_company(stock_code: str) -> None:
    st.session_state["view"] = "company"
    st.session_state["stock_code"] = stock_code


def go_alert(alert_id: Any) -> None:
    st.session_state["view"] = "alert"
    st.session_state["alert_id"] = int(alert_id)


def next_page() -> None:
    st.session_state["page"] = int(st.session_state.get("page") or 1) + 1


def reset_page() -> None:
    st.session_state["page"] = 1


def clear_codes() -> None:
    if "codes" in st.query_params:
        del st.query_params["codes"]


def held_codes() -> list[str]:
    """?codes=030200,005610 → 보유 종목 필터 (D-29). 로그인·저장 없음."""
    raw = st.query_params.get("codes", "")
    if isinstance(raw, list):
        raw = ",".join(raw)
    return [code.strip() for code in str(raw).split(",") if code.strip()]


# --------------------------------------------------------------------------- 화면 1 · 피드
def render_feed(codes: list[str]) -> None:
    st.subheader("경보 피드")
    if codes:
        names = []
        for code in codes:
            info = data.get_company(code)
            names.append(info["company"]["name"] if info else code)
        left, right = st.columns([5, 1])
        left.markdown(f"보유 종목 필터 적용중: {', '.join(names)}")
        right.button("해제", key="clear-codes", on_click=clear_codes)

    filter_left, filter_right = st.columns(2)
    categories = filter_left.multiselect("E/S/G", CATEGORIES, key="f_category", on_change=reset_page)
    grades = filter_right.multiselect("등급", GRADES, key="f_grade", on_change=reset_page)

    page = int(st.session_state.get("page") or 1)
    limit = page * PAGE_SIZE
    alerts = data.get_alerts(
        {"category": categories, "grade": grades, "stock_codes": codes, "offset": 0, "limit": limit + 1}
    )
    has_more = len(alerts) > limit
    alerts = alerts.iloc[:limit]

    if alerts.empty:
        st.info("조건에 맞는 경보가 없습니다.")
        return

    for row in alerts.itertuples(index=False):
        with st.container(border=True):
            body, actions = st.columns([5, 1])
            body.markdown(f"**{text_or(row.headline)}**")
            body.markdown(
                f"{grade_badge(row.grade)} {category_badge(row.category)} · {text_or(row.name)} · "
                f"{text_or(row.relation)} · 발행 {fmt_datetime(row.published_at)} · "
                f"중대성 {fmt_number(row.materiality)} · 신뢰도 {fmt_number(row.confidence)}"
            )
            actions.button("자세히", key=f"alert-{row.id}", on_click=go_alert, args=(row.id,))
            actions.button(text_or(row.name), key=f"company-{row.id}", on_click=go_company, args=(row.stock_code,))

    if has_more:
        st.button("더보기", key="more", on_click=next_page)
    else:
        st.caption(f"경보 {len(alerts)}건 전부 표시")


# --------------------------------------------------------------------------- 화면 2 · 기업 상세
def commitment_status_badges(
    commitment_id: Any, alerts: pd.DataFrame, positive_ids: set[int]
) -> str:
    """관련 경보 있음 = 최고 등급 / 이행긍정 = positive_matches 에 있으면 배지 (D-20) / 관측 없음."""
    badges = []
    if not alerts.empty:
        related = alerts[alerts["commitment_id"] == commitment_id]
        grades = [grade for grade in related["grade"].tolist() if grade in GRADE_RANK]
        if grades:
            top = max(grades, key=GRADE_RANK.get)
            badges.append(f":{GRADE_COLOR[top]}-badge[관련 경보 · {top}]")
    if commitment_id in positive_ids:
        badges.append(":green-badge[이행긍정]")
    return " ".join(badges) if badges else ":gray-badge[관측 없음]"


def render_company(stock_code: Any) -> None:
    st.button("← 피드로", key="back-feed", on_click=go_feed)
    info = data.get_company(str(stock_code)) if not is_missing(stock_code) else None
    if info is None:
        st.warning(f"기업을 찾을 수 없습니다: {text_or(stock_code)}")
        return

    company = info["company"]
    is_control = bool(company.get("is_control_group"))
    heading = f"{text_or(company.get('name'))} · {text_or(company.get('industry_key'))}"
    st.subheader(heading + (" · :gray-badge[대조군]" if is_control else ""))

    alerts = info["alerts"]
    if alerts.empty:
        st.info("관측된 리스크 없음 · 대조군" if is_control else "관측된 리스크 없음")

    # 공약 이행 현황판
    st.markdown("### 공약 이행 현황판")
    commitments = info["commitments"]
    positive_ids = {int(value) for value in info["positive_matches"]["commitment_id"].tolist() if not is_missing(value)}
    if commitments.empty:
        st.caption("활성 공약 없음")
    for row in commitments.itertuples(index=False):
        source = row.source if isinstance(row.source, dict) else {}
        target = []
        if not is_missing(row.metric):
            target.append(str(row.metric))
        if not is_missing(row.target_value):
            target.append(fmt_number(row.target_value))
        if not is_missing(row.target_year):
            target.append(f"{fmt_number(row.target_year)}년")
        sentence = row.normalized_text if not is_missing(row.normalized_text) else row.commitment_text
        with st.container(border=True):
            st.markdown(
                f"{category_badge(row.category)} `{text_or(row.commitment_type)}` **{text_or(sentence)}** · "
                f"목표 {' '.join(target) if target else '—'} · 보고서 p.{text_or(source.get('page'))} · "
                f"공시 {fmt_date(row.filed_at)}"
            )
            st.markdown(commitment_status_badges(row.id, alerts, positive_ids))

    # 사건 타임라인
    st.markdown("### 사건 타임라인")
    events = info["events"]
    if events.empty:
        st.caption("관측된 사건 없음")
    for row in events.itertuples(index=False):
        flags = []
        if not is_missing(row.confirmed) and not bool(row.confirmed):
            flags.append(f":orange-badge[{UNCONFIRMED_BADGE}]")
        if not is_missing(row.is_retrospective) and bool(row.is_retrospective):
            flags.append(":violet-badge[소급 확인]")
        st.markdown(
            f"- **{fmt_date(row.event_date, row.date_precision)}** {category_badge(row.category)} "
            f"{text_or(row.event_type)} · {text_or(row.title)} {' '.join(flags)}"
        )

    # 경보 목록
    st.markdown("### 경보")
    if alerts.empty:
        st.caption("경보 없음")
    for row in alerts.itertuples(index=False):
        with st.container(border=True):
            body, actions = st.columns([5, 1])
            body.markdown(f"{grade_badge(row.grade)} **{text_or(row.headline)}** · {fmt_datetime(row.published_at)}")
            actions.button("자세히", key=f"company-alert-{row.id}", on_click=go_alert, args=(row.id,))


# --------------------------------------------------------------------------- 화면 3 · 경보 상세
def render_alert(alert_id: Any) -> None:
    st.button("← 피드로", key="back-feed", on_click=go_feed)
    detail = data.get_alert_detail(alert_id)
    if detail is None:
        st.warning(f"경보를 찾을 수 없습니다: {text_or(alert_id)}")
        return

    alert = detail["alert"]
    match = detail["match"]
    commitment = detail["commitment"]
    event = detail["event"]
    company = detail["company"]
    document = detail["document"]
    articles = detail["articles"]

    st.subheader(text_or(alert.get("headline")))
    header = [grade_badge(alert.get("grade")), category_badge(event.get("category"))]
    if not is_missing(match.get("relation")):
        header.append(f"관계 {match['relation']}")
    if not is_missing(match.get("gap_months")):
        header.append(f"공시 {fmt_number(match['gap_months'])}개월 후")
    if event.get("is_retrospective"):
        header.append(":violet-badge[소급 확인]")
    if not is_missing(event.get("confirmed")) and not bool(event.get("confirmed")):
        header.append(f":orange-badge[{UNCONFIRMED_BADGE}]")
    header.append(f"발행 {fmt_datetime(alert.get('published_at'))}")
    st.markdown(" · ".join(header))
    if company.get("stock_code"):
        st.button(
            f"기업 상세 · {text_or(company.get('name'))}",
            key="alert-company",
            on_click=go_company,
            args=(company["stock_code"],),
        )

    # 설명 4단락
    for paragraph in paragraphs(alert.get("explanation")):
        st.markdown(paragraph)
    if not is_missing(alert.get("limitation")):
        st.warning(f"**판단의 한계** · {alert['limitation']}")
    if not is_missing(alert.get("fallback")):
        st.info(f"**다르게 읽힐 여지** · {alert['fallback']}")

    # 근거 카드 2매
    card_commitment, card_event = st.columns(2)
    with card_commitment.container(border=True):
        st.markdown("**① 공약**")
        st.markdown(f"> {text_or(commitment.get('commitment_text'), '공약 원문 없음')}")
        source = commitment.get("source") if isinstance(commitment.get("source"), dict) else {}
        st.caption(
            f"{text_or(document.get('title'), '보고서 미상')} · 회계연도 {fmt_number(document.get('fiscal_year'))} · "
            f"p.{text_or(source.get('page'))} · 공시 {fmt_date(commitment.get('filed_at'))}"
        )
    with card_event.container(border=True):
        st.markdown("**② 사건**")
        st.markdown(f"> {text_or(event.get('evidence_quote'), '근거 인용 없음')}")
        count = len(articles) if not articles.empty else fmt_number(event.get("source_count"), "0")
        st.caption(f"사건일 {fmt_date(event.get('event_date'), event.get('date_precision'))} · 보도 {count}건")
        # data.get_alert_detail 이 기업 언급 제목 → 날짜순으로 정렬해 준다. 앞 ARTICLE_PREVIEW 건만 펼치고 나머지는 접는다
        for row in articles.iloc[:ARTICLE_PREVIEW].itertuples(index=False):
            st.markdown(article_line(row))
        rest = articles.iloc[ARTICLE_PREVIEW:]
        if not rest.empty:
            with st.expander(f"원문 {len(rest)}건 더 보기"):
                for row in rest.itertuples(index=False):
                    st.markdown(article_line(row))
        if articles.empty:
            st.caption("원문 링크 없음")

    # 점수
    st.markdown("### 점수")
    scores = match.get("scores") if isinstance(match.get("scores"), dict) else {}
    score_bar("중대성", scores.get("materiality"))
    score_bar("신뢰도", scores.get("confidence"))
    components = scores.get("components")
    if isinstance(components, dict) and components:
        st.caption("구성 요소")
        for key, value in ordered_components(components):  # 고정 순서 — 모르는 키도 버리지 않고 뒤에 붙인다
            score_bar(key, value)

    st.divider()
    st.caption(DISCLAIMER)


# --------------------------------------------------------------------------- 본체
def render_status(slot: Any) -> None:
    """모드 배지. 조회가 끝난 뒤 채운다(에러 플래그가 확정된 뒤)."""
    state = data.status()
    if state["error"]:
        slot.error(f"DB 오류 · {state['error']} — 화면이 비어 있거나 일부만 보일 수 있습니다")
    elif state["mode"] == "db":
        slot.success(f"DB 연결 · 경보 {len(data.get_alerts({}))}건")
    else:
        slot.info(f"fixture 모드 · app/fixtures · 경보 {len(data.get_alerts({}))}건")


def main() -> None:
    st.set_page_config(page_title=TITLE, layout="wide")
    st.title(TITLE)
    status_slot = st.empty()
    data.reset_error()
    init_state()

    view = st.session_state["view"]
    try:
        if view == "company":
            render_company(st.session_state.get("stock_code"))
        elif view == "alert":
            render_alert(st.session_state.get("alert_id"))
        else:
            render_feed(held_codes())
    except Exception as exc:
        logger.exception("화면 렌더 실패 (view=%s)", view)
        st.error(f"화면을 그리는 중 오류가 났습니다 · {type(exc).__name__}")

    render_status(status_slot)


main()
