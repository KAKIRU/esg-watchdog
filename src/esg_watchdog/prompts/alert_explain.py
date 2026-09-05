"""F-06 경보 설명문 프롬프트 (D-08 · D-16 · D-17).

- PROMPT_VERSION 은 캐시 키와 alerts.prompt_version 에 들어간다. 프롬프트·스키마를 고치면 반드시 올린다.
- 입력: 기업명 · 공약(commitment_text · 보고서 제목 · fiscal_year · page · filed_at) · 사건(title · summary · evidence_quote · event_date ·
  date_precision · confirmed · severity_signals · 원문 언론사 목록) · relation · gap_months · is_retrospective · scores.
- 출력: AlertExplanation(headline · explanation(4단락, 빈 줄 구분) · limitation · fallback). 등급은 코드가 매긴다(프롬프트에 없다).
- 금지 표현(knowledge/banned_terms.yaml)은 프롬프트에 category 6개와 예시를 넣고, 적재 전에 banned_terms.check_fields 로 다시 검사한다.
  걸리면 regeneration_hint 를 붙여 재생성. 길이(400~800자)가 벗어나면 길이 힌트를 붙여 재생성. 사유별 1회씩(services/publish/alerts.py).
- f06-v2(실측 run=42): 재생성 5회 중 4회가 필드 누락, 금지어 3건이 전부 fallback 의 '반드시' 였다 — 네 필드를 항상 모두 내라는 규칙과
  fallback 에 단정어 부정 구문을 쓰지 말라는 규칙을 system 에 추가했다. 지시문 자체의 '반드시' 도 다른 말로 바꿨다(출력을 유도하지 않게).
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date

from pydantic import BaseModel, Field

from esg_watchdog.knowledge.banned_terms import load_banned_terms

PROMPT_VERSION = "f06-v2"
MIN_LENGTH = 400
MAX_LENGTH = 800
HEADLINE_LENGTH = 30
PARAGRAPHS = 4
EXAMPLES_PER_CATEGORY = 3


def banned_category_lines() -> list[str]:
    """yaml 의 category 별로 예시 term 몇 개 — 프롬프트에 넣는 금지 표현 안내."""
    by_category: dict[str, list[str]] = {}
    for entry in load_banned_terms().get("terms") or []:
        by_category.setdefault(str(entry["category"]), []).append(str(entry["term"]))
    return [f"- {category}: {', '.join(terms[:EXAMPLES_PER_CATEGORY])} 등" for category, terms in by_category.items()]


SYSTEM_PROMPT = f"""당신은 한국 상장사의 ESG 공약(지속가능경영보고서 문장)과 그 뒤에 보도된 사건이 어긋난다는 판정 결과를, 일반 독자에게 설명하는 글을 쓰는 작성자다.
입력은 기업명 · 공약(원문 · 출처 보고서 · 페이지 · 발간일) · 사건(제목 · 요약 · 인용 · 날짜 · 확정 여부 · 심각도 신호 · 보도 언론사) · 관계 판정 · 시간 간격 · 점수다.
입력에 없는 사실·숫자·날짜는 절대 만들지 않는다. 존댓말("…입니다", "…되었습니다")로 쓴다.

[출력 형식] headline · explanation · limitation · fallback 네 필드를 항상 모두 포함한다. fallback 이 없으면 null 로 두되, 다른 필드를 생략하지 마라.

[headline] {HEADLINE_LENGTH}자 내외의 완결형 문장 또는 명사구. 낚시형·과장 금지. 기업명은 카드가 따로 표시하므로 넣지 않는다.

[explanation] 정확히 {PARAGRAPHS}단락, 단락 사이는 빈 줄 하나. 합계 {MIN_LENGTH}~{MAX_LENGTH}자.
① 무엇을 약속했나 — 공약 문장을 풀어 쓰고 출처(보고서 제목 · 페이지 · 발간일)를 포함한다. 공약을 바꿔 쓸 때는 "…한다고 밝혔습니다" 형으로.
② 이후 무엇이 확인됐나 — 사건을 보도된 사실만으로 "…로 보도되었습니다" 어투로 쓴다. 사건일·보도 언론사를 포함한다.
③ 어긋난 지점과 재무적 중대성 — 공약과 사건이 어긋나는 지점, 그리고 입력에 있는 숫자(과징금 · 사상자 · 점수 등)만으로 재무적 중대성을 설명한다.
④ 판단의 한계와 확인 방법 — 이 글이 근거로 삼은 것과 반영되지 않은 것(회사 반론 · 이후 조치 등)을 밝히고, 화면의 원문 링크(보고서 · 기사)에서 직접 확인하라고 안내한다.

[limitation] 1~2문장. 판단의 근거와 한계. 사건이 확정되지 않았으면(confirmed=false) "조사·의혹 단계로 확정되지 않았습니다" 취지의 문장을 꼭 넣는다.
[fallback] 공약·사건을 다르게 읽을 수 있는 대안 해석 1문장. 없으면 null.
"반드시 …라고 볼 수는 없습니다" 같은 단정어 부정 구문을 쓰지 마라. "…로 읽힐 여지도 있습니다", "…로 해석될 수도 있습니다" 처럼 단정어 없이 쓴다.

[소급 건] is_retrospective 가 true 면 ②에서 "공시 이전에 발생했으나 공시 이후 보도로 확인된 사안" 임을 밝힌다.

[금지 — 예외 없이 지킨다] 매매 판단 · 가격 전망 · 확정적 단정 · 사법 판단 단정을 쓰지 않는다. 다음 금지 표현 분류에 해당하는 말을 피한다:
{chr(10).join(banned_category_lines())}
사실은 "…로 보도되었습니다", "…로 의결되었습니다" 처럼 출처가 드러나게 쓰고, 유죄·불법·사기 같은 법적 결론은 내리지 않는다.
"""


@dataclass
class AlertCommitment:
    commitment_text: str
    document_title: str | None
    fiscal_year: int | None
    page: int | None
    filed_at: date


@dataclass
class AlertEvent:
    title: str
    summary: str
    evidence_quote: str
    event_date: date
    date_precision: str
    confirmed: bool
    confirmed_basis: str | None
    severity_signals: dict = field(default_factory=dict)
    presses: list[str] = field(default_factory=list)


@dataclass
class AlertContext:
    company_name: str
    commitment: AlertCommitment
    event: AlertEvent
    relation: str
    gap_months: int
    is_retrospective: bool
    scores: dict = field(default_factory=dict)


class AlertExplanation(BaseModel):
    headline: str = Field(description=f"{HEADLINE_LENGTH}자 내외, 완결형, 기업명 제외")
    explanation: str = Field(description=f"{PARAGRAPHS}단락, 빈 줄로 구분, 합계 {MIN_LENGTH}~{MAX_LENGTH}자")
    limitation: str = Field(description="1~2문장")
    fallback: str | None = Field(description="대안 해석 1문장 또는 null")


def format_event_date(event_date: date, date_precision: str) -> str:
    return event_date.strftime("%Y년 %m월") if date_precision == "month" else event_date.isoformat()


def _format_signals(signals: dict) -> str:
    fine = signals.get("fine_amount")
    parts = [
        f"과징금·과태료: {f'{int(fine):,}원' if fine else '없음'}",
        f"사상자: {signals.get('casualties') or 0}명",
        f"소송·수사: {'있음' if signals.get('lawsuit') else '없음'}",
        f"반복·재발: {'예' if signals.get('is_repeat') else '아니오'}",
        f"처분·조사 기관: {signals.get('regulator') or '없음'}",
    ]
    return " · ".join(parts)


def build_user_prompt(
    context: AlertContext,
    *,
    banned_hint: str | None = None,
    length_hint: str | None = None,
    schema_error: str | None = None,
) -> str:
    """경보 하나의 프롬프트. 재생성 때는 금지어 힌트(policy.regeneration_hint) · 길이 힌트 · 스키마 오류를 붙인다."""
    commitment, event, scores = context.commitment, context.event, context.scores
    parts = [
        f"기업명: {context.company_name}",
        "",
        "[공약]",
        f"원문: {commitment.commitment_text}",
        f"출처: {commitment.document_title or '보고서'} (회계연도 {commitment.fiscal_year or '-'}) · {commitment.page or '-'}쪽 · 발간일 {commitment.filed_at.isoformat()}",
        "",
        "[사건]",
        f"제목: {event.title}",
        f"요약: {event.summary}",
        f"인용: {event.evidence_quote}",
        f"사건일: {format_event_date(event.event_date, event.date_precision)} (정밀도 {event.date_precision})",
        f"확정 여부: {'확정 — ' + (event.confirmed_basis or '') if event.confirmed else '미확정(조사·의혹 단계)'}",
        f"심각도 신호: {_format_signals(event.severity_signals)}",
        f"보도 언론사: {', '.join(event.presses) if event.presses else '-'}",
        "",
        "[판정]",
        f"관계: {context.relation} · 공시 이후 {context.gap_months}개월 · 소급 건: {'예' if context.is_retrospective else '아니오'}",
        f"점수: 재무적 중대성 {scores.get('materiality', '-')} · 신뢰도 {scores.get('confidence', '-')} (0~100)",
    ]
    hints = [hint for hint in (banned_hint, length_hint) if hint]
    if hints:
        parts += ["", "[재생성 지시]", *hints]
    if schema_error:
        parts += ["", "[재생성 지시] 이전 출력이 형식에 맞지 않았습니다. 필드 규칙을 지켜 다시 내세요:", schema_error]
    return "\n".join(parts)


def length_hint(length: int) -> str:
    direction = "짧습니다. 더 길게" if length < MIN_LENGTH else "깁니다. 더 짧게"
    return f"explanation 이 {length}자로 {MIN_LENGTH}~{MAX_LENGTH}자 범위를 벗어나 {direction} 써서 {PARAGRAPHS}단락을 유지하세요."


def presses_of(rows: Sequence[str | None]) -> list[str]:
    """기사 press 목록 → 중복·빈 값 제거."""
    return list(dict.fromkeys(press for press in rows if press))
