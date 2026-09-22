// API client for the astra REST API (examples/api/main.py).
//
// EventSource (the browser's native SSE client) only supports GET
// requests with no body, which doesn't fit "send this message and
// stream the reply" — so streaming here is done by hand: fetch() with
// a ReadableStream body reader, parsing the same "event: X\ndata:
// Y\n\n" wire format the backend emits.

const BASE_URL = import.meta.env.VITE_ASTRA_API_URL || "http://localhost:8000";

async function jsonFetch(path, options = {}) {
  const res = await fetch(`${BASE_URL}${path}`, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail || JSON.stringify(body);
    } catch {
      /* ignore */
    }
    throw new Error(`${res.status} ${detail}`);
  }
  if (res.status === 204) return null;
  return res.json();
}

export const api = {
  chat: (payload) => jsonFetch("/chat", { method: "POST", body: JSON.stringify(payload) }),
  resume: (payload) => jsonFetch("/chat/resume", { method: "POST", body: JSON.stringify(payload) }),
  regenerate: (sessionId, payload = {}) =>
    jsonFetch(`/sessions/${sessionId}/regenerate`, { method: "POST", body: JSON.stringify(payload) }),
  stop: (sessionId) => jsonFetch(`/sessions/${sessionId}/stop`, { method: "POST" }),
  getSession: (sessionId) => jsonFetch(`/sessions/${sessionId}`),
  deleteSession: (sessionId) => jsonFetch(`/sessions/${sessionId}`, { method: "DELETE" }),
  listSessions: (params = {}) => {
    const qs = new URLSearchParams(params).toString();
    return jsonFetch(`/sessions${qs ? `?${qs}` : ""}`);
  },
  getProviders: () => jsonFetch("/providers"),
  getConfig: () => jsonFetch("/config"),
  updateConfig: (payload) => jsonFetch("/config", { method: "POST", body: JSON.stringify(payload) }),
  health: () => jsonFetch("/health"),
};

/**
 * Streams a chat or regenerate call, invoking `onEvent({type, data})` for
 * every SSE event as it arrives. Returns an AbortController the caller
 * can use to cancel the underlying fetch (e.g. when the user hits Stop) —
 * separate from, and in addition to, calling api.stop(sessionId) for the
 * server-side cooperative stop.
 */
export function streamChat(payload, onEvent) {
  return streamPost("/chat/stream", payload, onEvent);
}

export function streamRegenerate(sessionId, payload, onEvent) {
  return streamPost(`/sessions/${sessionId}/regenerate/stream`, payload, onEvent);
}

export function streamResume(sessionId, clarificationId, answer, onEvent) {
  return streamPost("/chat/resume/stream", { session_id: sessionId, clarification_id: clarificationId, answer }, onEvent);
}

function streamPost(path, payload, onEvent) {
  const controller = new AbortController();

  (async () => {
    let res;
    try {
      res = await fetch(`${BASE_URL}${path}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
        signal: controller.signal,
      });
    } catch (err) {
      if (err.name !== "AbortError") {
        onEvent({ type: "result", data: { status: "error", error: `Network error: ${err.message}` } });
      }
      return;
    }

    if (!res.ok || !res.body) {
      onEvent({ type: "result", data: { status: "error", error: `HTTP ${res.status}` } });
      return;
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        // SSE frames are separated by a blank line ("\n\n")
        let sepIndex;
        while ((sepIndex = buffer.indexOf("\n\n")) !== -1) {
          const frame = buffer.slice(0, sepIndex);
          buffer = buffer.slice(sepIndex + 2);
          const event = parseFrame(frame);
          if (event) onEvent(event);
        }
      }
    } catch (err) {
      if (err.name !== "AbortError") {
        onEvent({ type: "result", data: { status: "error", error: `Stream error: ${err.message}` } });
      }
    }
  })();

  return controller;
}

function parseFrame(frame) {
  let eventType = "message";
  let dataLine = null;
  for (const line of frame.split("\n")) {
    if (line.startsWith("event: ")) eventType = line.slice(7);
    else if (line.startsWith("data: ")) dataLine = line.slice(6);
  }
  if (dataLine === null) return null;
  try {
    return { type: eventType, data: JSON.parse(dataLine) };
  } catch {
    return null;
  }
}
