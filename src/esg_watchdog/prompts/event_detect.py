"""F-03 사건 탐지 프롬프트 (D-05 · D-07 · D-08 · D-36 · D-39).

- PROMPT_VERSION 은 캐시 키와 events.prompt_version 에 들어간다. 프롬프트·스키마를 고치면 반드시 올린다.
- 입력: 한 회사의 기사 최대 BATCH_SIZE 건(id · title · description · published_at · press). 출력: 기사별 판정 EventBatch.
- event_type · sub_tags · confirmed_basis 는 DB CHECK 가 없어 스키마에서 str 로 두고(목록은 프롬프트에 명시),
  적재 직전에 services/detect/events.py 가 taxonomy 상수로 검증·강등한다. category · date_precision 은 CHECK 가 있어 Literal.
- evidence_quote 는 title 이나 description 에서 글자 그대로 — 적재 전에 quotes.quote_in 으로 검사한다.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date

from pydantic import BaseModel, Field

from esg_watchdog.knowledge.taxonomy import (
    CONFIRMED_BASES,
    EVENT_TYPES,
    SUB_TAGS,
    Category,
    DatePrecision,
)

PROMPT_VERSION = "f03-v1"
BATCH_SIZE = 15

SYSTEM_PROMPT = f"""당신은 한국 상장사 관련 뉴스 기사에서 ESG 사건을 탐지하는 분석가다.
입력은 한 회사의 기사 최대 {BATCH_SIZE}건(id · title · description · published_at · press)이다.
기사마다 판정을 정확히 하나씩 낸다. judgements 의 article_id 는 입력의 id 를 그대로 쓴다.

[사건 판정 — D-05]
- is_esg_event: 기사가 이 회사의 ESG 사건(산업재해·사고·법규 위반·제재·소송·수사·정보유출·환경오염·제품안전·품질·지배구조 논란·
  사건에 따른 재무영향·사건 뒤의 이행조치)을 보도하면 true. 홍보·실적·인사·신제품·주가·행사 기사는 false.
- is_subject: 기사의 주체가 이 회사인가. 이름이 비슷한 다른 회사(예: KT ≠ KT&G · kt wiz · KTX)거나, 단순 언급·비교 대상·업계 일반론이면 false.
- via_subsidiary: 사건 주체가 이 회사의 자회사·계열사면 true(그때도 is_subject 는 true).

[사건일 때 채우는 필드 — 사건이 아니면 null · 빈 리스트 · false]
- category: E(환경) / S(사회) / G(지배구조) 하나.
- sub_tags: 다음 목록에서만 1개 이상: {" | ".join(SUB_TAGS)}
- event_type: 다음 목록에서 하나: {" | ".join(EVENT_TYPES)}
- title: 정규화된 사건 제목. 기업명을 빼고 사건 자체를 명사구로 쓴다(예: "시화공장 화재로 공장 가동 중단"). 같은 사건은 같은 제목이 나오도록.
- summary: 1~2문장. 보도된 사실만, "…로 보도되었습니다" 어투. 추정·평가·전망 금지.
- event_date: 사건 발생일(보도일이 아니다). 날짜가 나오면 YYYY-MM-DD 와 date_precision="day".
  월까지만 알 수 있으면 그 달 1일(YYYY-MM-01)과 date_precision="month". 발생일을 알 수 없으면 보도 문맥으로 가장 좁게 추정한다.
- severity_signals: fine_amount(과징금·과태료 금액, 원 단위 정수, 없으면 null) · casualties(사상자 수, 없으면 0) ·
  lawsuit(소송·고발·수사 여부) · is_repeat(반복·재발로 보도됐는가) · regulator(처분·조사 기관명, 없으면 null).
- confirmed · confirmed_basis (D-08): 사실이 확정된 근거가 있으면 confirmed=true 와 근거 — {" | ".join(CONFIRMED_BASES[:-1])} 중 하나.
  의혹·주장·수사 단계·언론 보도뿐이면 confirmed=false, confirmed_basis="없음".
- evidence_quote: title 이나 description 에서 글자 그대로 옮긴 한 구절(연속된 부분 문자열). 두 필드에 없는 문장은 절대 만들지 않는다.
- is_retrospective: 발생일이 보도일보다 뚜렷이(몇 달 이상) 앞선 과거 사안이면 true.
"""


@dataclass
class ArticleInput:
    id: int
    title: str
    description: str
    published_at: date
    press: str | None


class SeveritySignals(BaseModel):
    fine_amount: int | None = Field(description="원 단위 정수, 없으면 null")
    casualties: int
    lawsuit: bool
    is_repeat: bool
    regulator: str | None


class ArticleJudgement(BaseModel):
    article_id: int
    is_esg_event: bool
    is_subject: bool
    via_subsidiary: bool
    category: Category | None
    sub_tags: list[str]
    event_type: str | None
    title: str | None = Field(description="정규화된 사건 제목, 기업명 제외")
    summary: str | None
    event_date: date | None = Field(description="발생일 YYYY-MM-DD (보도일 아님)")
    date_precision: DatePrecision | None
    severity_signals: SeveritySignals | None
    confirmed: bool
    confirmed_basis: str | None
    evidence_quote: str | None = Field(description="title 이나 description 의 글자 그대로")
    is_retrospective: bool


class EventBatch(BaseModel):
    judgements: list[ArticleJudgement]


def format_article(index: int, article: ArticleInput) -> str:
    press = article.press or "-"
    return (
        f"[기사 {index}] id={article.id} · 언론사={press} · 보도일={article.published_at.isoformat()}\n"
        f"제목: {article.title}\n"
        f"설명: {article.description or '-'}"
    )


def build_user_prompt(
    company_name: str,
    aliases: Sequence[str],
    articles: Sequence[ArticleInput],
    *,
    failed: Mapping[int, str] | None = None,
    schema_error: str | None = None,
) -> str:
    """기사 배치 프롬프트. 재생성 때는 실패한 기사 id 와 이유를 힌트로 붙인다."""
    alias_text = ", ".join(alias for alias in aliases if alias) or company_name
    parts = [
        f"회사: {company_name} (별칭: {alias_text})",
        f"기사 {len(articles)}건 — 기사마다 judgements 에 article_id 로 판정 하나를 낸다.",
        "",
        *[format_article(index, article) for index, article in enumerate(articles, start=1)],
    ]
    if failed:
        parts += ["", "[재생성 지시] 다음 기사는 evidence_quote 가 제목·설명에 없거나 필수 필드가 비었습니다:"]
        parts += [f"- id={article_id}: {reason}" for article_id, reason in failed.items()]
        parts += ["evidence_quote 는 제목이나 설명에서 글자 그대로(연속된 부분 문자열) 옮기고, 사건이면 필수 필드를 모두 채우세요."]
    if schema_error:
        parts += ["", "[재생성 지시] 이전 출력이 형식에 맞지 않았습니다. 필드 규칙을 지켜 다시 내세요:", schema_error]
    return "\n".join(parts)
