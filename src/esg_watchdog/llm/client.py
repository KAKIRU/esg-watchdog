"""LLM 구조화 출력 클라이언트 — temperature 0 · prompt_version · JSON 파일 캐시 · 1회 재시도 (D-28 · D-40).

- complete(schema, system, user, *, model, prompt_version) → schema 인스턴스(Pydantic).
- 캐시 키 = sha256(provider ␟ model ␟ prompt_version ␟ schema.__name__ ␟ system ␟ user) → {llm_cache_dir}/{key}.json 에 원시 JSON.
  히트면 호출 없이 돌려준다. 스키마 검증을 통과한 응답만 저장한다(깨진 응답이 캐시에 남지 않게).
- 일시 오류(TransientProviderError: 429 · 5xx · timeout)는 1회 재시도. 응답이 스키마 검증에 실패하면 ValueError(호출자가 재생성 판단).
- 공급자는 settings.llm_provider(openai | anthropic | fake)로 고르고, LLMClient(provider=...) 로 주입할 수 있다(테스트는 fake).
  키가 비어 있으면 첫 호출에서 LLMConfigError("LLM_PROVIDER/LLM_API_KEY 를 .env 에 넣으세요"). 모델명은 호출자가
  settings.llm_model_extract / llm_model_judge 에서 읽어 넘긴다(코드에 모델명 하드코딩 금지).
- 나머지 코드(F-02~F-06)는 공급자를 모른다 — LLMClient 만 본다. 요청 시점 호출 금지: 배치(CLI)에서만 쓴다.
- Pydantic → JSON schema 변환은 여기서 한 번만(schema.model_json_schema()). 어댑터는 dict 만 받는다.
- settings·SDK 는 함수 안에서만 읽는다 — .env 없이 import 가능.
"""

import hashlib
import json
import os
import time
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, ValidationError

from esg_watchdog.llm.providers import Provider, TransientProviderError

PROVIDER_NAMES = ("openai", "anthropic", "fake")
CONFIG_HINT = "LLM_PROVIDER/LLM_API_KEY 를 .env 에 넣으세요"
MODEL_HINT = "LLM_MODEL_EXTRACT / LLM_MODEL_JUDGE 를 .env 에 넣으세요"
FAKE_MODEL = "fake"
RETRY_DELAY_SECONDS = 2.0
_KEY_SEPARATOR = "\x1f"

Sleep = Callable[[float], None]


class LLMConfigError(RuntimeError):
    """LLM 설정 누락 — 공급자·키·모델명."""


def cache_key(provider: str, model: str, prompt_version: str, schema_name: str, system: str, user: str) -> str:
    joined = _KEY_SEPARATOR.join((provider, model, prompt_version, schema_name, system, user))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def make_provider(name: str, api_key: str, *, fake_responses_path: Path | None = None) -> Provider:
    """이름으로 어댑터를 만든다. SDK 는 해당 어댑터 파일 안에서만 import 된다."""
    if name == "fake":
        from esg_watchdog.llm.providers.fake_provider import FakeProvider

        return FakeProvider.from_file(fake_responses_path) if fake_responses_path else FakeProvider()
    if name not in PROVIDER_NAMES:
        raise LLMConfigError(f"LLM_PROVIDER={name!r} 는 지원하지 않는다 ({' | '.join(PROVIDER_NAMES)}). {CONFIG_HINT}")
    if not api_key:
        raise LLMConfigError(f"LLM_API_KEY 가 비어 있다. {CONFIG_HINT}")
    if name == "openai":
        from esg_watchdog.llm.providers.openai_provider import OpenAIProvider

        return OpenAIProvider(api_key)
    from esg_watchdog.llm.providers.anthropic_provider import AnthropicProvider

    return AnthropicProvider(api_key)


def build_provider() -> Provider:
    """settings 로 어댑터를 고른다. settings 는 여기서만(lazy) 읽는다."""
    from esg_watchdog.config import settings
    from esg_watchdog.llm.providers.fake_provider import FILE_NAME

    name = (settings.llm_provider or "").strip().lower()
    if not name:
        raise LLMConfigError(f"LLM_PROVIDER 가 비어 있다. {CONFIG_HINT}")
    return make_provider(name, settings.llm_api_key, fake_responses_path=Path(settings.llm_cache_dir) / FILE_NAME)


class LLMClient:
    def __init__(
        self,
        provider: Provider | None = None,
        *,
        cache_dir: str | Path | None = None,
        sleep: Sleep = time.sleep,
    ) -> None:
        self._provider = provider
        self._cache_dir = Path(cache_dir) if cache_dir is not None else None
        self._sleep = sleep
        self.stats = {"calls": 0, "cache_hits": 0, "retries": 0}

    @property
    def provider(self) -> Provider:
        if self._provider is None:
            self._provider = build_provider()
        return self._provider

    @property
    def cache_dir(self) -> Path:
        if self._cache_dir is None:
            from esg_watchdog.config import settings

            self._cache_dir = Path(settings.llm_cache_dir)
        return self._cache_dir

    def cache_path(self, schema: type[BaseModel], system: str, user: str, *, model: str, prompt_version: str) -> Path:
        key = cache_key(self.provider.name, model, prompt_version, schema.__name__, system, user)
        return self.cache_dir / f"{key}.json"

    def complete(
        self,
        schema: type[BaseModel],
        system: str,
        user: str,
        *,
        model: str,
        prompt_version: str,
    ) -> BaseModel:
        provider = self.provider
        if not model:
            if provider.name != "fake":
                raise LLMConfigError(f"모델명이 비어 있다. {MODEL_HINT}")
            model = FAKE_MODEL

        path = self.cache_path(schema, system, user, model=model, prompt_version=prompt_version)
        if path.is_file():
            self.stats["cache_hits"] += 1
            return self._validate(schema, json.loads(path.read_text(encoding="utf-8")))

        raw = self._call(system, user, schema.model_json_schema(), model)
        result = self._validate(schema, raw)
        self._write_cache(path, raw)
        return result

    def _call(self, system: str, user: str, json_schema: dict, model: str) -> dict:
        self.stats["calls"] += 1
        try:
            return self.provider.complete_json(system, user, json_schema, model)
        except TransientProviderError:
            self.stats["retries"] += 1
            self._sleep(RETRY_DELAY_SECONDS)
            self.stats["calls"] += 1
            return self.provider.complete_json(system, user, json_schema, model)

    @staticmethod
    def _validate(schema: type[BaseModel], raw: object) -> BaseModel:
        try:
            return schema.model_validate(raw)
        except ValidationError as exc:
            raise ValueError(f"LLM 응답이 {schema.__name__} 스키마에 맞지 않음: {exc}") from exc

    @staticmethod
    def _write_cache(path: Path, raw: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
