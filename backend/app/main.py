"""FastAPI application: /health, /chat, /sessions/{session_id}/messages."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import Optional

if os.getenv("USE_SYSTEM_CERTS", "").strip().lower() in {"1", "true", "yes"}:
    # Local dev behind TLS-inspecting proxies/antivirus: trust the OS certificate store.
    import truststore

    truststore.inject_into_ssl()

import litellm
from fastapi import FastAPI, Header, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import Settings, load_settings
from .db import Database
from .llm import AllProvidersUnavailable, InvalidSimulationHeader, LLMGateway, parse_simulation_header
from .schemas import ChatMetadata, ChatRequest, ChatResponse, HealthResponse

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")

# LiteLLM is chatty and may print request details; keep it quiet.
logging.getLogger("LiteLLM").setLevel(logging.WARNING)
# httpx logs full request URLs at INFO; Gemini passes the API key as ?key=..., so never log them.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
litellm.suppress_debug_info = True


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or load_settings()
    db = Database(settings.database_path)
    gateway = LLMGateway(settings)
    session_locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        db.init()
        gateway.setup_cache()
        if len(gateway.keys) < 2:
            log.warning("LLM_API_KEYS has %d key(s); at least 2 are expected for failover",
                        len(gateway.keys))
        log.info("started: %d key(s), model=%s, cache=%s, failure_simulation=%s",
                 len(gateway.keys), settings.model, settings.cache_type,
                 settings.enable_failure_simulation)
        yield

    app = FastAPI(title="Lyftr LLM Chatbot", version="1.0.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.db = db
    app.state.gateway = gateway

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    @app.exception_handler(Exception)
    async def unhandled_exception(_: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error: %s", type(exc).__name__)
        return JSONResponse(status_code=500, content={"detail": "internal server error"})

    @app.get("/health", response_model=HealthResponse)
    async def health():
        db_ok = await run_in_threadpool(db.ping)
        keys = len(gateway.keys)
        if not db_ok or keys == 0:
            return JSONResponse(
                status_code=503,
                content={"status": "unavailable", "keys_configured": keys,
                         "cache_enabled": gateway.cache_enabled,
                         "detail": "database unreachable" if not db_ok else "no API keys configured"},
            )
        return HealthResponse(status="ok", keys_configured=keys, cache_enabled=gateway.cache_enabled)

    @app.post("/chat", response_model=ChatResponse)
    async def chat(
        body: ChatRequest,
        x_simulate_failure: Optional[str] = Header(default=None, alias="X-Simulate-Failure"),
    ):
        started = time.perf_counter()

        simulate: dict[str, int] = {}
        if settings.enable_failure_simulation and x_simulate_failure:
            try:
                simulate = parse_simulation_header(x_simulate_failure, gateway.key_ids)
            except InvalidSimulationHeader as exc:
                return JSONResponse(status_code=422, content={"detail": str(exc)})

        # Serialise turns within one session so history stays consistent.
        async with session_locks[body.session_id]:
            session = await run_in_threadpool(db.get_session, body.session_id)
            stored_prompt = session["system_prompt"] if session else None
            override = body.system_prompt.strip() if body.system_prompt else ""
            session_prompt = override or stored_prompt  # persisted per session
            system_prompt = session_prompt or settings.default_system_prompt

            history = await run_in_threadpool(
                db.get_history, body.session_id, settings.max_history_messages
            ) if session else []

            messages = [{"role": "system", "content": system_prompt}]
            messages += [{"role": m["role"], "content": m["content"]} for m in history]
            messages.append({"role": "user", "content": body.message})

            try:
                result = await gateway.complete(messages, simulate)
            except AllProvidersUnavailable:
                return JSONResponse(status_code=503, content={"detail": "all providers unavailable"})

            latency_ms = int(round((time.perf_counter() - started) * 1000))
            metadata = ChatMetadata(
                resolution_source=result.resolution_source,
                model=result.model,
                latency_ms=latency_ms,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                key_id=result.key_id,
                fallback_attempts=result.fallback_attempts,
            )
            await run_in_threadpool(
                db.save_exchange, body.session_id, session_prompt, body.message,
                result.reply, metadata.model_dump(),
            )

        return ChatResponse(session_id=body.session_id, reply=result.reply, metadata=metadata)

    @app.get("/sessions/{session_id}/messages")
    async def session_messages(session_id: str):
        session = await run_in_threadpool(db.get_session, session_id)
        if session is None:
            return JSONResponse(status_code=404, content={"detail": "session not found"})
        messages = await run_in_threadpool(db.get_history, session_id)
        return {"session_id": session_id, "messages": messages}

    return app


app = create_app()
