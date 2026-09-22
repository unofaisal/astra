import { useCallback, useEffect, useReducer, useRef } from "react";
import { api, streamChat, streamRegenerate, streamResume } from "./api";
import { sessionToTurns } from "./transform";

const initialState = {
  sessions: [],
  activeSessionId: null,
  turns: [],
  isStreaming: false,
  pendingClarification: null, // { clarificationId, question, options, allowFreeText }
  error: null,
  sessionsLoading: true,
};

function reducer(state, action) {
  switch (action.type) {
    case "SESSIONS_LOADED":
      return { ...state, sessions: action.sessions, sessionsLoading: false };
    case "SESSION_SELECTED":
      return {
        ...state,
        activeSessionId: action.sessionId,
        turns: action.turns,
        pendingClarification: action.pendingClarification || null,
        error: null,
      };
    case "NEW_SESSION":
      return { ...state, activeSessionId: null, turns: [], pendingClarification: null, error: null };
    case "USER_TURN_ADDED":
      return { ...state, turns: [...state.turns, action.turn], error: null };
    case "ASSISTANT_TURN_STARTED":
      return { ...state, turns: [...state.turns, action.turn], isStreaming: true };
    case "TOKEN_APPENDED":
      return { ...state, turns: mapLastAssistant(state.turns, (t) => ({ ...t, content: t.content + action.delta })) };
    case "REASONING_APPENDED":
      return {
        ...state,
        turns: mapLastAssistant(state.turns, (t) => ({ ...t, reasoning: (t.reasoning || "") + action.delta })),
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
              : tc
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
      };
    case "CLARIFICATION_ANSWERED":
      return { ...state, pendingClarification: null, error: null };
    case "STREAM_FINISHED": {
      const turns = mapLastAssistant(state.turns, (t) => ({ ...t, streaming: false }));
      return {
        ...state,
        turns,
        isStreaming: false,
        activeSessionId: action.sessionId || state.activeSessionId,
        error: action.status === "error" ? action.error : null,
        pendingClarification: action.status === "clarification_pending" ? state.pendingClarification : null,
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

function mapLastAssistant(turns, fn) {
  const idx = [...turns].reverse().findIndex((t) => t.role === "assistant");
  if (idx === -1) return turns;
  const realIdx = turns.length - 1 - idx;
  return turns.map((t, i) => (i === realIdx ? fn(t) : t));
}

function dropLastAssistantTurn(turns) {
  const idx = [...turns].reverse().findIndex((t) => t.role === "assistant");
  if (idx === -1) return turns;
  const realIdx = turns.length - 1 - idx;
  return turns.slice(0, realIdx);
}

function blankAssistantTurn() {
  return {
    id: `pending-${Date.now()}`,
    role: "assistant",
    content: "",
    reasoning: "",
    toolCalls: [],
    streaming: true,
  };
}

export function useChat(defaultUser = "local-user") {
  const [state, dispatch] = useReducer(reducer, initialState);
  const abortRef = useRef(null);

  const refreshSessions = useCallback(async () => {
    try {
      const sessions = await api.listSessions({ user: defaultUser, limit: 100 });
      dispatch({ type: "SESSIONS_LOADED", sessions });
    } catch {
      dispatch({ type: "SESSIONS_LOADED", sessions: [] });
    }
  }, [defaultUser]);

  useEffect(() => {
    refreshSessions();
  }, [refreshSessions]);

  const selectSession = useCallback(async (sessionId) => {
    const session = await api.getSession(sessionId);
    const turns = sessionToTurns(session);
    const lastAssistant = [...turns].reverse().find((t) => t.role === "assistant");
    const pausedToolCall = lastAssistant?.toolCalls.find((tc) => tc.status === "awaiting_clarification");
    dispatch({
      type: "SESSION_SELECTED",
      sessionId,
      turns,
      pendingClarification: pausedToolCall
        ? {
            clarificationId: pausedToolCall.clarificationId,
            question: pausedToolCall.args?.question,
            options: pausedToolCall.args?.options || [],
            allowFreeText: pausedToolCall.args?.allow_free_text ?? true,
          }
        : null,
    });
  }, []);

  const newSession = useCallback(() => dispatch({ type: "NEW_SESSION" }), []);

  const handleStreamEvent = useCallback(({ type, data }) => {
    switch (type) {
      case "token":
        dispatch({ type: "TOKEN_APPENDED", delta: data.delta });
        break;
      case "reasoning":
        dispatch({ type: "REASONING_APPENDED", delta: data.delta });
        break;
      case "tool_start":
        dispatch({ type: "TOOL_START", callId: data.call_id, toolName: data.tool_name, args: data.args });
        break;
      case "tool_result":
        dispatch({
          type: "TOOL_RESULT",
          callId: data.call_id,
          isError: data.is_error,
          result: data.result,
          error: data.error,
          elapsedMs: data.elapsed_ms,
        });
        break;
      case "clarification_request":
        dispatch({
          type: "CLARIFICATION_REQUESTED",
          clarificationId: data.clarification_id,
          question: data.question,
          options: data.options,
          allowFreeText: data.allow_free_text,
        });
        break;
      case "result":
        dispatch({
          type: "STREAM_FINISHED",
          sessionId: data.session_id,
          status: data.status,
          error: data.error,
        });
        break;
      default:
        break;
    }
  }, []);

  const sendMessage = useCallback(
    (text, { model, attachments } = {}) => {
      dispatch({ type: "USER_TURN_ADDED", turn: { id: `u-${Date.now()}`, role: "user", content: text, attachments } });
      dispatch({ type: "ASSISTANT_TURN_STARTED", turn: blankAssistantTurn() });
      abortRef.current = streamChat(
        { message: text, session_id: state.activeSessionId, model, attachments },
        (event) => {
          handleStreamEvent(event);
          if (event.type === "result") refreshSessions();
        }
      );
    },
    [state.activeSessionId, handleStreamEvent, refreshSessions]
  );

  const answerClarification = useCallback(
    (answer) => {
      const clarificationId = state.pendingClarification?.clarificationId;
      if (!clarificationId || !state.activeSessionId) return;
      dispatch({ type: "ASSISTANT_TURN_STARTED", turn: blankAssistantTurn() });
      dispatch({ type: "CLARIFICATION_ANSWERED" }); // clear the card immediately
      abortRef.current = streamResume(state.activeSessionId, clarificationId, answer, (event) => {
        handleStreamEvent(event);
        if (event.type === "result") refreshSessions();
      });
    },
    [state.activeSessionId, state.pendingClarification, handleStreamEvent, refreshSessions]
  );

  const regenerate = useCallback(
    (model) => {
      if (!state.activeSessionId) return;
      dispatch({ type: "REGENERATE_START" });
      dispatch({ type: "ASSISTANT_TURN_STARTED", turn: blankAssistantTurn() });
      abortRef.current = streamRegenerate(state.activeSessionId, { model }, (event) => {
        handleStreamEvent(event);
        if (event.type === "result") refreshSessions();
      });
    },
    [state.activeSessionId, handleStreamEvent, refreshSessions]
  );

  const stop = useCallback(() => {
    if (state.activeSessionId) api.stop(state.activeSessionId).catch(() => {});
    abortRef.current?.abort();
    dispatch({ type: "STOP_REQUESTED" });
  }, [state.activeSessionId]);

  const deleteSession = useCallback(async (sessionId) => {
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
