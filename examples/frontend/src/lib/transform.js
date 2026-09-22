// Converts the astra REST API's raw Session shape (a flat list of
// message rows + a flat list of tool_call rows, linked by
// parent_message) into an array of "turns" the UI renders — the same
// shape a live SSE stream builds up incrementally, so <Message> only
// needs to know one format regardless of whether it's history or live.

function safeParseArgs(raw) {
  try {
    return JSON.parse(raw);
  } catch {
    return raw;
  }
}

export function sessionToTurns(session) {
  if (!session) return [];
  const toolsByParent = {};
  for (const tc of session.tool_calls || []) {
    (toolsByParent[tc.parent_message] ||= []).push(tc);
  }

  const turns = [];
  let pendingReasoning = [];

  for (const m of session.messages || []) {
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
        timestamp: m.timestamp,
      });
    } else if (m.role === "assistant") {
      const toolCalls = (toolsByParent[m.message_id] || []).map((tc) => ({
        callId: tc.call_id,
        toolName: tc.tool_name,
        args: safeParseArgs(tc.arguments),
        status: tc.status,
        result: tc.result,
        error: tc.error,
        elapsedMs: tc.elapsed_ms,
        clarificationId: tc.clarification_id,
      }));
      turns.push({
        id: m.message_id,
        role: "assistant",
        content: m.content || "",
        reasoning: pendingReasoning.join("\n\n"),
        toolCalls,
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
      // "system" rows (skill injections, etc.) aren't shown inline in
      // the transcript — low value to surface there; visible in full
      // via the Run Inspector's raw session view instead.
      pendingReasoning = [];
    }
  }

  return turns;
}

const DAY = 86400;

export function groupSessionsByRecency(sessions) {
  const startOfToday = new Date();
  startOfToday.setHours(0, 0, 0, 0);
  const todayTs = startOfToday.getTime() / 1000;
  const yesterdayTs = todayTs - DAY;
  const weekAgoTs = todayTs - 7 * DAY;

  const groups = { Today: [], Yesterday: [], "Previous 7 Days": [], Earlier: [] };
  for (const s of sessions) {
    const t = s.last_active || 0;
    if (t >= todayTs) groups.Today.push(s);
    else if (t >= yesterdayTs) groups.Yesterday.push(s);
    else if (t >= weekAgoTs) groups["Previous 7 Days"].push(s);
    else groups.Earlier.push(s);
  }
  return Object.entries(groups).filter(([, list]) => list.length > 0);
}

export function formatElapsed(ms) {
  if (ms == null) return null;
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}
