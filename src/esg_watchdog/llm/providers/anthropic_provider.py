"""Anthropic 어댑터 — tools 의 input_schema 로 schema 를 넣고 tool_choice 로 그 도구를 강제 · temperature 0 (D-40).

- 설치된 SDK(anthropic 1.x)의 messages.create(tools=[{name, input_schema}], tool_choice={"type": "tool", "name": ...}).
  응답 content 의 tool_use 블록 input 이 결과다.
- SDK 재시도는 끄고(max_retries=0) 일시 오류는 TransientProviderError 로 바꿔 LLMClient 가 1회 재시도하게 한다.
- SDK import 는 생성자 안에서만 — 모듈 import 는 SDK 없이도 된다.
"""

from esg_watchdog.llm.providers import (
    ProviderError,
    TransientProviderError,
    is_transient_status,
    schema_name,
)

DEFAULT_MAX_TOKENS = 8192
DEFAULT_TIMEOUT_SECONDS = 120.0


def parse_output(message, tool_name: str) -> dict:
    """Message → tool_use 블록의 input. max_tokens 로 잘렸거나 블록이 없으면 ProviderError."""
    if getattr(message, "stop_reason", None) == "max_tokens":
        raise ProviderError("anthropic 응답이 max_tokens 에서 잘림 — max_tokens 를 늘리거나 입력을 줄이세요")
    for block in getattr(message, "content", []) or []:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == tool_name:
            data = block.input
            if not isinstance(data, dict):
                raise ProviderError(f"anthropic tool input 이 object 가 아님: {type(data).__name__}")
            return dict(data)
    raise ProviderError(f"anthropic 응답에 tool_use({tool_name}) 블록이 없음 (stop_reason={getattr(message, 'stop_reason', None)})")


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self,
        api_key: str,
        *,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        import anthropic  # SDK 는 여기서만 import 한다

        self._sdk = anthropic
        self._client = anthropic.Anthropic(api_key=api_key, max_retries=0, timeout=timeout)
        self._max_tokens = max_tokens

    def complete_json(self, system: str, user: str, json_schema: dict, model: str) -> dict:
        sdk = self._sdk
        tool_name = schema_name(json_schema)
        tool = {
            "name": tool_name,
            "description": f"{tool_name} 형식의 결과를 기록한다. 반드시 이 도구를 정확히 한 번 호출해 답한다.",
            "input_schema": json_schema,
        }
        try:
            message = self._client.messages.create(
                model=model,
                max_tokens=self._max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                tools=[tool],
                tool_choice={"type": "tool", "name": tool_name, "disable_parallel_tool_use": True},
            )
        except sdk.APIConnectionError as exc:  # APITimeoutError 포함
            raise TransientProviderError(f"anthropic 연결/타임아웃: {exc}") from exc
        except sdk.APIStatusError as exc:
            text = f"anthropic HTTP {exc.status_code}: {exc}"
            if is_transient_status(exc.status_code):
                raise TransientProviderError(text) from exc
            raise ProviderError(text) from exc
        return parse_output(message, tool_name)
