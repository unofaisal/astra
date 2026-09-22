export type ToolStatus =
  | "running"
  | "success"
  | "error"
  | "pending"
  | "awaiting_clarification"
  | "cancelled";

export type Attachment = {
  file_name?: string;
  file_url?: string;
};

export type ToolCall = {
  callId: string;
  toolName: string;
  args: unknown;
  status: ToolStatus;
  result?: unknown;
  error?: string;
  elapsedMs?: number;
  clarificationId?: string;
};

export type Turn = {
  id: string;
  role: "user" | "assistant";
  content: string;
  attachments?: Attachment[];
  reasoning?: string;
  toolCalls: ToolCall[];
  streaming?: boolean;
  isError?: boolean;
  model?: string;
  cost?: number;
  inputTokens?: number;
  outputTokens?: number;
  timestamp?: number;
};

export type Clarification = {
  clarificationId: string;
  question: string;
  options: string[];
  allowFreeText: boolean;
};

export type SessionSummary = {
  session_id: string;
  title: string;
  last_active: number;
  turn_count?: number;
  message_count?: number;
  total_input_tokens?: number;
  total_output_tokens?: number;
  estimated_cost?: number;
  ended_reason?: string | null;
  messages?: unknown[];
  tool_calls?: unknown[];
};

export type AgentConfig = {
  provider: string;
  model: string;
  reasoning_effort?: string | null;
  base_url_override?: string | null;
  api_key_set?: boolean;
  api_key_suffix?: string | null;
};

export type ChatState = {
  sessions: SessionSummary[];
  activeSessionId: string | null;
  turns: Turn[];
  isStreaming: boolean;
  pendingClarification: Clarification | null;
  error: string | null;
  sessionsLoading: boolean;
};
