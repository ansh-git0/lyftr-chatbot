"""Runtime configuration, read once from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _env_list(name: str, default: str = "") -> list[str]:
    raw = os.getenv(name, default)
    return [item.strip() for item in raw.split(",") if item.strip()]


def sqlite_path_from_url(url: str) -> str:
    """Turn ``sqlite:////data/app.db`` / ``sqlite:///./data/app.db`` into a file path."""
    prefix = "sqlite:///"
    if not url.startswith(prefix):
        raise ValueError("DATABASE_URL must start with sqlite:///")
    path = url[len(prefix):]
    if path in {"", ":memory:"}:
        raise ValueError("DATABASE_URL must point to a file")
    return path


DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful, concise assistant. Answer clearly and accurately."
)


@dataclass(frozen=True)
class Settings:
    api_keys: list[str] = field(default_factory=list)
    model: str = "gemini/gemini-3.5-flash-lite"
    # Optional per-key model override, same order as api_keys (blank = `model`).
    key_models: list[str] = field(default_factory=list)
    database_url: str = "sqlite:///./data/app.db"
    allowed_origins: list[str] = field(default_factory=lambda: ["http://localhost:3000"])
    enable_failure_simulation: bool = False
    cache_type: str = "disk"  # "disk" or "local" (in-memory)
    cache_dir: str = ""
    cache_ttl_seconds: int = 86400
    default_system_prompt: str = DEFAULT_SYSTEM_PROMPT
    llm_timeout_seconds: int = 30
    key_cooldown_seconds: int = 60
    max_history_messages: int = 40
    max_output_tokens: int = 2048

    @property
    def database_path(self) -> str:
        return sqlite_path_from_url(self.database_url)

    @property
    def resolved_cache_dir(self) -> str:
        if self.cache_dir:
            return self.cache_dir
        # Keep the disk cache next to the SQLite file so it lives on the same volume.
        return str(Path(self.database_path).parent / "llm_cache")

    def model_for_key(self, index: int) -> str:
        if index < len(self.key_models) and self.key_models[index]:
            return self.key_models[index]
        return self.model


def load_settings() -> Settings:
    return Settings(
        api_keys=_env_list("LLM_API_KEYS"),
        model=os.getenv("LLM_MODEL", "").strip() or "gemini/gemini-3.5-flash-lite",
        key_models=[m.strip() for m in os.getenv("LLM_KEY_MODELS", "").split(",")]
        if os.getenv("LLM_KEY_MODELS", "").strip()
        else [],
        database_url=os.getenv("DATABASE_URL", "").strip() or "sqlite:///./data/app.db",
        allowed_origins=_env_list("ALLOWED_ORIGINS", "http://localhost:3000"),
        enable_failure_simulation=_env_bool("ENABLE_FAILURE_SIMULATION", False),
        cache_type=(os.getenv("LLM_CACHE_TYPE", "").strip().lower() or "disk"),
        cache_dir=os.getenv("LLM_CACHE_DIR", "").strip(),
        cache_ttl_seconds=_env_int("LLM_CACHE_TTL_SECONDS", 86400),
        default_system_prompt=os.getenv("DEFAULT_SYSTEM_PROMPT", "").strip()
        or DEFAULT_SYSTEM_PROMPT,
        llm_timeout_seconds=_env_int("LLM_TIMEOUT_SECONDS", 30),
        key_cooldown_seconds=_env_int("KEY_COOLDOWN_SECONDS", 60),
        max_history_messages=_env_int("MAX_HISTORY_MESSAGES", 40),
        max_output_tokens=_env_int("LLM_MAX_OUTPUT_TOKENS", 2048),
    )
