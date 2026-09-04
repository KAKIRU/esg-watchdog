"""가짜 공급자 — 테스트·오프라인 배관 검증용 (D-40).

- 생성자에 넣은 응답(dict 또는 callable)을 순서대로 돌려준다. callable 은 (system, user, json_schema, model) 을 받아 dict 를 돌려준다.
- 큐는 스키마 title 별로 둘 수도 있다(by_schema). 그 스키마의 큐가 있으면 그것을, 없으면 공용 큐를 쓴다.
- settings.llm_provider == "fake" 면 LLMClient 가 FakeProvider.from_file({llm_cache_dir}/fake_responses.json) 로 만든다.
  파일 형식: 리스트(공용 큐) 또는 {"스키마이름": [응답, ...]}(스키마별 큐). 응답은 quote 검사를 통과하도록 원문 문장을 써야 한다.
- 응답이 떨어지면 ProviderError — 조용히 빈 결과를 만들지 않는다.
- 호출 기록은 calls 에 남는다(테스트가 호출 횟수·프롬프트를 확인한다).
"""

import json
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from esg_watchdog.llm.providers import ProviderError

FILE_NAME = "fake_responses.json"

FakeResponse = dict | Callable[[str, str, dict, str], dict]


class FakeProvider:
    name = "fake"

    def __init__(
        self,
        responses: Sequence[FakeResponse] = (),
        *,
        by_schema: Mapping[str, Sequence[FakeResponse]] | None = None,
        source: str = "생성자",
    ) -> None:
        self._queue: deque[FakeResponse] = deque(responses)
        self._by_schema: dict[str, deque[FakeResponse]] = {
            key: deque(value) for key, value in (by_schema or {}).items()
        }
        self._source = source
        self.calls: list[dict] = []

    @classmethod
    def from_file(cls, path: Path) -> "FakeProvider":
        """{llm_cache_dir}/fake_responses.json 을 읽는다. 없으면 빈 큐 — 첫 호출에서 안내 에러."""
        if not path.is_file():
            return cls(source=str(path))
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return cls(data, source=str(path))
        if isinstance(data, dict):
            return cls(by_schema=data, source=str(path))
        raise ProviderError(f"{path}: 최상위는 리스트 또는 {{스키마이름: 리스트}} 여야 한다")

    @property
    def remaining(self) -> int:
        return len(self._queue) + sum(len(queue) for queue in self._by_schema.values())

    def complete_json(self, system: str, user: str, json_schema: dict, model: str) -> dict:
        title = str(json_schema.get("title") or "")
        self.calls.append({"system": system, "user": user, "schema": title, "model": model})
        queue = self._by_schema.get(title)
        if not queue:
            queue = self._queue
        if not queue:
            raise ProviderError(
                f"FakeProvider: 남은 응답이 없다 (schema={title or '-'}, 출처={self._source}). "
                f"LLM_PROVIDER=fake 는 {{LLM_CACHE_DIR}}/{FILE_NAME} 의 응답을 순서대로 돌려준다"
            )
        item = queue.popleft()
        result = item(system, user, json_schema, model) if callable(item) else item
        if not isinstance(result, dict):
            raise ProviderError(f"FakeProvider: 응답은 dict 여야 한다 ({type(result).__name__})")
        return result
