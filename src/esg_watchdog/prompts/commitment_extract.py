"""F-02 공약 추출 프롬프트 (D-04 · D-05 · D-06).

- PROMPT_VERSION 은 캐시 키와 commitments.prompt_version 에 들어간다. 프롬프트·스키마를 고치면 반드시 올린다.
- 출력은 CommitmentPage(items: list[CommitmentItem]). sub_tags 는 택소노미 Literal 로 강제한다.
- commitment_text 는 페이지 원문 글자 그대로 — 적재 전에 quotes.quote_in 으로 검사한다.
"""

from collections.abc import Sequence

from pydantic import BaseModel, Field

from esg_watchdog.knowledge.taxonomy import SUB_TAGS, Category, SubTag

PROMPT_VERSION = "f02-v1"

SYSTEM_PROMPT = f"""당신은 한국 상장사의 ESG·지속가능경영보고서에서 '공약'을 추출하는 분석가다. 입력은 보고서 한 페이지의 원문 텍스트다.

[공약의 정의 — D-04] 다음 셋을 모두 만족해야 공약이다.
① 주체가 회사 자신이다(명시된 계열사 포함).
② 미래의 행동·상태에 대한 약속이다.
③ 이행 여부를 사후에 외부에서 확인할 수 있다.
③만 만족하면 정성 공약이고, 수치와 시점(목표 연도)까지 있으면 정량 공약이다.

[제외] 과거 실적·현황 서술, 업계 일반론, 검증 불가 슬로건, 타사·정부 목표 인용은 공약이 아니다.

[회색지대] ①②③ 중 하나라도 확신이 서지 않으면 버리지 말고 is_gray=true 로 표시하고 gray_reason 에 이유를 쓴다(격리 적재된다).
확실한 공약은 is_gray=false, gray_reason=null.

[필드 규칙]
- commitment_text: 페이지 원문을 글자 그대로 옮긴다. 요약·다듬기·오탈자 수정·어순 변경 금지. 원문에 없는 문장은 절대 만들지 않는다.
  원문의 연속된 한 구간(문장 하나 또는 이어진 몇 문장)이어야 한다.
- normalized_text: 공약을 한 문장으로 정규화한다(주체 생략, "…까지 …한다" 형). 같은 공약은 같은 문장이 나오도록 간결하게 쓴다.
- category: E(환경) / S(사회) / G(지배구조) 중 하나.
- sub_tags: 다음 목록에서만 1개 이상 고른다: {" | ".join(SUB_TAGS)}
- metric: 측정 지표명(예: "온실가스 감축률(%)", "투자금액(억원)"). 없으면 null.
- target_value: 목표 수치(숫자만, 단위 없이). 없으면 null.
- target_year: 목표 연도(4자리 정수). 없으면 null.
- baseline: 기준연도·기준값 서술(예: "2020년 배출량 대비"). 없으면 null.
- 공약이 하나도 없으면 items 를 빈 리스트로 낸다.
"""

class CommitmentItem(BaseModel):
    commitment_text: str = Field(description="페이지 원문 글자 그대로. 연속된 한 구간")
    normalized_text: str = Field(description="한 문장으로 정규화한 공약")
    category: Category
    sub_tags: list[SubTag] = Field(description="택소노미 sub_tags 목록에서만")
    is_gray: bool = Field(description="회색지대면 true")
    gray_reason: str | None
    metric: str | None
    target_value: float | None
    target_year: int | None
    baseline: str | None


class CommitmentPage(BaseModel):
    items: list[CommitmentItem]


def commitment_type_for(item: CommitmentItem) -> str:
    """수치(target_value)와 시점(target_year)이 모두 있으면 정량, 아니면 정성 (D-04)."""
    return "정량" if item.target_value is not None and item.target_year is not None else "정성"


def build_user_prompt(
    company_name: str,
    page_no: int,
    text: str,
    *,
    failed_quotes: Sequence[str] = (),
    schema_error: str | None = None,
) -> str:
    """페이지 원문 프롬프트. 재생성 때는 원문에 없던 문장 목록(또는 스키마 오류)을 힌트로 붙인다."""
    parts = [f"회사: {company_name}", f"페이지: {page_no}", "", "[페이지 원문]", text]
    if failed_quotes:
        listed = "\n".join(f"- {quote}" for quote in failed_quotes)
        parts += [
            "",
            "[재생성 지시] 다음 문장은 원문에 없습니다:",
            listed,
            "원문 그대로 인용하세요. commitment_text 는 위 페이지 원문의 연속된 부분 문자열이어야 합니다.",
        ]
    if schema_error:
        parts += ["", "[재생성 지시] 이전 출력이 형식에 맞지 않았습니다. 필드 규칙을 지켜 다시 내세요:", schema_error]
    return "\n".join(parts)
