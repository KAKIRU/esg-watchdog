"""공급자 어댑터 — 공통 인터페이스는 complete_json 하나 (D-40).

- 어댑터: openai_provider.OpenAIProvider · anthropic_provider.AnthropicProvider · fake_provider.FakeProvider.
- SDK 는 각 어댑터 파일의 생성자 안에서만 import 한다 — 한쪽 키만 있어도 다른 쪽이 문제되지 않는다.
- 어댑터는 JSON schema dict 만 받는다(Pydantic → dict 변환은 llm/client.py 가 한 번만 한다).
- 일시 오류(429 · 5xx · timeout · 연결 끊김)는 TransientProviderError 로 바꿔 던진다 — LLMClient 가 1회 재시도한다.
"""

import re
from typing import Protocol

_NAME_RE = re.compile(r"[^A-Za-z0-9_-]+")
NAME_MAX_LENGTH = 64


class ProviderError(RuntimeError):
    """공급자 호출 실패 — 인증·잘못된 요청·응답 형식 등, 다시 보내도 같은 결과가 나오는 오류."""


class TransientProviderError(ProviderError):
    """일시 오류 — 429 · 5xx · timeout · 연결 끊김. LLMClient 가 1회 재시도한다."""


class Provider(Protocol):
    name: str

    def complete_json(self, system: str, user: str, json_schema: dict, model: str) -> dict: ...


def is_transient_status(status_code: int) -> bool:
    return status_code == 429 or status_code >= 500


def schema_name(json_schema: dict, default: str = "response") -> str:
    """스키마 title → 공급자가 허용하는 이름([A-Za-z0-9_-], 64자 이하). OpenAI format name · Anthropic tool name 규칙."""
    raw = str(json_schema.get("title") or default)
    cleaned = _NAME_RE.sub("_", raw).strip("_") or default
    return cleaned[:NAME_MAX_LENGTH]
