"""Test fixtures: a fake LiteLLM provider so tests run offline but still go through
LiteLLM's real completion + caching code path."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import litellm
import pytest
from fastapi.testclient import TestClient
from litellm import CustomLLM

from app.config import Settings
from app.main import create_app


class FakeProvider(CustomLLM):
    """Behaviour is chosen by the API key:
    ``good-*`` -> answers, ``ratelimited`` -> 429, ``broken`` -> 500."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    async def acompletion(self, *args, **kwargs):
        api_key = kwargs.get("api_key") or ""
        model = kwargs.get("model")
        self.calls.append(api_key)
        await asyncio.sleep(0.05)  # live calls are measurably slower than cache hits
        if api_key == "ratelimited":
            raise litellm.RateLimitError("quota exceeded", llm_provider="fake", model=model)
        if api_key == "broken":
            raise litellm.InternalServerError("boom", llm_provider="fake", model=model)
        messages = kwargs["messages"]
        user_turns = [m["content"] for m in messages if m["role"] == "user"]
        system = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
        reply = f"reply #{len(user_turns)} to '{user_turns[-1]}' (previous: {user_turns[:-1]}) [sys:{system[:20]}]"
        prompt_tokens = sum(len(m["content"].split()) + 4 for m in messages)
        return litellm.ModelResponse(
            model=model,
            choices=[{"message": {"role": "assistant", "content": reply}}],
            usage={"prompt_tokens": prompt_tokens, "completion_tokens": len(reply.split()),
                   "total_tokens": prompt_tokens + len(reply.split())},
        )


@pytest.fixture()
def fake_provider():
    provider = FakeProvider()
    litellm.custom_provider_map = [{"provider": "fake", "custom_handler": provider}]
    yield provider
    litellm.custom_provider_map = []
    litellm.cache = None


@pytest.fixture()
def base_settings(tmp_path) -> Settings:
    return Settings(
        api_keys=["good-1", "good-2"],
        model="fake/test-model",
        database_url=f"sqlite:///{(tmp_path / 'app.db').as_posix()}",
        allowed_origins=["http://localhost:3000"],
        enable_failure_simulation=True,
        cache_type="disk",
        cache_dir=str(tmp_path / "cache"),
    )


@pytest.fixture()
def make_client(fake_provider):
    clients = []

    def _make(settings: Settings, **overrides) -> TestClient:
        client = TestClient(create_app(replace(settings, **overrides)), raise_server_exceptions=False)
        client.__enter__()
        clients.append(client)
        return client

    yield _make
    for c in clients:
        c.__exit__(None, None, None)


@pytest.fixture()
def client(make_client, base_settings) -> TestClient:
    return make_client(base_settings)
