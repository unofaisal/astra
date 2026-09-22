import { useState } from "react";
import { AlertCircle, Check, Copy, Paperclip, RotateCcw } from "lucide-react";
import { toast } from "sonner";
import type { Turn } from "@/lib/chat/types";
import { IconButton } from "@/components/ui/icon-button";
import { ActivityTimeline } from "./activity-timeline";
import { Logo } from "./logo";
import { Markdown } from "./markdown";

function TypingIndicator() {
  return (
    <div className="flex items-center gap-1 py-2" aria-label="Waiting for first token">
      {[0, 1, 2].map((i) => (
        <span
          key={i}
          className="size-1.5 rounded-full bg-accent"
          style={{ animation: `pulse-dot 0.9s ease-in-out ${i * 0.15}s infinite` }}
        />
      ))}
    </div>
  );
}

function Actions({
  onCopy,
  onRegenerate,
  copied,
  align = "start",
}: {
  onCopy: () => void;
  onRegenerate?: () => void;
  copied: boolean;
  align?: "start" | "end";
}) {
  return (
    <div
      className={`mt-1 flex items-center gap-0.5 opacity-100 md:opacity-0 md:transition-opacity md:duration-(--motion-quick) md:group-hover:opacity-100 ${
        align === "end" ? "justify-end" : ""
      }`}
    >
      <IconButton label={copied ? "Copied" : "Copy"} size="sm" onClick={onCopy}>
        {copied ? <Check size={14} className="text-success" /> : <Copy size={14} />}
      </IconButton>
      {onRegenerate && (
        <IconButton label="Regenerate" size="sm" onClick={onRegenerate}>
          <RotateCcw size={14} />
        </IconButton>
      )}
    </div>
  );
}

export function Message({
  turn,
  isLastAssistant,
  onRegenerate,
}: {
  turn: Turn;
  isLastAssistant: boolean;
  onRegenerate?: () => void;
}) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    await navigator.clipboard.writeText(turn.content);
    setCopied(true);
    toast.success("Copied message");
    setTimeout(() => setCopied(false), 1400);
  };

  if (turn.role === "user") {
    return (
      <article className="group flex justify-end animate-enter">
        <div className="max-w-lg">
          <div className="rounded-xl rounded-br-sm border border-border bg-elevated px-4 py-3 text-sm leading-relaxed text-fg shadow-raised">
            {turn.attachments && turn.attachments.length > 0 && (
              <div className="mb-2 flex flex-wrap gap-1.5">
                {turn.attachments.map((a, i) => (
                  <span
                    key={i}
                    className="flex items-center gap-1 rounded-sm bg-hover px-1.5 py-0.5 text-xs text-muted"
                  >
                    <Paperclip size={11} />
                    {a.file_name || "attachment"}
                  </span>
                ))}
              </div>
            )}
            <div className="whitespace-pre-wrap">{turn.content}</div>
          </div>
          <Actions onCopy={copy} copied={copied} align="end" />
        </div>
      </article>
    );
  }

  const waiting = turn.streaming && !turn.content && !turn.toolCalls?.length && !turn.reasoning;

  return (
    <article className="group flex gap-3 animate-enter">
      <div className="mt-1 hidden size-8 shrink-0 items-center justify-center rounded-md border border-border bg-elevated sm:flex">
        <Logo size={16} />
      </div>
      <div className="min-w-0 flex-1">
        <div className="mb-1 flex items-baseline gap-2">
          <span className="font-mono text-2xs uppercase tracking-[0.18em] text-subtle">Astra</span>
          {turn.model && <span className="font-mono text-2xs text-subtle">{turn.model}</span>}
        </div>
        <ActivityTimeline
          reasoning={turn.reasoning}
          toolCalls={turn.toolCalls}
          streaming={turn.streaming}
        />
        {turn.isError && (
          <div className="mb-2 flex items-center gap-2 rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-xs text-danger">
            <AlertCircle size={13} />
            This response ended with an error.
          </div>
        )}
        {waiting && <TypingIndicator />}
        {turn.content && <Markdown content={turn.content} />}
        {!turn.streaming && turn.content && (
          <Actions
            onCopy={copy}
            copied={copied}
            onRegenerate={isLastAssistant ? onRegenerate : undefined}
          />
        )}
      </div>
    </article>
  );
}
