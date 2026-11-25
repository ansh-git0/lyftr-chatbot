from __future__ import annotations

import uuid

import pytest

from app.llm import InvalidSimulationHeader, parse_simulation_header


def sid() -> str:
    return f"s-{uuid.uuid4().hex[:8]}"


def chat(client, message, header=None, session_id=None):
    headers = {"X-Simulate-Failure": header} if header else {}
    return client.post("/chat", json={"session_id": session_id or sid(), "message": message},
                       headers=headers)


# ---- header parsing -----------------------------------------------------------

def test_parse_header_variants():
    ids = ["key_1", "key_2"]
    assert parse_simulation_header("key_1=429", ids) == {"key_1": 429}
    assert parse_simulation_header("key_1=429, key_2=500", ids) == {"key_1": 429, "key_2": 500}
    assert parse_simulation_header("*=503", ids) == {"key_1": 503, "key_2": 503}
    assert parse_simulation_header("", ids) == {}


@pytest.mark.parametrize("bad", ["key_1", "key_1=404", "key_1=abc", "=429"])
def test_parse_header_rejects_bad_values(bad):
    with pytest.raises(InvalidSimulationHeader):
        parse_simulation_header(bad, ["key_1", "key_2"])


# ---- simulated failover -------------------------------------------------------

@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_key_1_failure_falls_over_to_key_2(client, status):
    r = chat(client, f"failover {status}", f"key_1={status}")
    assert r.status_code == 200
    meta = r.json()["metadata"]
    assert meta["key_id"] == "key_2"
    assert meta["fallback_attempts"] == 1
    assert meta["resolution_source"] == "LIVE_INFERENCE"
    assert r.json()["reply"].startswith("reply #1")  # a real answer, not an error string


def test_all_keys_fail_returns_503_and_nothing_stored(client):
    s = sid()
    r = chat(client, "nobody home", "*=429", session_id=s)
    assert r.status_code == 503
    assert r.json() == {"detail": "all providers unavailable"}
    assert client.get(f"/sessions/{s}/messages").status_code == 404


def test_every_listed_key_failing_is_503(client):
    r = chat(client, "both down", "key_1=429,key_2=500")
    assert r.status_code == 503
    assert r.json() == {"detail": "all providers unavailable"}


def test_simulation_is_scoped_to_one_request(client):
    assert chat(client, "scoped a", "key_1=429").json()["metadata"]["key_id"] == "key_2"
    assert chat(client, "scoped b").json()["metadata"]["key_id"] == "key_1"


def test_simulation_bypasses_cache_reads(client):
    chat(client, "cached question")
    assert chat(client, "cached question").json()["metadata"]["resolution_source"] == "LLM_CACHE"
    r = chat(client, "cached question", "key_1=429").json()["metadata"]
    assert r["resolution_source"] == "LIVE_INFERENCE"
    assert r["key_id"] == "key_2"


def test_header_ignored_when_simulation_disabled(make_client, base_settings):
    c = make_client(base_settings, enable_failure_simulation=False)
    r = chat(c, "ignored", "*=429")
    assert r.status_code == 200
    assert r.json()["metadata"]["key_id"] == "key_1"


def test_invalid_header_is_422(client):
    r = chat(client, "bad header", "key_1=418")
    assert r.status_code == 422
    assert "detail" in r.json()


# ---- real provider failures ---------------------------------------------------

def test_real_rate_limit_fails_over_and_cools_down(make_client, base_settings, fake_provider):
    c = make_client(base_settings, api_keys=["ratelimited", "good-2"])
    r = chat(c, "real 429").json()["metadata"]
    assert r["key_id"] == "key_2" and r["fallback_attempts"] == 1
    # key_1 is now cooling down, so the next request goes straight to key_2
    fake_provider.calls.clear()
    r2 = chat(c, "after cooldown").json()["metadata"]
    assert r2["key_id"] == "key_2" and r2["fallback_attempts"] == 0
    assert fake_provider.calls == ["good-2"]


def test_real_5xx_fails_over(make_client, base_settings):
    c = make_client(base_settings, api_keys=["broken", "good-2", "good-3"])
    r = chat(c, "real 500").json()["metadata"]
    assert r["key_id"] == "key_2" and r["fallback_attempts"] == 1


def test_all_real_failures_503(make_client, base_settings):
    c = make_client(base_settings, api_keys=["broken", "ratelimited"])
    r = chat(c, "all broken")
    assert r.status_code == 503
    assert r.json() == {"detail": "all providers unavailable"}


def test_secrets_never_returned(make_client, base_settings):
    c = make_client(base_settings, api_keys=["good-SECRET-1", "good-SECRET-2"])
    s = sid()
    r = chat(c, "leak check", session_id=s)
    assert "SECRET" not in r.text
    assert "SECRET" not in c.get(f"/sessions/{s}/messages").text
    assert "SECRET" not in c.get("/health").text
