import { createContext, useCallback, useContext, useEffect, useReducer, useRef } from "react";
import { api, streamChat, streamRegenerate, streamResume, type StreamEvent } from "./api";
import { sessionToTurns } from "./transform";
import type { ChatState, Clarification, SessionSummary, Turn } from "./types";

const initialState: ChatState = {
  sessions: [],
  activeSessionId: null,
  turns: [],
  isStreaming: false,
  pendingClarification: null,
  error: null,
  sessionsLoading: true,
};

type Action =
  | { type: "SESSIONS_LOADED"; sessions: SessionSummary[] }
  | {
      type: "SESSION_SELECTED";
      sessionId: string;
      turns: Turn[];
      pendingClarification: Clarification | null;
    }
  | { type: "NEW_SESSION" }
  | { type: "USER_TURN_ADDED"; turn: Turn }
  | { type: "ASSISTANT_TURN_STARTED"; turn: Turn }
  | { type: "TOKEN_APPENDED"; delta: string }
  | { type: "REASONING_APPENDED"; delta: string }
  | { type: "TOOL_START"; callId: string; toolName: string; args: unknown }
  | {
      type: "TOOL_RESULT";
      callId: string;
      isError: boolean;
      result: unknown;
      error?: string;
      elapsedMs?: number;
    }
  | {
      type: "CLARIFICATION_REQUESTED";
      clarificationId: string;
      question: string;
      options: string[];
      allowFreeText: boolean;
    }
  | { type: "CLARIFICATION_ANSWERED"; answer: string }
  | { type: "STREAM_FINISHED"; sessionId?: string; status?: string; error?: string }
  | { type: "REGENERATE_START" }
  | { type: "STOP_REQUESTED" }
  | { type: "ERROR"; error: string }
  | { type: "SESSION_DELETED"; sessionId: string };

function mapLastAssistant(turns: Turn[], fn: (t: Turn) => Turn) {
  const idx = [...turns].reverse().findIndex((t) => t.role === "assistant");
  if (idx === -1) return turns;
  const realIdx = turns.length - 1 - idx;
  return turns.map((t, i) => (i === realIdx ? fn(t) : t));
}

// Unlike mapLastAssistant, this scans EVERY assistant turn — needed for
// resolving a paused clarification's tool call, since by the time it's
// answered a NEW (blank, streaming) assistant turn has already been
// appended for the follow-up reply, so the paused tool call is no
// longer in the "last" assistant turn.
function updateToolCallByStatus(
  turns: Turn[],
  status: string,
  updateFn: (tc: Turn["toolCalls"][number]) => Turn["toolCalls"][number],
) {
  return turns.map((t) => {
    if (t.role !== "assistant") return t;
    let matched = false;
    const toolCalls = t.toolCalls.map((tc) => {
      if (!matched && tc.status === status) {
        matched = true;
        return updateFn(tc);
      }
      return tc;
    });
    return matched ? { ...t, toolCalls } : t;
  });
}

function dropLastAssistantTurn(turns: Turn[]) {
  // Mirrors the backend's Conversation.truncate_last_assistant_turn():
  // a single response can span multiple assistant rows (multi-round
  // tool-calling), so drop the WHOLE trailing run of assistant turns,
  // not just the final one — otherwise earlier rounds of the response
  // being regenerated get left behind, stitched onto the new one.
  let cut = turns.length;
  for (let i = turns.length - 1; i >= 0; i--) {
    if (turns[i].role === "assistant") cut = i;
    else break;
  }
  return turns.slice(0, cut);
}

function blankAssistantTurn(): Turn {
  return {
    id: `pending-${Date.now()}`,
    role: "assistant",
    content: "",
    reasoning: "",
    toolCalls: [],
    streaming: true,
  };
}

function reducer(state: ChatState, action: Action): ChatState {
  switch (action.type) {
    case "SESSIONS_LOADED":
      return { ...state, sessions: action.sessions, sessionsLoading: false };
    case "SESSION_SELECTED":
      return {
        ...state,
        activeSessionId: action.sessionId,
        turns: action.turns,
        pendingClarification: action.pendingClarification,
        error: null,
      };
    case "NEW_SESSION":
      return {
        ...state,
        activeSessionId: null,
        turns: [],
        pendingClarification: null,
        error: null,
      };
    case "USER_TURN_ADDED":
      return { ...state, turns: [...state.turns, action.turn], error: null };
    case "ASSISTANT_TURN_STARTED":
      return { ...state, turns: [...state.turns, action.turn], isStreaming: true };
    case "TOKEN_APPENDED":
      return {
        ...state,
        turns: mapLastAssistant(state.turns, (t) => ({ ...t, content: t.content + action.delta })),
      };
    case "REASONING_APPENDED":
      return {
        ...state,
        turns: mapLastAssistant(state.turns, (t) => ({
          ...t,
          reasoning: (t.reasoning || "") + action.delta,
        })),
      };
    case "TOOL_START":
      return {
        ...state,
        turns: mapLastAssistant(state.turns, (t) => ({
          ...t,
          toolCalls: [
            ...t.toolCalls,
            { callId: action.callId, toolName: action.toolName, args: action.args, status: "running" },
          ],
        })),
      };
    case "TOOL_RESULT":
      return {
        ...state,
        turns: mapLastAssistant(state.turns, (t) => ({
          ...t,
          toolCalls: t.toolCalls.map((tc) =>
            tc.callId === action.callId
              ? {
                  ...tc,
                  status: action.isError ? "error" : "success",
                  result: action.result,
                  error: action.error,
                  elapsedMs: action.elapsedMs,
                }
              : tc,
          ),
        })),
      };
    case "CLARIFICATION_REQUESTED":
      return {
        ...state,
        pendingClarification: {
          clarificationId: action.clarificationId,
          question: action.question,
          options: action.options || [],
          allowFreeText: action.allowFreeText,
        },
        // The request_clarification tool call itself never gets a
        // tool_result event (the backend pauses the run instead of
        // completing it) — without this, that tool call's status would
        // stay "running" forever, showing a spinner that never resolves
        // even after the clarification is answered and the conversation
        // moves on.
        turns: mapLastAssistant(state.turns, (t) => ({
          ...t,
          toolCalls: t.toolCalls.map((tc) =>
            tc.status === "running" ? { ...tc, status: "awaiting_clarification" } : tc,
          ),
        })),
      };
    case "CLARIFICATION_ANSWERED":
      return {
        ...state,
        pendingClarification: null,
        error: null,
        turns: updateToolCallByStatus(state.turns, "awaiting_clarification", (tc) => ({
          ...tc,
          status: "success",
          result: `User answered: ${action.answer}`,
        })),
      };
    case "STREAM_FINISHED": {
      const turns = mapLastAssistant(state.turns, (t) => ({ ...t, streaming: false }));
      return {
        ...state,
        turns,
        isStreaming: false,
        activeSessionId: action.sessionId || state.activeSessionId,
        error: action.status === "error" ? action.error || "Stream failed" : null,
        pendingClarification:
          action.status === "clarification_pending" ? state.pendingClarification : null,
      };
    }
    case "REGENERATE_START":
      return { ...state, turns: dropLastAssistantTurn(state.turns), isStreaming: true, error: null };
    case "STOP_REQUESTED":
      return { ...state, isStreaming: false };
    case "ERROR":
      return { ...state, error: action.error, isStreaming: false };
    case "SESSION_DELETED":
      return {
        ...state,
        sessions: state.sessions.filter((s) => s.session_id !== action.sessionId),
        ...(state.activeSessionId === action.sessionId
          ? { activeSessionId: null, turns: [], pendingClarification: null }
          : {}),
      };
    default:
      return state;
  }
}

export function useChat(defaultUser = "local-user") {
  const [state, dispatch] = useReducer(reducer, initialState);
  const abortRef = useRef<AbortController | null>(null);
  const stateRef = useRef(state);
  stateRef.current = state;

  const refreshSessions = useCallback(async () => {
    try {
      const sessions = (await api.listSessions({ user: defaultUser, limit: 100 })) as SessionSummary[];
      dispatch({ type: "SESSIONS_LOADED", sessions });
    } catch {
      dispatch({ type: "SESSIONS_LOADED", sessions: [] });
    }
  }, [defaultUser]);

  useEffect(() => {
    refreshSessions();
  }, [refreshSessions]);

  const selectSession = useCallback(async (sessionId: string) => {
    const session = (await api.getSession(sessionId)) as SessionSummary;
    const turns = sessionToTurns(session);
    const lastAssistant = [...turns].reverse().find((t) => t.role === "assistant");
    const pausedToolCall = lastAssistant?.toolCalls.find((tc) => tc.status === "awaiting_clarification");
    dispatch({
      type: "SESSION_SELECTED",
      sessionId,
      turns,
      pendingClarification: pausedToolCall
        ? {
            clarificationId: pausedToolCall.clarificationId || "",
            question: (pausedToolCall.args as { question?: string })?.question || "",
            options: (pausedToolCall.args as { options?: string[] })?.options || [],
            allowFreeText: (pausedToolCall.args as { allow_free_text?: boolean })?.allow_free_text ?? true,
          }
        : null,
    });
  }, []);

  const newSession = useCallback(() => dispatch({ type: "NEW_SESSION" }), []);

  const handleStreamEvent = useCallback((event: StreamEvent) => {
    const { type, data } = event;
    switch (type) {
      case "token":
        dispatch({ type: "TOKEN_APPENDED", delta: String(data.delta ?? "") });
        break;
      case "reasoning":
        dispatch({ type: "REASONING_APPENDED", delta: String(data.delta ?? "") });
        break;
      case "tool_start":
        dispatch({
          type: "TOOL_START",
          callId: String(data.call_id),
          toolName: String(data.tool_name),
          args: data.args,
        });
        break;
      case "tool_result":
        dispatch({
          type: "TOOL_RESULT",
          callId: String(data.call_id),
          isError: Boolean(data.is_error),
          result: data.result,
          error: data.error ? String(data.error) : undefined,
          elapsedMs: typeof data.elapsed_ms === "number" ? data.elapsed_ms : undefined,
        });
        break;
      case "clarification_request":
        dispatch({
          type: "CLARIFICATION_REQUESTED",
          clarificationId: String(data.clarification_id),
          question: String(data.question ?? ""),
          options: (data.options as string[]) || [],
          allowFreeText: Boolean(data.allow_free_text ?? true),
        });
        break;
      case "result":
        dispatch({
          type: "STREAM_FINISHED",
          sessionId: data.session_id ? String(data.session_id) : undefined,
          status: data.status ? String(data.status) : undefined,
          error: data.error ? String(data.error) : undefined,
        });
        break;
      default:
        break;
    }
  }, []);

  const sendMessage = useCallback(
    (text: string, { model }: { model?: string | null } = {}) => {
      dispatch({
        type: "USER_TURN_ADDED",
        turn: {
          id: `u-${Date.now()}`,
          role: "user",
          content: text,
          toolCalls: [],
          timestamp: Date.now() / 1000,
        },
      });
      dispatch({ type: "ASSISTANT_TURN_STARTED", turn: blankAssistantTurn() });

      abortRef.current = streamChat(
        { message: text, session_id: stateRef.current.activeSessionId, model },
        (event) => {
          handleStreamEvent(event);
          if (event.type === "result") refreshSessions();
        },
      );
    },
    [handleStreamEvent, refreshSessions],
  );

  const answerClarification = useCallback(
    (answer: string) => {
      const clarificationId = stateRef.current.pendingClarification?.clarificationId;
      if (!clarificationId || !stateRef.current.activeSessionId) return;
      dispatch({ type: "ASSISTANT_TURN_STARTED", turn: blankAssistantTurn() });
      dispatch({ type: "CLARIFICATION_ANSWERED", answer });

      abortRef.current = streamResume(
        stateRef.current.activeSessionId,
        clarificationId,
        answer,
        (event) => {
          handleStreamEvent(event);
          if (event.type === "result") refreshSessions();
        },
      );
    },
    [handleStreamEvent, refreshSessions],
  );

  const regenerate = useCallback(
    (model?: string | null) => {
      if (!stateRef.current.activeSessionId) return;
      dispatch({ type: "REGENERATE_START" });
      dispatch({ type: "ASSISTANT_TURN_STARTED", turn: blankAssistantTurn() });

      abortRef.current = streamRegenerate(
        stateRef.current.activeSessionId,
        { model },
        (event) => {
          handleStreamEvent(event);
          if (event.type === "result") refreshSessions();
        },
      );
    },
    [handleStreamEvent, refreshSessions],
  );

  const stop = useCallback(() => {
    if (stateRef.current.activeSessionId) {
      api.stop(stateRef.current.activeSessionId).catch(() => {});
    }
    abortRef.current?.abort();
    dispatch({ type: "STOP_REQUESTED" });
  }, []);

  const deleteSession = useCallback(async (sessionId: string) => {
    await api.deleteSession(sessionId);
    dispatch({ type: "SESSION_DELETED", sessionId });
  }, []);

  return {
    ...state,
    selectSession,
    newSession,
    sendMessage,
    answerClarification,
    regenerate,
    stop,
    deleteSession,
    refreshSessions,
  };
}

export type ChatApi = ReturnType<typeof useChat>;

export const ChatContext = createContext<ChatApi | null>(null);

export function useChatContext() {
  const ctx = useContext(ChatContext);
  if (!ctx) throw new Error("useChatContext must be used inside ChatProvider");
  return ctx;
}
