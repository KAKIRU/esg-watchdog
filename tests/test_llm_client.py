"""llm/client.py — 가짜 provider 를 주입해 캐시 히트·미스 · 스키마 실패 · 재시도 · 공급자 선택을 검사한다 (D-28 · D-40)."""

import json

import pytest
from pydantic import BaseModel

from esg_watchdog.llm.client import (
    CONFIG_HINT,
    LLMClient,
    LLMConfigError,
    cache_key,
    make_provider,
)
from esg_watchdog.llm.providers import (
    ProviderError,
    TransientProviderError,
    schema_name,
)
from esg_watchdog.llm.providers.fake_provider import FakeProvider
from esg_watchdog.llm.providers.openai_provider import to_strict_schema


class Item(BaseModel):
    text: str
    score: int | None


class Out(BaseModel):
    items: list[Item]


GOOD = {"items": [{"text": "a", "score": 1}, {"text": "b", "score": None}]}


def make_client(tmp_path, responses=()):
    fake = FakeProvider(responses)
    return LLMClient(provider=fake, cache_dir=tmp_path, sleep=lambda _seconds: None), fake


# --------------------------------------------------------------------------- 캐시
def test_cache_miss_calls_provider_and_writes_raw_json(tmp_path):
    client, fake = make_client(tmp_path, [GOOD])
    result = client.complete(Out, "sys", "user", model="m", prompt_version="v1")

    assert isinstance(result, Out)
    assert [item.text for item in result.items] == ["a", "b"]
    assert len(fake.calls) == 1
    assert fake.calls[0]["model"] == "m"
    assert fake.calls[0]["schema"] == "Out"
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_text(encoding="utf-8")) == GOOD
    assert files[0].stem == cache_key("fake", "m", "v1", "Out", "sys", "user")
    assert client.stats == {"calls": 1, "cache_hits": 0, "retries": 0}


def test_cache_hit_returns_without_calling_provider(tmp_path):
    first, _ = make_client(tmp_path, [GOOD])
    first.complete(Out, "sys", "user", model="m", prompt_version="v1")

    # 응답이 하나도 없는 provider — 호출되면 ProviderError 가 난다
    second, fake = make_client(tmp_path)
    result = second.complete(Out, "sys", "user", model="m", prompt_version="v1")
    assert result == Out.model_validate(GOOD)
    assert fake.calls == []
    assert second.stats["cache_hits"] == 1


def test_cache_key_depends_on_every_part():
    base = cache_key("fake", "m", "v1", "Out", "sys", "user")
    assert base != cache_key("openai", "m", "v1", "Out", "sys", "user")
    assert base != cache_key("fake", "m2", "v1", "Out", "sys", "user")
    assert base != cache_key("fake", "m", "v2", "Out", "sys", "user")
    assert base != cache_key("fake", "m", "v1", "Other", "sys", "user")
    assert base != cache_key("fake", "m", "v1", "Out", "sys2", "user")
    assert base != cache_key("fake", "m", "v1", "Out", "sys", "user2")
    assert len(base) == 64


def test_prompt_version_change_is_a_cache_miss(tmp_path):
    client, fake = make_client(tmp_path, [GOOD, GOOD])
    client.complete(Out, "sys", "user", model="m", prompt_version="v1")
    client.complete(Out, "sys", "user", model="m", prompt_version="v2")
    assert len(fake.calls) == 2
    assert len(list(tmp_path.glob("*.json"))) == 2


# --------------------------------------------------------------------------- 스키마 검증
def test_schema_failure_raises_value_error_and_is_not_cached(tmp_path):
    client, fake = make_client(tmp_path, [{"items": [{"text": 1}]}, GOOD])
    with pytest.raises(ValueError, match="Out 스키마"):
        client.complete(Out, "sys", "user", model="m", prompt_version="v1")
    assert list(tmp_path.glob("*.json")) == []

    # 같은 입력을 다시 부르면 캐시가 없으므로 provider 를 다시 호출한다
    result = client.complete(Out, "sys", "user", model="m", prompt_version="v1")
    assert isinstance(result, Out)
    assert len(fake.calls) == 2


# --------------------------------------------------------------------------- 재시도
def test_transient_error_is_retried_once(tmp_path):
    def flaky(system, user, json_schema, model):
        raise TransientProviderError("429")

    client, fake = make_client(tmp_path, [flaky, GOOD])
    result = client.complete(Out, "sys", "user", model="m", prompt_version="v1")
    assert isinstance(result, Out)
    assert len(fake.calls) == 2
    assert client.stats == {"calls": 2, "cache_hits": 0, "retries": 1}


def test_second_transient_error_propagates(tmp_path):
    def flaky(system, user, json_schema, model):
        raise TransientProviderError("503")

    client, fake = make_client(tmp_path, [flaky, flaky, GOOD])
    with pytest.raises(TransientProviderError):
        client.complete(Out, "sys", "user", model="m", prompt_version="v1")
    assert len(fake.calls) == 2
    assert list(tmp_path.glob("*.json")) == []


def test_permanent_provider_error_is_not_retried(tmp_path):
    def broken(system, user, json_schema, model):
        raise ProviderError("401")

    client, fake = make_client(tmp_path, [broken, GOOD])
    with pytest.raises(ProviderError):
        client.complete(Out, "sys", "user", model="m", prompt_version="v1")
    assert len(fake.calls) == 1


# --------------------------------------------------------------------------- 공급자 선택 · 가짜 공급자
def test_make_provider_requires_key_and_known_name():
    with pytest.raises(LLMConfigError, match=CONFIG_HINT):
        make_provider("openai", "")
    with pytest.raises(LLMConfigError, match=CONFIG_HINT):
        make_provider("anthropic", "")
    with pytest.raises(LLMConfigError, match="지원하지 않는다"):
        make_provider("gemini", "key")


def test_make_provider_builds_sdk_adapters_without_network():
    assert make_provider("openai", "test-key").name == "openai"
    assert make_provider("anthropic", "test-key").name == "anthropic"
    assert make_provider("fake", "").name == "fake"


def test_empty_model_is_allowed_only_for_fake(tmp_path):
    client, fake = make_client(tmp_path, [GOOD])
    client.complete(Out, "sys", "user", model="", prompt_version="v1")
    assert fake.calls[0]["model"] == "fake"

    class NotFake(FakeProvider):
        name = "openai"

    strict = LLMClient(provider=NotFake([GOOD]), cache_dir=tmp_path)
    with pytest.raises(LLMConfigError, match="LLM_MODEL_EXTRACT"):
        strict.complete(Out, "sys", "user", model="", prompt_version="v1")


def test_fake_provider_exhausted_gives_clear_error(tmp_path):
    client, _ = make_client(tmp_path)
    with pytest.raises(ProviderError, match="fake_responses.json"):
        client.complete(Out, "sys", "user", model="m", prompt_version="v1")


def test_fake_provider_from_file_supports_list_and_per_schema_queues(tmp_path):
    missing = FakeProvider.from_file(tmp_path / "none.json")
    assert missing.remaining == 0

    as_list = tmp_path / "list.json"
    as_list.write_text(json.dumps([GOOD]), encoding="utf-8")
    assert FakeProvider.from_file(as_list).complete_json("s", "u", {"title": "Out"}, "m") == GOOD

    per_schema = tmp_path / "dict.json"
    per_schema.write_text(json.dumps({"Out": [GOOD], "Other": [{"x": 1}]}), encoding="utf-8")
    provider = FakeProvider.from_file(per_schema)
    assert provider.complete_json("s", "u", {"title": "Other"}, "m") == {"x": 1}
    assert provider.complete_json("s", "u", {"title": "Out"}, "m") == GOOD
    with pytest.raises(ProviderError):
        provider.complete_json("s", "u", {"title": "Out"}, "m")


# --------------------------------------------------------------------------- OpenAI strict 스키마
def test_to_strict_schema_closes_objects_and_requires_all_properties():
    class WithDefault(BaseModel):
        name: str
        note: str | None = None
        items: list[Item] = []

    original = WithDefault.model_json_schema()
    strict = to_strict_schema(original)

    assert strict["additionalProperties"] is False
    assert strict["required"] == ["name", "note", "items"]
    item_def = strict["$defs"]["Item"]
    assert item_def["additionalProperties"] is False
    assert item_def["required"] == ["text", "score"]
    assert "default" not in strict["properties"]["note"]
    assert "default" not in strict["properties"]["items"]
    # 원본은 그대로
    assert "additionalProperties" not in original
    assert original["required"] == ["name"]


def test_schema_name_is_provider_safe():
    assert schema_name({"title": "Out"}) == "Out"
    assert schema_name({"title": "공약 목록"}) == "response"
    assert schema_name({"title": "a b.c"}) == "a_b_c"
    assert schema_name({}) == "response"
    assert len(schema_name({"title": "x" * 100})) == 64
