// Backend base URL: runtime config (Docker) > build-time env (Vercel/Netlify) > local default.
const runtime = typeof window !== "undefined" ? window.__APP_CONFIG__ || {} : {};
export const API_BASE_URL = (
  runtime.API_BASE_URL ||
  import.meta.env.VITE_API_BASE_URL ||
  "http://localhost:8000"
).replace(/\/+$/, "");

export function newSessionId() {
  const rand =
    typeof crypto !== "undefined" && crypto.randomUUID
      ? crypto.randomUUID().replace(/-/g, "")
      : Math.random().toString(16).slice(2) + Date.now().toString(16);
  return `s-${rand.slice(0, 12)}`;
}

/** Turn any error body into one readable sentence. */
function readableError(status, body) {
  const detail = body && body.detail;
  if (typeof detail === "string") {
    if (status === 503) return "All AI providers are unavailable right now. Please try again in a moment.";
    return detail;
  }
  if (Array.isArray(detail) && detail.length) {
    // FastAPI 422: [{loc, msg, ...}]
    return detail
      .map((d) => {
        const field = Array.isArray(d.loc) ? d.loc.filter((p) => p !== "body").join(".") : "";
        const msg = String(d.msg || "invalid value").replace(/^Value error, /, "");
        return field ? `${field}: ${msg}` : msg;
      })
      .join("; ");
  }
  return `Request failed with status ${status}.`;
}

export class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

async function request(path, options = {}) {
  let res;
  try {
    res = await fetch(`${API_BASE_URL}${path}`, options);
  } catch {
    throw new ApiError(0, "Could not reach the server. Check your connection and try again.");
  }
  let body = null;
  try {
    body = await res.json();
  } catch {
    body = null;
  }
  if (!res.ok) throw new ApiError(res.status, readableError(res.status, body));
  return body;
}

export function sendChat({ sessionId, message, systemPrompt, simulateFailure }) {
  const headers = { "Content-Type": "application/json" };
  if (simulateFailure) headers["X-Simulate-Failure"] = simulateFailure;
  const payload = { session_id: sessionId, message };
  if (systemPrompt && systemPrompt.trim()) payload.system_prompt = systemPrompt;
  return request("/chat", { method: "POST", headers, body: JSON.stringify(payload) });
}

export function getHealth() {
  return request("/health");
}
