# Lyftr LLM Chatbot: Key Failover and Response Caching

A multi-turn chatbot. The React frontend talks to a FastAPI backend. The backend calls the LLM through **LiteLLM**, using a pool of API keys with automatic failover and LiteLLM's built-in response cache. Session history is stored in **SQLite**.

```
React (Vite, nginx)  ──HTTP/JSON──►  FastAPI
                                       ├── SQLite (/data/app.db)     sessions + messages
                                       └── LLMGateway
                                             ├── LiteLLM Cache (disk, /data/llm_cache)
                                             └── key_1 → key_2 → … (failover on 429 / quota / 5xx)
```

## 1. Live URLs

| Tier | URL |
|---|---|
| Frontend | `https://<your-frontend>.vercel.app` |
| Backend | `https://<your-backend>.onrender.com` (`/health`) |
| Repository | `https://github.com/<you>/lyftr-chatbot` |
| Demo video | `<link>` |

## 2. Run locally

### With Docker (recommended)

```bash
cp .env.example .env        # then put two real keys in LLM_API_KEYS
make up                     # frontend http://localhost:3000, backend http://localhost:8000
make down                   # stop; the chat-data volume (SQLite + cache) is kept
```

Other targets: `make logs`, `make test` (runs the backend tests in a container), `make clean` (also deletes the volume).

If you don't have `make` (for example on Windows), run `docker compose up -d --build` and `docker compose down` instead.

### Without Docker

Backend (Python 3.11+):

```bash
cd backend
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
export LLM_API_KEYS="key-one,key-two" DATABASE_URL="sqlite:///./data/app.db" ENABLE_FAILURE_SIMULATION=true
uvicorn app.main:app --reload --port 8000
pytest -q                                              # 45 offline tests
```

Frontend (Node 18+):

```bash
cd frontend
npm install
VITE_API_BASE_URL=http://localhost:8000 npm run dev    # http://localhost:3000
```

### Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `LLM_API_KEYS` | (none) | Comma-separated key pool, at least 2. Ids are `key_1`, `key_2`, … in this order |
| `LLM_MODEL` | `gemini/gemini-3.5-flash` | LiteLLM model name |
| `LLM_KEY_MODELS` | (empty) | Optional per-key model override, in the same order as the keys (lets you span several models or providers) |
| `DATABASE_URL` | `sqlite:///./data/app.db` (`sqlite:////data/app.db` in Docker) | SQLite file location |
| `ALLOWED_ORIGINS` | `http://localhost:3000` | Comma-separated CORS origins |
| `ENABLE_FAILURE_SIMULATION` | `false` | `true` enables the `X-Simulate-Failure` header |
| `LLM_CACHE_TYPE` | `disk` | `disk` (persists on the volume) or `local` (in-memory) |
| `LLM_CACHE_DIR` / `LLM_CACHE_TTL_SECONDS` | next to the DB / `86400` | Cache location and TTL |
| `DEFAULT_SYSTEM_PROMPT` | built-in | Used when a session has no prompt of its own |
| `LLM_TIMEOUT_SECONDS`, `LLM_MAX_OUTPUT_TOKENS` | `30`, `2048` (Gemini 3.x thinking tokens count as output) | Per-call limits |
| `KEY_COOLDOWN_SECONDS` | `60` | How long a key that hit a real 429 or quota error is moved to the back of the pool |
| `MAX_HISTORY_MESSAGES` | `40` | How many history messages are sent to the LLM |
| `USE_SYSTEM_CERTS` | `false` | Local dev only: trust the OS certificate store (for TLS-inspecting antivirus or proxies) |
| `VITE_API_BASE_URL` (frontend) | `http://localhost:8000` | Backend URL the browser calls |

### Deploying

- **Backend on Render:** create a *Blueprint* from this repo. It reads `render.yaml`, which uses the Docker runtime with `rootDir: backend`. Set `LLM_API_KEYS` and `ALLOWED_ORIGINS` (your Vercel URL) in the dashboard. Render's free plan has no persistent disk, so `/data` resets on each redeploy. On a paid plan, attach a disk at `/data`. Fly.io or Railway with a volume also work unchanged.
- **Frontend on Vercel:** import the repo, set *Root Directory* to `frontend` and the framework to Vite, and add the env var `VITE_API_BASE_URL=https://<backend>.onrender.com`.

## 3. Design decisions

### Key fallback (`backend/app/llm.py`)

- `LLM_API_KEYS` becomes an ordered pool: `key_1`, `key_2`, …. Each request walks the pool in order and calls `litellm.acompletion(api_key=…, num_retries=0)`. Any provider failure moves on to the next key. That includes LiteLLM's normalised `RateLimitError` (429 or quota), `InternalServerError`, `ServiceUnavailableError`, timeouts, connection errors, auth errors and empty replies. `fallback_attempts` counts the keys that failed first.
- If every key fails, the backend returns `503 {"detail": "all providers unavailable"}`. Nothing is written to SQLite, because the user and assistant messages are saved together in one transaction only after a successful reply.
- **Why not `litellm.Router`?** The contract needs a deterministic order (`key_1=429` must give `key_2`), failures injected per key for a single request, and exact `key_id` and `fallback_attempts` reporting. Router's load balancing, retries and cooldowns hide which deployment answered and don't support per-request, per-key fault injection. So the routing loop (about 40 lines) is ours. Provider calls, error mapping and caching still go through LiteLLM.
- **Cooldown:** a key that hits a *real* 429 or quota error goes to the back of the order for `KEY_COOLDOWN_SECONDS`. Later requests then skip a key that is known to be exhausted instead of paying a failed round-trip each time. Simulated failures never trigger the cooldown, so they only affect their own request.
- **`X-Simulate-Failure`** (only when `ENABLE_FAILURE_SIMULATION=true`): parsed into `{key_id: status}`. For each listed key, the gateway raises the same LiteLLM exception the provider would have caused (`RateLimitError` for 429, and so on), so the same failover code runs. A malformed header returns 422. Unknown key ids are ignored.
- **Secrets:** keys only ever appear as `key_N`. `ApiKey.__repr__` hides the secret. The `httpx` INFO logger is silenced because Gemini sends the key in the URL query (`?key=…`).

### Caching

- LiteLLM's built-in `Cache` is used, on **disk** by default (`diskcache`, stored on the `/data` volume, so it survives restarts). `local` (in-memory) is also supported. `/health` reports `cache_enabled`.
- **Cache key:** LiteLLM's key is built from `model` plus the full `messages` list (system prompt, history and the new message) plus generation params. It does **not** include the API key, so all keys in the pool share one cache. As a result, the same first message in a new session is a hit, a different message never is, and the same message with different history or a different system prompt is a miss. Tests cover all of these cases.
- `resolution_source` comes from LiteLLM's `response._hidden_params["cache_hit"]`. On a hit the reply text and token usage are the ones stored with the cached response, `key_id` is `null` and `fallback_attempts` is `0`.
- **Race fix:** LiteLLM writes the cache in a background task after it returns. A repeat request that arrives right away could miss. After each live response the gateway also awaits `litellm.cache.async_add_cache(...)` with the exact same kwargs, so the very next identical request is guaranteed to hit.
- When `X-Simulate-Failure` is present, cache **reads** are skipped (`cache={"no-cache": True}`). Otherwise a cached prompt would never reach the keys, and the failover demo would show `LLM_CACHE` instead of the expected `key_2`.

### SQLite session storage (`backend/app/db.py`)

- Two tables, created on startup with `CREATE TABLE IF NOT EXISTS`:
  - `sessions(session_id, system_prompt, created_at, updated_at)`
  - the required `messages(id, session_id, role, content, metadata JSON, created_at)`, with an index on `(session_id, id)`
- WAL mode is on, and each operation opens a short-lived connection (run in a threadpool), so the code needs no shared-connection locking.
- The client sends only the new message. The server loads the session's history (the last `MAX_HISTORY_MESSAGES`), prepends the system prompt and sends the full list. Turns within one session are serialised with a per-session `asyncio.Lock`, so two concurrent sends can't interleave history.
- **System prompt:** an optional `system_prompt` on `/chat` overrides the default and is **saved on the session**, so later turns keep it. It is never returned by the history endpoint.
- **Errors:** every error is JSON with `detail`. This covers FastAPI's 422s (including malformed JSON), the 503 above, the 404 for unknown sessions, and a catch-all handler that turns any unexpected exception into `500 {"detail": "internal server error"}` with no stack trace.
- `latency_ms` is measured on the server with `perf_counter` over the whole request.

## 4. Verification

Replace `$API` with `http://localhost:8000` or the deployed backend URL.

```bash
# health
curl $API/health
# → {"status":"ok","keys_configured":2,"cache_enabled":true}

# 1) live call
curl -s -X POST $API/chat -H 'Content-Type: application/json' \
  -d '{"session_id":"demo-a","message":"What is the capital of France?"}'
# → resolution_source LIVE_INFERENCE, key_id key_1, latency ~hundreds of ms

# 2) cache hit: same first message in a NEW session
curl -s -X POST $API/chat -H 'Content-Type: application/json' \
  -d '{"session_id":"demo-b","message":"What is the capital of France?"}'
# → resolution_source LLM_CACHE, identical reply, same tokens, key_id null, latency a few ms

# 3) failover
curl -s -X POST $API/chat -H 'Content-Type: application/json' -H 'X-Simulate-Failure: key_1=429' \
  -d '{"session_id":"demo-c","message":"Name a prime number"}'
# → 200, key_id key_2, fallback_attempts 1   (same with key_1=500)

curl -s -i -X POST $API/chat -H 'Content-Type: application/json' -H 'X-Simulate-Failure: *=429' \
  -d '{"session_id":"demo-d","message":"Hello"}'
# → 503 {"detail":"all providers unavailable"}; GET /sessions/demo-d/messages → 404 (nothing stored)

# 4) multi-turn + history
curl -s -X POST $API/chat -H 'Content-Type: application/json' -d '{"session_id":"demo-e","message":"My name is Ada."}'
curl -s -X POST $API/chat -H 'Content-Type: application/json' -d '{"session_id":"demo-e","message":"What is my name?"}'
curl -s $API/sessions/demo-e/messages

# 5) validation
curl -s -X POST $API/chat -H 'Content-Type: application/json' -d '{"session_id":"x","message":"   "}'   # 422
curl -s $API/sessions/nope/messages                                                               # 404
```

**In the UI:**

- Every assistant bubble shows chips for `source` (green LIVE_INFERENCE, purple LLM_CACHE), `model`, `latency`, `in` and `out` tokens for that request, and the `key` that answered. A `failovers` chip appears when `fallback_attempts > 0`.
- To see a cache hit, click **New chat** (or reload the page) and send the same first message again.
- To see failover, open **Settings → Failover test**, choose `key_1 → 429` and send a message. The reply shows `key key_2` and `failovers 1`. Choosing `All keys → 429` shows a readable 503 error in the chat.
- Token usage is per request. The second turn of a conversation has more input tokens because it carries history, and each reply's output tokens cover only that reply.

**Automated tests** (`backend/tests`, 45 tests, run offline): a fake LiteLLM provider is registered through `litellm.custom_provider_map`, so the real LiteLLM completion and cache code runs without network access. The tests cover:

- the response shape
- cache hit and miss rules (history, system prompt, disk and memory)
- identical replies and tokens on a hit, and that hits are faster than live calls
- per-request tokens
- multi-turn memory and session isolation
- persistence across an app restart
- every 422 case
- 404 for unknown sessions
- 503 when all keys fail, with nothing stored
- simulated 429, 500, 502 and 503 failover
- header scoping and the disabled switch
- real provider 429 with cooldown, and real 5xx failover
- that secrets never appear in responses

## 5. Setup Used

- Editor: *(fill in, e.g. VS Code)*
- AI tools: Claude Code (Claude Opus 5.5) for scaffolding, implementation and tests, with manual review of each design decision.

## 6. Bonus features

- **Failover demo control in the UI:** the `X-Simulate-Failure` presets in Settings, so failover can be shown without curl.
- **Key cooldown** after real 429 or quota errors, so a key that is known to be exhausted isn't retried on every request.
- **Persistent disk cache** on the same volume as SQLite, so cache hits survive restarts.
- **System prompt per session** (`system-prompt-input`), saved in SQLite.
- **Per-key model override** (`LLM_KEY_MODELS`), so the pool can span several models or providers.
- **Extra metadata in the UI:** `key_id` and failover count shown next to each reply, plus a backend health badge.
- **Hardening:**
  - secrets are kept out of logs
  - turns are serialised per session
  - history sent to the LLM is capped
  - containers run as a non-root user with a healthcheck
  - the frontend has runtime config (`API_BASE_URL`), so one image can point at any backend
