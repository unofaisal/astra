import { useState } from "react";
import { ChevronRight, Wrench } from "lucide-react";
import StatusIcon, { statusLabel } from "./StatusIcon";
import { formatElapsed } from "../lib/transform";

function formatPayload(value) {
  if (value == null) return "";
  if (typeof value === "string") {
    try {
      return JSON.stringify(JSON.parse(value), null, 2);
    } catch {
      return value;
    }
  }
  return JSON.stringify(value, null, 2);
}

function ToolCall({ call }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="overflow-hidden rounded-md border border-border bg-surface-2">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-2 px-3 py-1.5 text-left hover:bg-surface-3"
      >
        <ChevronRight size={13} className={`text-text-tertiary transition-transform ${open ? "rotate-90" : ""}`} />
        <Wrench size={13} className="text-text-tertiary" />
        <span className="font-mono text-xs text-text-secondary">{call.toolName}</span>
        <span className="ml-auto flex items-center gap-1.5">
          {call.elapsedMs != null && <span className="text-[11px] text-text-tertiary">{formatElapsed(call.elapsedMs)}</span>}
          <StatusIcon status={call.status} />
        </span>
      </button>
      {open && (
        <div className="space-y-2 border-t border-border px-3 py-2">
          <div>
            <div className="mb-1 text-[10px] font-medium uppercase tracking-wide text-text-tertiary">Arguments</div>
            <pre className="overflow-x-auto rounded bg-bg p-2 font-mono text-[11px] text-text-secondary">
              {formatPayload(call.args) || "{}"}
            </pre>
          </div>
          {(call.result != null || call.error != null) && (
            <div>
              <div className="mb-1 text-[10px] font-medium uppercase tracking-wide text-text-tertiary">
                {call.status === "error" ? "Error" : "Result"}
              </div>
              <pre
                className={`overflow-x-auto rounded p-2 font-mono text-[11px] ${
                  call.status === "error" ? "bg-error/10 text-error" : "bg-bg text-text-secondary"
                }`}
              >
                {formatPayload(call.error ?? call.result)}
              </pre>
            </div>
          )}
          {call.status === "running" && <div className="text-[11px] text-text-tertiary">{statusLabel(call.status)}…</div>}
        </div>
      )}
    </div>
  );
}

export default function ToolCallTrace({ toolCalls }) {
  if (!toolCalls || toolCalls.length === 0) return null;
  return (
    <div className="my-2 space-y-1.5">
      {toolCalls.map((call) => (
        <ToolCall key={call.callId} call={call} />
      ))}
    </div>
  );
}
