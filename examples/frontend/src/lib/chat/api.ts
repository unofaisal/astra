const BASE_URL = (import.meta.env.VITE_ASTRA_API_URL as string | undefined) || "http://localhost:8000";

async function jsonFetch(path: string, options: RequestInit = {}) {
  const res = await fetch(`${BASE_URL}${path}`, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = (body as { detail?: string }).detail || JSON.stringify(body);
    } catch {
      /* ignore */
    }
    throw new Error(`${res.status} ${detail}`);
  }
  if (res.status === 204) return null;
  return res.json();
}

export const api = {
  chat: (payload: unknown) => jsonFetch("/chat", { method: "POST", body: JSON.stringify(payload) }),
  resume: (payload: unknown) =>
    jsonFetch("/chat/resume", { method: "POST", body: JSON.stringify(payload) }),
  regenerate: (sessionId: string, payload: unknown = {}) =>
    jsonFetch(`/sessions/${sessionId}/regenerate`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  stop: (sessionId: string) => jsonFetch(`/sessions/${sessionId}/stop`, { method: "POST" }),
  getSession: (sessionId: string) => jsonFetch(`/sessions/${sessionId}`),
  deleteSession: (sessionId: string) => jsonFetch(`/sessions/${sessionId}`, { method: "DELETE" }),
  listSessions: (params: Record<string, string | number> = {}) => {
    const qs = new URLSearchParams(
      Object.fromEntries(Object.entries(params).map(([k, v]) => [k, String(v)])),
    ).toString();
    return jsonFetch(`/sessions${qs ? `?${qs}` : ""}`);
  },
  getProviders: () => jsonFetch("/providers"),
  getConfig: () => jsonFetch("/config"),
  updateConfig: (payload: unknown) =>
    jsonFetch("/config", { method: "POST", body: JSON.stringify(payload) }),
  health: () => jsonFetch("/health"),
};

export type StreamEvent = { type: string; data: Record<string, unknown> };

export function streamChat(payload: unknown, onEvent: (e: StreamEvent) => void) {
  return streamPost("/chat/stream", payload, onEvent);
}

export function streamRegenerate(
  sessionId: string,
  payload: unknown,
  onEvent: (e: StreamEvent) => void,
) {
  return streamPost(`/sessions/${sessionId}/regenerate/stream`, payload, onEvent);
}

export function streamResume(
  sessionId: string,
  clarificationId: string,
  answer: string,
  onEvent: (e: StreamEvent) => void,
) {
  return streamPost(
    "/chat/resume/stream",
    { session_id: sessionId, clarification_id: clarificationId, answer },
    onEvent,
  );
}

function streamPost(path: string, payload: unknown, onEvent: (e: StreamEvent) => void) {
  const controller = new AbortController();

  (async () => {
    let res: Response;
    try {
      res = await fetch(`${BASE_URL}${path}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
        signal: controller.signal,
      });
    } catch (err) {
      if ((err as Error).name !== "AbortError") {
        onEvent({
          type: "result",
          data: { status: "error", error: `Network error: ${(err as Error).message}` },
        });
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
        let sepIndex: number;
        while ((sepIndex = buffer.indexOf("\n\n")) !== -1) {
          const frame = buffer.slice(0, sepIndex);
          buffer = buffer.slice(sepIndex + 2);
          const event = parseFrame(frame);
          if (event) onEvent(event);
        }
      }
    } catch (err) {
      if ((err as Error).name !== "AbortError") {
        onEvent({
          type: "result",
          data: { status: "error", error: `Stream error: ${(err as Error).message}` },
        });
      }
    }
  })();

  return controller;
}

function parseFrame(frame: string): StreamEvent | null {
  let eventType = "message";
  let dataLine: string | null = null;
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
