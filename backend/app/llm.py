"""LiteLLM gateway: ordered key pool with automatic failover + LiteLLM response cache.

Why an explicit failover loop instead of ``litellm.Router``:
the contract needs (a) a deterministic key order (key_1 -> key_2 -> ...),
(b) per-key failure injection for one request (X-Simulate-Failure) and
(c) exact ``key_id`` / ``fallback_attempts`` reporting. All provider calls,
error normalisation and caching still go through LiteLLM.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import litellm
from litellm.caching.caching import Cache

from .config import Settings

log = logging.getLogger("app.llm")

SIMULATABLE_STATUSES = {429, 500, 502, 503}


class AllProvidersUnavailable(Exception):
    """Every key in the pool failed for this request."""


class InvalidSimulationHeader(ValueError):
    pass


@dataclass
class ApiKey:
    key_id: str
    secret: str
    model: str
    cooldown_until: float = 0.0

    def __repr__(self) -> str:  # never leak the secret into logs/tracebacks
        return f"ApiKey(key_id={self.key_id!r}, model={self.model!r})"


@dataclass
class LLMResult:
    reply: str
    model: str
    resolution_source: str  # LIVE_INFERENCE | LLM_CACHE
    input_tokens: int
    output_tokens: int
    key_id: str | None
    fallback_attempts: int


def parse_simulation_header(raw: str | None, key_ids: list[str]) -> dict[str, int]:
    """Parse ``key_1=429,key_2=500`` / ``*=429`` into {key_id: status}."""
    if raw is None or not raw.strip():
        return {}
    result: dict[str, int] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise InvalidSimulationHeader(f"invalid X-Simulate-Failure entry: {part!r}")
        target, _, status_raw = part.partition("=")
        target, status_raw = target.strip(), status_raw.strip()
        try:
            status = int(status_raw)
        except ValueError:
            raise InvalidSimulationHeader(f"invalid status in X-Simulate-Failure: {status_raw!r}")
        if status not in SIMULATABLE_STATUSES:
            raise InvalidSimulationHeader(
                f"unsupported status {status}; use one of {sorted(SIMULATABLE_STATUSES)}"
            )
        if target == "*":
            for key_id in key_ids:
                result[key_id] = status
        elif target:
            result[target] = status  # unknown key ids are harmless no-ops
        else:
            raise InvalidSimulationHeader(f"missing key id in X-Simulate-Failure: {part!r}")
    return result


def simulated_error(status: int, key: ApiKey) -> Exception:
    """Build the same exception LiteLLM raises when a provider returns ``status``."""
    provider = key.model.split("/", 1)[0] if "/" in key.model else "unknown"
    msg = f"Simulated HTTP {status} for {key.key_id} (X-Simulate-Failure)"
    if status == 429:
        return litellm.RateLimitError(message=msg, llm_provider=provider, model=key.model)
    if status == 503:
        return litellm.ServiceUnavailableError(message=msg, llm_provider=provider, model=key.model)
    err = litellm.InternalServerError(message=msg, llm_provider=provider, model=key.model)
    err.status_code = status  # 500 or 502
    return err


def _is_rate_limit(exc: Exception) -> bool:
    if isinstance(exc, litellm.RateLimitError):
        return True
    text = str(exc).lower()
    return "quota" in text or "resource_exhausted" in text or "rate limit" in text


class LLMGateway:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.keys = [
            ApiKey(key_id=f"key_{i + 1}", secret=secret, model=settings.model_for_key(i))
            for i, secret in enumerate(settings.api_keys)
        ]
        self.cache_enabled = False

    # ---- setup -------------------------------------------------------------
    def setup_cache(self) -> None:
        if self.settings.cache_type == "disk":
            litellm.cache = Cache(type="disk", disk_cache_dir=self.settings.resolved_cache_dir,
                                  ttl=self.settings.cache_ttl_seconds)
        else:
            litellm.cache = Cache(type="local", ttl=self.settings.cache_ttl_seconds)
        self.cache_enabled = True

    @property
    def key_ids(self) -> list[str]:
        return [k.key_id for k in self.keys]

    # ---- key ordering ------------------------------------------------------
    def _ordered_keys(self) -> list[ApiKey]:
        """Configured order, but keys in cooldown (recent real 429/quota) go last."""
        now = time.monotonic()
        healthy = [k for k in self.keys if k.cooldown_until <= now]
        cooling = [k for k in self.keys if k.cooldown_until > now]
        return healthy + cooling

    # ---- main entry point --------------------------------------------------
    async def complete(
        self, messages: list[dict[str, str]], simulate: dict[str, int] | None = None
    ) -> LLMResult:
        simulate = simulate or {}
        attempts = 0
        for key in self._ordered_keys():
            call_kwargs: dict[str, Any] = {
                "model": key.model,
                "messages": messages,
                "max_tokens": self.settings.max_output_tokens,
                "timeout": self.settings.llm_timeout_seconds,
                "num_retries": 0,
            }
            try:
                if key.key_id in simulate:
                    raise simulated_error(simulate[key.key_id], key)
                response = await litellm.acompletion(
                    api_key=key.secret,  # not part of the cache key: keys share one cache
                    caching=self.cache_enabled,
                    # A failover demo must exercise the keys, so skip cache *reads*
                    # while X-Simulate-Failure is active (writes still happen).
                    cache={"no-cache": bool(simulate)},
                    **call_kwargs,
                )
                reply = (response.choices[0].message.content or "").strip()
                if not reply:
                    raise ValueError("provider returned an empty reply")
            except Exception as exc:  # noqa: BLE001 - any provider failure triggers failover
                attempts += 1
                status = getattr(exc, "status_code", None)
                simulated = key.key_id in simulate
                if not simulated and _is_rate_limit(exc):
                    key.cooldown_until = time.monotonic() + self.settings.key_cooldown_seconds
                log.warning(
                    "key %s failed (%s, status=%s%s); trying next key",
                    key.key_id, type(exc).__name__, status, ", simulated" if simulated else "",
                )
                continue

            cache_hit = bool((getattr(response, "_hidden_params", None) or {}).get("cache_hit"))
            if not cache_hit and self.cache_enabled and litellm.cache is not None:
                # LiteLLM writes the cache from a background task; write it here too so a
                # repeat request that arrives immediately is guaranteed to hit.
                try:
                    await litellm.cache.async_add_cache(response, **call_kwargs)
                except Exception:  # noqa: BLE001 - cache write failure must not fail the request
                    log.exception("cache write failed")

            usage = getattr(response, "usage", None)
            input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
            output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
            if input_tokens == 0:
                input_tokens = litellm.token_counter(model=key.model, messages=messages)

            if cache_hit:
                return LLMResult(reply, key.model, "LLM_CACHE", input_tokens, output_tokens,
                                 key_id=None, fallback_attempts=0)
            return LLMResult(reply, key.model, "LIVE_INFERENCE", input_tokens, output_tokens,
                             key_id=key.key_id, fallback_attempts=attempts)

        raise AllProvidersUnavailable()
