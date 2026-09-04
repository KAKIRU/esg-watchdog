"""OpenAI 어댑터 — Responses API · text.format=json_schema(strict) · temperature 0 (D-40).

- 설치된 SDK(openai 3.x)의 responses.create(text={"format": {"type": "json_schema", ...}}) 로 구조화 출력을 강제한다.
- strict=True 규칙에 맞게 schema 사본을 손본다(to_strict_schema): 모든 object 에 additionalProperties=false ·
  properties 전부 required · default 제거. Pydantic model_json_schema() 출력을 그대로 넣으면 API 가 400 을 낸다.
- SDK 재시도는 끄고(max_retries=0) 일시 오류는 TransientProviderError 로 바꿔 LLMClient 가 1회 재시도하게 한다.
- SDK import 는 생성자 안에서만 — 모듈 import 는 SDK 없이도 된다.
"""

import copy
import json

from esg_watchdog.llm.providers import (
    ProviderError,
    TransientProviderError,
    is_transient_status,
    schema_name,
)

DEFAULT_MAX_OUTPUT_TOKENS = 8192
DEFAULT_TIMEOUT_SECONDS = 120.0

_SCHEMA_MAP_KEYS = ("$defs", "definitions", "properties")
_SCHEMA_LIST_KEYS = ("anyOf", "oneOf", "allOf", "prefixItems")
_SCHEMA_NODE_KEYS = ("items", "not")


def _strict(node: dict) -> dict:
    if node.get("type") == "object" or isinstance(node.get("properties"), dict):
        node.setdefault("additionalProperties", False)
        if isinstance(node.get("properties"), dict):
            node["required"] = list(node["properties"])
    node.pop("default", None)
    for key in _SCHEMA_MAP_KEYS:
        mapping = node.get(key)
        if isinstance(mapping, dict):
            for child in mapping.values():
                if isinstance(child, dict):
                    _strict(child)
    for key in _SCHEMA_LIST_KEYS:
        items = node.get(key)
        if isinstance(items, list):
            for child in items:
                if isinstance(child, dict):
                    _strict(child)
    for key in _SCHEMA_NODE_KEYS:
        child = node.get(key)
        if isinstance(child, dict):
            _strict(child)
    return node


def to_strict_schema(json_schema: dict) -> dict:
    """OpenAI strict 규칙에 맞춘 사본을 돌려준다. 원본은 바꾸지 않는다."""
    return _strict(copy.deepcopy(json_schema))


def parse_output(response) -> dict:
    """Response → JSON dict. 불완전(max_output_tokens) · 거부 · 비JSON 은 ProviderError."""
    if getattr(response, "status", None) == "incomplete":
        details = getattr(response, "incomplete_details", None)
        reason = getattr(details, "reason", None) or "unknown"
        raise ProviderError(f"openai 응답이 불완전함({reason}) — max_output_tokens 를 늘리거나 입력을 줄이세요")

    text = response.output_text
    if not text:
        for item in getattr(response, "output", []) or []:
            for content in getattr(item, "content", []) or []:
                if getattr(content, "type", None) == "refusal":
                    raise ProviderError(f"openai 가 응답을 거부함: {getattr(content, 'refusal', '')}")
        raise ProviderError("openai 응답에 텍스트가 없음")

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProviderError(f"openai 응답이 JSON 이 아님: {exc}") from exc
    if not isinstance(data, dict):
        raise ProviderError(f"openai 응답 최상위가 object 가 아님: {type(data).__name__}")
    return data


class OpenAIProvider:
    name = "openai"

    def __init__(
        self,
        api_key: str,
        *,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        import openai  # SDK 는 여기서만 import 한다

        self._sdk = openai
        self._client = openai.OpenAI(api_key=api_key, max_retries=0, timeout=timeout)
        self._max_output_tokens = max_output_tokens

    def complete_json(self, system: str, user: str, json_schema: dict, model: str) -> dict:
        sdk = self._sdk
        try:
            response = self._client.responses.create(
                model=model,
                instructions=system,
                input=user,
                temperature=0,
                max_output_tokens=self._max_output_tokens,
                text={
                    "format": {
                        "type": "json_schema",
                        "name": schema_name(json_schema),
                        "schema": to_strict_schema(json_schema),
                        "strict": True,
                    }
                },
            )
        except sdk.APIConnectionError as exc:  # APITimeoutError 포함
            raise TransientProviderError(f"openai 연결/타임아웃: {exc}") from exc
        except sdk.APIStatusError as exc:
            message = f"openai HTTP {exc.status_code}: {exc}"
            if is_transient_status(exc.status_code):
                raise TransientProviderError(message) from exc
            raise ProviderError(message) from exc
        return parse_output(response)
