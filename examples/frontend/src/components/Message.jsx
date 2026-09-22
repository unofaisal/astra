import { useState } from "react";
import { Copy, Check, RotateCcw, Paperclip, AlertCircle } from "lucide-react";
import Markdown from "./Markdown";
import ReasoningBlock from "./ReasoningBlock";
import ToolCallTrace from "./ToolCallTrace";

function HoverActions({ onCopy, onRegenerate, copied }) {
  return (
    <div className="mt-1 flex items-center gap-1 opacity-0 transition-opacity group-hover:opacity-100">
      <button
        onClick={onCopy}
        title="Copy"
        className="rounded p-1 text-text-tertiary hover:bg-surface-3 hover:text-text-secondary"
      >
        {copied ? <Check size={13} className="text-success" /> : <Copy size={13} />}
      </button>
      {onRegenerate && (
        <button
          onClick={onRegenerate}
          title="Regenerate"
          className="rounded p-1 text-text-tertiary hover:bg-surface-3 hover:text-text-secondary"
        >
          <RotateCcw size={13} />
        </button>
      )}
    </div>
  );
}

export default function Message({ turn, isLastAssistant, onRegenerate }) {
  const [copied, setCopied] = useState(false);
  const copy = () => {
    navigator.clipboard.writeText(turn.content);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  if (turn.role === "user") {
    return (
      <div className="group flex justify-end">
        <div className="max-w-[75%]">
          <div className="rounded-lg bg-surface-2 px-3.5 py-2.5 text-sm text-text-primary">
            {turn.attachments?.length > 0 && (
              <div className="mb-1.5 flex flex-wrap gap-1.5">
                {turn.attachments.map((a, i) => (
                  <span key={i} className="flex items-center gap-1 rounded bg-surface-3 px-1.5 py-0.5 text-xs text-text-tertiary">
                    <Paperclip size={11} />
                    {a.file_name || "attachment"}
                  </span>
                ))}
              </div>
            )}
            <div className="whitespace-pre-wrap leading-relaxed">{turn.content}</div>
          </div>
          <div className="flex justify-end">
            <HoverActions onCopy={copy} copied={copied} />
          </div>
        </div>
      </div>
    );
  }

  // assistant
  const showCursor = turn.streaming && !turn.content;
  return (
    <div className="group">
      <div className="max-w-[85%]">
        <ReasoningBlock content={turn.reasoning} streaming={turn.streaming && !turn.content} />
        <ToolCallTrace toolCalls={turn.toolCalls} />

        {turn.isError && (
          <div className="mb-1.5 flex items-center gap-1.5 rounded-md border border-error/30 bg-error/10 px-2.5 py-1.5 text-xs text-error">
            <AlertCircle size={13} />
            This response ended with an error.
          </div>
        )}

        {(turn.content || turn.streaming) && (
          <div className="text-sm text-text-primary">
            {turn.content ? <Markdown content={turn.content} /> : null}
            {showCursor && <span className="cursor-blink inline-block h-4 w-1.5 translate-y-0.5 bg-accent" />}
            {turn.streaming && turn.content && (
              <span className="cursor-blink ml-0.5 inline-block h-4 w-1.5 translate-y-0.5 bg-accent" />
            )}
          </div>
        )}

        {!turn.streaming && turn.content && (
          <HoverActions onCopy={copy} copied={copied} onRegenerate={isLastAssistant ? onRegenerate : null} />
        )}
      </div>
    </div>
  );
}
