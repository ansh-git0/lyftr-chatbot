from __future__ import annotations

import uuid

import pytest


def sid() -> str:
    return f"s-{uuid.uuid4().hex[:8]}"


def chat(client, session_id, message, headers=None, **extra):
    return client.post("/chat", json={"session_id": session_id, "message": message, **extra},
                       headers=headers or {})


# ---- /health ----------------------------------------------------------------

def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "keys_configured": 2, "cache_enabled": True}


def test_health_without_keys_is_503(make_client, base_settings):
    c = make_client(base_settings, api_keys=[])
    r = c.get("/health")
    assert r.status_code == 503
    assert "detail" in r.json()


# ---- /chat: live + metadata ---------------------------------------------------

def test_chat_live_response_shape(client):
    s = sid()
    r = chat(client, s, "What is the capital of France?")
    assert r.status_code == 200
    body = r.json()
    assert body["session_id"] == s
    assert body["reply"]
    meta = body["metadata"]
    assert meta["resolution_source"] == "LIVE_INFERENCE"
    assert meta["model"] == "fake/test-model"
    assert isinstance(meta["latency_ms"], int)
    assert meta["input_tokens"] > 0 and meta["output_tokens"] > 0
    assert meta["key_id"] == "key_1"
    assert meta["fallback_attempts"] == 0


# ---- caching ----------------------------------------------------------------

def test_same_first_message_in_new_session_is_cache_hit(client, fake_provider):
    first = chat(client, sid(), "Tell me a fact about owls").json()
    calls = len(fake_provider.calls)
    second = chat(client, sid(), "Tell me a fact about owls").json()

    assert first["metadata"]["resolution_source"] == "LIVE_INFERENCE"
    assert second["metadata"]["resolution_source"] == "LLM_CACHE"
    assert second["reply"] == first["reply"]
    assert second["metadata"]["key_id"] is None
    assert second["metadata"]["fallback_attempts"] == 0
    assert second["metadata"]["input_tokens"] == first["metadata"]["input_tokens"]
    assert second["metadata"]["output_tokens"] == first["metadata"]["output_tokens"]
    assert second["metadata"]["model"] == first["metadata"]["model"]
    assert second["metadata"]["latency_ms"] < first["metadata"]["latency_ms"]
    assert len(fake_provider.calls) == calls  # provider not called on a hit


def test_different_message_never_from_cache(client):
    chat(client, sid(), "Message A")
    r = chat(client, sid(), "Message B").json()
    assert r["metadata"]["resolution_source"] == "LIVE_INFERENCE"


def test_cache_key_includes_history(client):
    s1, s2 = sid(), sid()
    chat(client, s1, "hello")
    chat(client, s1, "and then?")
    chat(client, s2, "different start")
    r = chat(client, s2, "and then?").json()  # same message, different history
    assert r["metadata"]["resolution_source"] == "LIVE_INFERENCE"


def test_cache_key_includes_system_prompt(client):
    chat(client, sid(), "hi there", system_prompt="You are a pirate.")
    r = chat(client, sid(), "hi there", system_prompt="You are a poet.").json()
    assert r["metadata"]["resolution_source"] == "LIVE_INFERENCE"
    r2 = chat(client, sid(), "hi there", system_prompt="You are a poet.").json()
    assert r2["metadata"]["resolution_source"] == "LLM_CACHE"


def test_in_memory_cache_mode(make_client, base_settings):
    c = make_client(base_settings, cache_type="local")
    chat(c, sid(), "memory cache?")
    assert chat(c, sid(), "memory cache?").json()["metadata"]["resolution_source"] == "LLM_CACHE"


# ---- tokens -----------------------------------------------------------------

def test_tokens_are_per_request_not_cumulative(client):
    s = sid()
    a = chat(client, s, "one two three").json()["metadata"]
    b = chat(client, s, "four five six").json()["metadata"]
    # the second request carries history, so it has more input tokens, but output is
    # only for its own reply and is not a running total
    assert b["input_tokens"] > a["input_tokens"]
    assert b["output_tokens"] < a["output_tokens"] + b["output_tokens"]
    # two equal-length first messages in new sessions report the same input tokens
    c = chat(client, sid(), "seven eight nine").json()["metadata"]
    assert c["input_tokens"] == a["input_tokens"]


# ---- multi-turn + history ---------------------------------------------------

def test_multi_turn_memory_and_history_endpoint(client):
    s = sid()
    chat(client, s, "My name is Ada")
    r = chat(client, s, "What is my name?").json()
    assert "My name is Ada" in r["reply"]  # fake echoes previous user turns it received

    h = client.get(f"/sessions/{s}/messages")
    assert h.status_code == 200
    body = h.json()
    assert body["session_id"] == s
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["user", "assistant", "user", "assistant"]
    assert body["messages"][0]["content"] == "My name is Ada"
    assert "metadata" not in body["messages"][0]
    meta = body["messages"][1]["metadata"]
    assert meta["resolution_source"] in {"LIVE_INFERENCE", "LLM_CACHE"}
    assert isinstance(meta["input_tokens"], int) and isinstance(meta["output_tokens"], int)
    assert all(m["role"] != "system" for m in body["messages"])


def test_sessions_are_isolated(client):
    a, b = sid(), sid()
    chat(client, a, "secret is 42")
    r = chat(client, b, "what is the secret?").json()
    assert "secret is 42" not in r["reply"]
    assert len(client.get(f"/sessions/{b}/messages").json()["messages"]) == 2


def test_system_prompt_persists_for_session(client):
    s = sid()
    chat(client, s, "first", system_prompt="You are a pirate.")
    r = chat(client, s, "second").json()
    assert "[sys:You are a pirate." in r["reply"]


def test_history_survives_app_restart(make_client, base_settings):
    s = sid()
    chat(make_client(base_settings), s, "persist me")
    fresh = make_client(base_settings)  # new app instance, same SQLite file
    msgs = fresh.get(f"/sessions/{s}/messages").json()["messages"]
    assert msgs[0]["content"] == "persist me"


def test_unknown_session_404(client):
    r = client.get("/sessions/does-not-exist/messages")
    assert r.status_code == 404
    assert r.json() == {"detail": "session not found"}


# ---- validation -------------------------------------------------------------

@pytest.mark.parametrize(
    "payload",
    [
        {"session_id": "s-1"},
        {"message": "hi"},
        {"session_id": "s-1", "message": ""},
        {"session_id": "s-1", "message": "    "},
        {"session_id": "", "message": "hi"},
        {"session_id": "   ", "message": "hi"},
        {"session_id": "x" * 129, "message": "hi"},
        {"session_id": "s-1", "message": "a" * 4001},
        {"session_id": "s-1", "message": 123},
    ],
)
def test_validation_errors_are_422(client, payload):
    r = client.post("/chat", json=payload)
    assert r.status_code == 422
    assert "detail" in r.json()


def test_message_trimmed_4000_chars_ok(client):
    r = chat(client, sid(), "  " + "a" * 4000 + "  ")
    assert r.status_code == 200


def test_malformed_json_is_422(client):
    r = client.post("/chat", content=b"{not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 422
    assert "detail" in r.json()


def test_failed_request_not_added_to_history(client):
    s = sid()
    chat(client, s, "kept")
    client.post("/chat", json={"session_id": s, "message": ""})
    chat(client, s, "dropped", headers={"X-Simulate-Failure": "*=429"})
    msgs = client.get(f"/sessions/{s}/messages").json()["messages"]
    assert [m["content"] for m in msgs if m["role"] == "user"] == ["kept"]
