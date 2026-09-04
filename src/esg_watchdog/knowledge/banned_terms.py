"""금지 표현 목록 v1(D-17) 로더 + 스캔 (F-06).

- load_banned_terms(): banned_terms.yaml 을 패키지 리소스에서 읽어 dict 로 (최초 1회만 파싱).
- scan(text) -> list[str]: 걸린 term 목록(yaml 순서, 중복 없음). 항목마다 **allow 표현(과 never_ban 단어)을 먼저 같은 길이의 \\0 로
  마스킹**한 뒤 pattern 이 있으면 re.search(pattern, masked), 없으면 term in masked (case_sensitive false 면 소문자 비교).
  마스킹을 먼저 하지 않으면 "매수세" 안의 "매수", "사업 포트폴리오" 안의 "포트폴리오" 가 오탐이 난다 — 순서 고정.
- check_fields(record) -> {필드: [term, ...]}: policy.applies_to 필드만 검사한다. policy.excluded(evidence_quote · rationale …)는 건드리지 않는다.
- regeneration_hint(hit_terms): policy.regeneration_hint 의 {hit_terms} 를 채운 재생성 지시문.
- 치환(replacement)은 여기서 하지 않는다 — 걸리면 재생성 1회 → 폐기(on_violation).
"""

import re
from collections.abc import Iterable, Mapping
from functools import cache
from importlib import resources

import yaml

MASK = "\0"


@cache
def load_banned_terms() -> dict:
    """banned_terms.yaml 을 패키지 리소스에서 읽어 dict 로 돌려준다 (최초 1회만 파싱)."""
    text = (
        resources.files("esg_watchdog.knowledge")
        .joinpath("banned_terms.yaml")
        .read_text(encoding="utf-8")
    )
    return yaml.safe_load(text)


def _fold(value: str, case_sensitive: bool) -> str:
    return value if case_sensitive else value.lower()


def _mask(text: str, phrases: Iterable[str]) -> str:
    """phrases 를 같은 길이의 \\0 로 바꾼다 — 위치는 그대로, 글자만 사라진다."""
    for phrase in phrases:
        if phrase:
            text = text.replace(phrase, MASK * len(phrase))
    return text


def scan(text: str | None) -> list[str]:
    """text 에서 걸린 금지 term 목록. 비어 있으면 통과."""
    if not text or not text.strip():
        return []
    data = load_banned_terms()
    policy = data.get("policy") or {}
    case_sensitive = bool(policy.get("case_sensitive", False))
    never_ban = [str(word) for word in data.get("never_ban") or []]
    haystack = _fold(text, case_sensitive)
    protected = [_fold(word, case_sensitive) for word in never_ban]
    flags = 0 if case_sensitive else re.IGNORECASE

    hits: list[str] = []
    for entry in data.get("terms") or []:
        term = str(entry["term"])
        if term in never_ban or term in hits:
            continue
        allow = [_fold(str(phrase), case_sensitive) for phrase in entry.get("allow") or []]
        masked = _mask(haystack, [*protected, *allow])  # allow 마스킹이 먼저 — 그 다음에 검색
        pattern = entry.get("pattern")
        if pattern:
            matched = re.search(pattern, masked, flags) is not None
        else:
            matched = _fold(term, case_sensitive) in masked
        if matched:
            hits.append(term)
    return hits


def check_fields(record: Mapping[str, object]) -> dict[str, list[str]]:
    """policy.applies_to 필드만 스캔한다. 걸린 필드만 {필드: [term, ...]} 로. excluded 필드는 보지 않는다."""
    applies_to = (load_banned_terms().get("policy") or {}).get("applies_to") or []
    result: dict[str, list[str]] = {}
    for name in applies_to:
        value = record.get(name)
        if isinstance(value, str):
            hits = scan(value)
            if hits:
                result[name] = hits
    return result


def regeneration_hint(hit_terms: Iterable[str]) -> str:
    """재생성 지시문 — policy.regeneration_hint 의 {hit_terms} 를 채운다."""
    template = (load_banned_terms().get("policy") or {}).get("regeneration_hint") or "다음 표현을 쓰지 말고 다시 쓰세요: {hit_terms}."
    return template.format(hit_terms=", ".join(dict.fromkeys(hit_terms)))
