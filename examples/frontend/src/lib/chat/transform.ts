import type { SessionSummary, Turn } from "./types";

function safeParseArgs(raw: unknown) {
  if (typeof raw !== "string") return raw;
  try {
    return JSON.parse(raw);
  } catch {
    return raw;
  }
}

type RawMessage = {
  message_id: string;
  role: string;
  content?: string;
  attachments?: Turn["attachments"];
  timestamp?: number;
  model?: string;
  is_error?: boolean;
  estimated_cost?: number;
  input_tokens?: number;
  output_tokens?: number;
};

type RawToolCall = {
  call_id: string;
  tool_name: string;
  arguments?: unknown;
  status: Turn["toolCalls"][number]["status"];
  result?: unknown;
  error?: string;
  elapsed_ms?: number;
  clarification_id?: string;
  parent_message?: string;
};

export function sessionToTurns(session: SessionSummary | null): Turn[] {
  if (!session) return [];
  const messages = (session.messages || []) as RawMessage[];
  const toolCalls = (session.tool_calls || []) as RawToolCall[];
  const toolsByParent: Record<string, RawToolCall[]> = {};
  for (const tc of toolCalls) {
    const key = tc.parent_message || "";
    (toolsByParent[key] ||= []).push(tc);
  }

  const turns: Turn[] = [];
  let pendingReasoning: string[] = [];

  for (const m of messages) {
    if (m.role === "reasoning") {
      if (m.content) pendingReasoning.push(m.content);
      continue;
    }
    if (m.role === "user") {
      pendingReasoning = [];
      turns.push({
        id: m.message_id,
        role: "user",
        content: m.content || "",
        attachments: m.attachments || [],
        toolCalls: [],
        timestamp: m.timestamp,
      });
    } else if (m.role === "assistant") {
      turns.push({
        id: m.message_id,
        role: "assistant",
        content: m.content || "",
        reasoning: pendingReasoning.join("\n\n"),
        toolCalls: (toolsByParent[m.message_id] || []).map((tc) => ({
          callId: tc.call_id,
          toolName: tc.tool_name,
          args: safeParseArgs(tc.arguments),
          status: tc.status,
          result: tc.result,
          error: tc.error,
          elapsedMs: tc.elapsed_ms,
          clarificationId: tc.clarification_id,
        })),
        streaming: false,
        model: m.model,
        isError: m.is_error,
        cost: m.estimated_cost,
        inputTokens: m.input_tokens,
        outputTokens: m.output_tokens,
        timestamp: m.timestamp,
      });
      pendingReasoning = [];
    } else {
      pendingReasoning = [];
    }
  }

  return turns;
}

const DAY = 86400;

export function groupSessionsByRecency(sessions: SessionSummary[]) {
  const startOfToday = new Date();
  startOfToday.setHours(0, 0, 0, 0);
  const todayTs = startOfToday.getTime() / 1000;
  const yesterdayTs = todayTs - DAY;
  const weekAgoTs = todayTs - 7 * DAY;

  const groups: Record<string, SessionSummary[]> = {
    Today: [],
    Yesterday: [],
    "Previous 7 days": [],
    Earlier: [],
  };
  for (const s of sessions) {
    const t = s.last_active || 0;
    if (t >= todayTs) groups.Today.push(s);
    else if (t >= yesterdayTs) groups.Yesterday.push(s);
    else if (t >= weekAgoTs) groups["Previous 7 days"].push(s);
    else groups.Earlier.push(s);
  }
  return Object.entries(groups).filter(([, list]) => list.length > 0);
}

export function formatElapsed(ms?: number | null) {
  if (ms == null) return null;
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

export function formatTokens(n?: number | null) {
  if (n == null) return "—";
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
}

/**
 * Merges consecutive assistant-role turns (no user turn between them)
 * into one display group — one avatar, one continuous activity
 * timeline, content concatenated in order.
 *
 * Why this is needed: a single logical agent response can legitimately
 * span MULTIPLE assistant MessageRows on the backend — every internal
 * LLM call within one Agent.run() loop gets its own row (see
 * Agent._run_one_turn calling add_assistant_message each iteration),
 * and a clarification pause+resume is explicitly two separate calls
 * (two separate rows) with no user-role message in between (the
 * answer is stored as a tool result, not a chat message). Without this
 * grouping, sessionToTurns() (and the live streaming reducer, for the
 * clarification-resume case specifically) produce multiple adjacent
 * assistant turns that each render as their own message block — extra
 * avatars, a timeline chopped into disconnected pieces. Applying this
 * at render time, uniformly for both reloaded history and live
 * streaming, keeps behavior consistent between the two and fixes both
 * cases with one function.
 */
export function groupTurnsForDisplay(turns: Turn[]): Turn[] {
  const groups: Turn[] = [];
  for (const t of turns) {
    const prev = groups[groups.length - 1];
    if (t.role === "assistant" && prev && prev.role === "assistant") {
      prev.content = prev.content ? (t.content ? `${prev.content}\n\n${t.content}` : prev.content) : t.content;
      prev.reasoning = [prev.reasoning, t.reasoning].filter(Boolean).join("\n\n") || undefined;
      prev.toolCalls = [...prev.toolCalls, ...t.toolCalls];
      prev.streaming = t.streaming;
      prev.isError = prev.isError || t.isError;
      prev.model = t.model || prev.model;
      prev.timestamp = t.timestamp ?? prev.timestamp;
      prev.inputTokens = (prev.inputTokens || 0) + (t.inputTokens || 0);
      prev.outputTokens = (prev.outputTokens || 0) + (t.outputTokens || 0);
      prev.cost = (prev.cost || 0) + (t.cost || 0);
      continue;
    }
    // Shallow-copy so later merges don't mutate the original turns
    // array (React state / the reloaded session data).
    groups.push({ ...t, toolCalls: [...t.toolCalls] });
  }
  return groups;
}

export function relativeTime(ts?: number) {
  if (!ts) return "";
  const delta = Date.now() / 1000 - ts;
  if (delta < 60) return "just now";
  if (delta < 3600) return `${Math.floor(delta / 60)}m`;
  if (delta < 86400) return `${Math.floor(delta / 3600)}h`;
  if (delta < 604800) return `${Math.floor(delta / 86400)}d`;
  return new Date(ts * 1000).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
  });
}
