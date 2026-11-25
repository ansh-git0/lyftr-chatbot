import { useEffect, useRef, useState } from "react";
import { API_BASE_URL, getHealth, newSessionId, sendChat } from "./api.js";

const FAILURE_PRESETS = [
  { value: "", label: "Off (normal request)" },
  { value: "key_1=429", label: "key_1 → 429 (rate limit)" },
  { value: "key_1=500", label: "key_1 → 500 (server error)" },
  { value: "key_1=503", label: "key_1 → 503 (unavailable)" },
  { value: "*=429", label: "All keys → 429 (expect 503)" },
];

function MetaItem({ label, testId, children, tone }) {
  return (
    <span className={`meta-item${tone ? ` meta-${tone}` : ""}`}>
      <span className="meta-label">{label}</span>
      <span className="meta-value" data-testid={testId}>
        {children}
      </span>
    </span>
  );
}

function AssistantMessage({ msg }) {
  const m = msg.metadata;
  const cached = m.resolution_source === "LLM_CACHE";
  return (
    <div className="row row-assistant">
      <div className="bubble bubble-assistant" data-testid="assistant-message">
        <div className="reply-text">{msg.content}</div>
        <div className="meta">
          <MetaItem label="source" testId="meta-source" tone={cached ? "cache" : "live"}>
            {m.resolution_source}
          </MetaItem>
          <MetaItem label="model" testId="meta-model">{m.model}</MetaItem>
          <MetaItem label="latency" testId="meta-latency">{m.latency_ms} ms</MetaItem>
          <MetaItem label="in" testId="meta-input-tokens">{m.input_tokens}</MetaItem>
          <MetaItem label="out" testId="meta-output-tokens">{m.output_tokens}</MetaItem>
          <MetaItem label="key" testId="meta-key-id">{m.key_id ?? "—"}</MetaItem>
          {m.fallback_attempts > 0 && (
            <MetaItem label="failovers" testId="meta-fallback-attempts" tone="warn">
              {m.fallback_attempts}
            </MetaItem>
          )}
        </div>
      </div>
    </div>
  );
}

export default function App() {
  const [sessionId, setSessionId] = useState(newSessionId); // new session per page load
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [systemPrompt, setSystemPrompt] = useState("");
  const [simulate, setSimulate] = useState("");
  const [sending, setSending] = useState(false);
  const [health, setHealth] = useState(null);
  const [showSettings, setShowSettings] = useState(false);
  const bottomRef = useRef(null);
  const inputRef = useRef(null);

  useEffect(() => {
    getHealth().then(setHealth).catch(() => setHealth({ status: "down" }));
  }, []);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, sending]);

  async function handleSend(e) {
    e?.preventDefault();
    const text = input.trim();
    if (!text || sending) return;
    setInput("");
    setMessages((prev) => [...prev, { id: crypto.randomUUID?.() ?? Date.now(), role: "user", content: text }]);
    setSending(true);
    try {
      const res = await sendChat({ sessionId, message: text, systemPrompt, simulateFailure: simulate });
      setMessages((prev) => [
        ...prev,
        { id: `${Date.now()}-a`, role: "assistant", content: res.reply, metadata: res.metadata },
      ]);
    } catch (err) {
      setMessages((prev) => [
        ...prev,
        { id: `${Date.now()}-e`, role: "error", content: err.message, status: err.status },
      ]);
    } finally {
      setSending(false);
      inputRef.current?.focus();
    }
  }

  function handleKeyDown(e) {
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      handleSend(e);
    }
  }

  function startNewChat() {
    setSessionId(newSessionId());
    setMessages([]);
    inputRef.current?.focus();
  }

  const healthy = health?.status === "ok";

  return (
    <div className="app">
      <header className="header">
        <div className="brand">
          <div className="logo" aria-hidden="true">L</div>
          <div>
            <h1>Lyftr Chat</h1>
            <p className="subtitle">
              session <code>{sessionId}</code>
            </p>
          </div>
        </div>
        <div className="header-actions">
          <span
            className={`status ${health === null ? "status-pending" : healthy ? "status-ok" : "status-down"}`}
            title={API_BASE_URL}
          >
            <span className="dot" />
            {health === null
              ? "checking…"
              : healthy
              ? `${health.keys_configured} keys · cache ${health.cache_enabled ? "on" : "off"}`
              : "backend offline"}
          </span>
          <button type="button" className="btn btn-ghost" onClick={() => setShowSettings((s) => !s)}>
            {showSettings ? "Hide settings" : "Settings"}
          </button>
          <button type="button" className="btn btn-ghost" onClick={startNewChat}>
            New chat
          </button>
        </div>
      </header>

      {showSettings && (
        <section className="settings">
          <label className="field">
            <span>System prompt (this session)</span>
            <textarea
              data-testid="system-prompt-input"
              rows={2}
              value={systemPrompt}
              placeholder="Optional, e.g. “You are a terse assistant that answers in one sentence.”"
              onChange={(e) => setSystemPrompt(e.target.value)}
            />
          </label>
          <label className="field">
            <span>Failover test (X-Simulate-Failure header)</span>
            <select value={simulate} onChange={(e) => setSimulate(e.target.value)} data-testid="simulate-failure-select">
              {FAILURE_PRESETS.map((p) => (
                <option key={p.value} value={p.value}>
                  {p.label}
                </option>
              ))}
            </select>
          </label>
        </section>
      )}

      <main className="chat" aria-live="polite">
        {messages.length === 0 && (
          <div className="empty">
            <h2>Start a conversation</h2>
            <p>
              Every reply shows where it came from (<b>LIVE_INFERENCE</b> or <b>LLM_CACHE</b>), the model,
              latency and the tokens used for that request.
            </p>
          </div>
        )}
        {messages.map((msg) => {
          if (msg.role === "user") {
            return (
              <div key={msg.id} className="row row-user">
                <div className="bubble bubble-user" data-testid="user-message">{msg.content}</div>
              </div>
            );
          }
          if (msg.role === "error") {
            return (
              <div key={msg.id} className="row row-assistant">
                <div className="bubble bubble-error" role="alert" data-testid="error-message">
                  <strong>{msg.status ? `Error ${msg.status}` : "Network error"}:</strong> {msg.content}
                </div>
              </div>
            );
          }
          return <AssistantMessage key={msg.id} msg={msg} />;
        })}
        {sending && (
          <div className="row row-assistant">
            <div className="bubble bubble-assistant typing" aria-label="Assistant is typing">
              <span /> <span /> <span />
            </div>
          </div>
        )}
        <div ref={bottomRef} />
      </main>

      <form className="composer" onSubmit={handleSend}>
        {simulate && <div className="sim-banner">Simulating failure: <code>{simulate}</code></div>}
        <div className="composer-row">
          <textarea
            ref={inputRef}
            data-testid="chat-input"
            value={input}
            rows={1}
            placeholder="Type a message…  (Enter to send, Shift+Enter for a new line)"
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            autoFocus
          />
          <button type="submit" className="btn btn-primary" data-testid="send-button" disabled={sending}>
            {sending ? "Sending…" : "Send"}
          </button>
        </div>
      </form>
    </div>
  );
}
