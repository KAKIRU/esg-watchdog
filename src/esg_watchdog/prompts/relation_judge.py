"""F-04 관계 판정 프롬프트 (D-10 · D-11 · D-12).

- PROMPT_VERSION 은 캐시 키와 matches.prompt_version 에 들어간다. 프롬프트·스키마를 고치면 반드시 올린다.
- 입력: 공약 하나(commitment_text · normalized_text · commitment_type · metric · target_value · target_year · filed_at) ·
  사건 하나(title · summary · evidence_quote · event_date · confirmed · confirmed_basis) · **완성된 사실 문장**(gap_months · 소급 여부 ·
  목표연도 도래 여부). 날짜 계산은 전부 코드가 한다 — LLM 에 시키지 않는다(fact_sentence · target_year_sentence).
- 출력: RelationJudgement(relation · rationale · commitment_quote · event_quote · llm_confidence). relation 은 DB CHECK 가 있어 Literal.
- 인용은 각각 공약 원문(commitment_text) · 사건 evidence_quote 에서 글자 그대로 — 적재 전에 quotes.quote_in 으로 검사한다.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from pydantic import BaseModel, Field

from esg_watchdog.knowledge.taxonomy import RELATIONS, Relation

PROMPT_VERSION = "f04-v1"

SYSTEM_PROMPT = f"""당신은 한국 상장사의 ESG 공약(지속가능경영보고서 문장)과 그 뒤에 보도된 사건의 관계를 판정하는 분석가다.
입력은 공약 하나 · 사건 하나 · 둘의 시간 관계를 서술한 [사실 문장]이다. 날짜·기간 계산은 이미 되어 있으니 다시 계산하지 마라.

[관계 — 다음 중 정확히 하나: {" | ".join(RELATIONS)}]
- 위반: 공약이 명시한 행동·상태와 사건이 직접 어긋나고, 기한이 지났거나 기한 없이 즉시 지켜야 하는 약속(예: "지체 없이 신고한다")이 지켜지지 않은 경우.
- 후퇴: 공약 방향과 반대되는 결과·조치가 확인되지만, 기한·범위가 남아 있어 '위반'으로 단정할 수 없는 경우.
- 이행지연: 공약을 이행했다면 막았을 사건이 발생했으나, 목표연도가 아직 도래하지 않아 이행 여부를 판정할 수 없는 경우.
- 이행긍정: 사건이 공약을 이행한 조치·결과인 경우(경보 대상이 아니라 배지만 붙는다).
- 무관: 같은 분류라도 공약이 다루는 대상과 사건이 실질적으로 이어지지 않는 경우.

[규칙]
- 목표연도(target_year)가 아직 도래하지 않은 공약은 '위반'으로 판정하지 않는다 — '후퇴' 또는 '이행지연' 중에서 고른다. [사실 문장]이 도래 여부를 알려준다.
- 소급 건([사실 문장]에 "소급 건"이 있으면)은 rationale 에 "소급 확인"이라는 표현을 반드시 넣는다.
- rationale: 3~5문장, 존댓말. 공약 문장과 보도된 사실만 근거로 쓴다. 추정·평가·전망·매매 판단·사법 판단 단정 금지.
- commitment_quote: 공약 원문(commitment_text)에서 글자 그대로 옮긴 연속 구간. event_quote: 사건의 evidence_quote 에서 글자 그대로 옮긴 연속 구간.
  두 원문에 없는 문장은 절대 만들지 않는다. 요약·다듬기·어순 변경 금지.
- llm_confidence: 판정 확신도, 0~100 정수.
"""


@dataclass
class CommitmentInput:
    id: int
    company_id: int
    category: str
    commitment_text: str
    normalized_text: str | None
    commitment_type: str
    metric: str | None
    target_value: float | None
    target_year: int | None
    filed_at: date


@dataclass
class EventInput:
    id: int
    company_id: int
    category: str
    title: str
    summary: str
    evidence_quote: str
    event_date: date
    reported_at: date | None
    is_retrospective: bool
    confirmed: bool
    confirmed_basis: str | None


class RelationJudgement(BaseModel):
    relation: Relation
    rationale: str = Field(description="3~5문장, 존댓말")
    commitment_quote: str = Field(description="commitment_text 의 글자 그대로 연속 구간")
    event_quote: str = Field(description="사건 evidence_quote 의 글자 그대로 연속 구간")
    llm_confidence: int = Field(description="0~100 정수")


def fact_sentence(gap_months: int, is_retrospective: bool, *, event_before_filing: bool = False) -> str:
    """LLM 에 넘기는 완성된 사실 문장. 소급 건은 기준일이 보도일이라 '발생' 대신 '보도로 확인' 으로 쓴다."""
    if not is_retrospective:
        return f"이 사건은 공약 공시 {gap_months}개월 후에 발생했습니다."
    first = f"이 사건은 공약 공시 {gap_months}개월 후에 보도로 확인되었습니다."
    if event_before_filing:
        return f"{first} 이 사건은 공시 이전에 발생했으나 공시 이후 보도로 확인된 소급 건입니다."
    return f"{first} 이 사건은 발생 시점보다 뒤에 보도로 확인된 소급 건입니다."


def target_year_sentence(target_year: int | None, today: date) -> str:
    if target_year is None:
        return "이 공약에는 목표연도가 없습니다."
    if target_year > today.year:
        return f"이 공약의 목표연도 {target_year}년은 아직 도래하지 않았습니다 — '위반' 판정 금지, '후퇴' 또는 '이행지연' 중에서 고르세요."
    return f"이 공약의 목표연도 {target_year}년은 이미 도래했습니다."


def _format_value(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:g}"


def build_user_prompt(
    commitment: CommitmentInput,
    event: EventInput,
    gap_months: int,
    *,
    today: date,
    failed_quotes: Sequence[str] = (),
    schema_error: str | None = None,
) -> str:
    """후보 하나의 프롬프트. 재생성 때는 원문에 없던 인용(또는 스키마 오류)을 힌트로 붙인다."""
    parts = [
        "[공약]",
        f"원문(commitment_text): {commitment.commitment_text}",
        f"정규화 문장: {commitment.normalized_text or '-'}",
        (
            f"유형: {commitment.commitment_type} · 지표: {commitment.metric or '-'} · 목표값: {_format_value(commitment.target_value)} · "
            f"목표연도: {commitment.target_year or '-'} · 공시일(filed_at): {commitment.filed_at.isoformat()}"
        ),
        "",
        "[사건]",
        f"제목: {event.title}",
        f"요약: {event.summary}",
        f"evidence_quote: {event.evidence_quote}",
        f"사건일: {event.event_date.isoformat()} · 확정: {'예' if event.confirmed else '아니오'} · 확정 근거: {event.confirmed_basis or '없음'}",
        "",
        "[사실 문장]",
        fact_sentence(gap_months, event.is_retrospective, event_before_filing=event.event_date < commitment.filed_at),
        target_year_sentence(commitment.target_year, today),
    ]
    if failed_quotes:
        parts += ["", "[재생성 지시] 다음 인용은 원문에 없습니다:"]
        parts += [f"- {quote}" for quote in failed_quotes]
        parts += ["commitment_quote 는 공약 원문의, event_quote 는 evidence_quote 의 연속된 부분 문자열을 글자 그대로 옮기세요."]
    if schema_error:
        parts += ["", "[재생성 지시] 이전 출력이 형식에 맞지 않았습니다. 필드 규칙을 지켜 다시 내세요:", schema_error]
    return "\n".join(parts)
