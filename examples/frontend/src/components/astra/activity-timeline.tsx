import { useEffect, useState } from "react";
import { Brain, ChevronRight } from "lucide-react";
import { formatElapsed } from "@/lib/chat/transform";
import type { ToolCall } from "@/lib/chat/types";
import { StatusBadge, StatusIcon } from "./status-badge";

function formatPayload(value: unknown) {
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

function Payload({ label, value, danger }: { label: string; value: unknown; danger?: boolean }) {
  return (
    <div>
      <div className="mb-1 font-mono text-2xs uppercase tracking-wider text-subtle">{label}</div>
      <pre
        className={`overflow-x-auto rounded-sm border p-2 font-mono text-2xs leading-relaxed ${
          danger ? "border-danger/30 bg-danger/5 text-danger" : "border-border bg-bg text-muted"
        }`}
      >
        {formatPayload(value) || "{}"}
      </pre>
    </div>
  );
}

function ToolStep({ call, isLast }: { call: ToolCall; isLast: boolean }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="relative pb-3 pl-6 last:pb-0">
      {!isLast && <span className="absolute top-4 left-[7px] h-full w-px bg-border" />}
      <span className="absolute top-0.5 left-0 flex size-3.5 items-center justify-center">
        <StatusIcon status={call.status} size={12} />
      </span>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-2 py-0.5 text-left"
      >
        <ChevronRight
          size={12}
          className={`text-subtle transition-transform duration-(--motion-quick) ${open ? "rotate-90" : ""}`}
        />
        <span className="rounded-xs bg-hover px-1.5 py-0.5 font-mono text-2xs text-muted">
          {call.toolName}
        </span>
        <span className="ml-auto flex items-center gap-2">
          {call.elapsedMs != null && (
            <span className="font-mono text-2xs text-subtle tabular-nums">
              {formatElapsed(call.elapsedMs)}
            </span>
          )}
          <StatusBadge status={call.status} />
        </span>
      </button>
      {open && (
        <div className="space-y-2 py-2 pl-5">
          <Payload label="Arguments" value={call.args} />
          {(call.result != null || call.error != null) && (
            <Payload
              label={call.status === "error" ? "Error" : "Result"}
              value={call.error ?? call.result}
              danger={call.status === "error"}
            />
          )}
        </div>
      )}
    </div>
  );
}

export function ActivityTimeline({
  reasoning,
  toolCalls,
  streaming,
}: {
  reasoning?: string;
  toolCalls?: ToolCall[];
  streaming?: boolean;
}) {
  const hasReasoning = Boolean(reasoning);
  const hasTools = Boolean(toolCalls && toolCalls.length > 0);
  const runningCount = (toolCalls || []).filter((t) => t.status === "running").length;
  const isActive = Boolean(streaming && (runningCount > 0 || (hasReasoning && !hasTools)));
  const [open, setOpen] = useState(() => Boolean(streaming));

  useEffect(() => {
    if (isActive) setOpen(true);
  }, [isActive]);

  if (!hasReasoning && !hasTools) return null;

  const summary = isActive
    ? runningCount > 0
      ? `Using ${runningCount} tool${runningCount > 1 ? "s" : ""}`
      : "Thinking"
    : [hasReasoning ? "Thought" : null, hasTools ? `${toolCalls!.length} tool${toolCalls!.length > 1 ? "s" : ""}` : null]
        .filter(Boolean)
        .join(" · ");

  return (
    <div className="my-3 overflow-hidden rounded-md border border-border bg-elevated/60">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-muted hover:text-fg"
      >
        <ChevronRight
          size={13}
          className={`transition-transform duration-(--motion-quick) ${open ? "rotate-90" : ""}`}
        />
        {isActive && (
          <span className="relative flex size-1.5">
            <span className="absolute inline-flex size-full animate-ping rounded-full bg-accent/60" />
            <span className="relative inline-flex size-1.5 rounded-full bg-accent" />
          </span>
        )}
        <span className={`text-xs ${isActive ? "shimmer-text" : ""}`}>{summary}</span>
      </button>
      {open && (
        <div className="border-t border-border px-3 py-3">
          {hasReasoning && (
            <div className={`relative pl-6 ${hasTools ? "pb-3" : ""}`}>
              {hasTools && <span className="absolute top-4 left-[7px] h-full w-px bg-border" />}
              <span className="absolute top-0.5 left-0 text-subtle">
                <Brain size={12} />
              </span>
              <p className="text-xs leading-relaxed text-muted italic whitespace-pre-wrap">{reasoning}</p>
            </div>
          )}
          {(toolCalls || []).map((call, i) => (
            <ToolStep key={call.callId} call={call} isLast={i === toolCalls!.length - 1} />
          ))}
        </div>
      )}
    </div>
  );
}
