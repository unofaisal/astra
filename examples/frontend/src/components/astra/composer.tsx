import { useEffect, useRef, useState } from "react";
import { ArrowUp, ChevronDown, HelpCircle, Paperclip, Square } from "lucide-react";
import { cn } from "@/lib/utils";
import type { Clarification } from "@/lib/chat/types";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Tooltip } from "@/components/ui/tooltip";

export function Composer({
  onSend,
  onAnswerClarification,
  onStop,
  isStreaming,
  clarification,
  model,
  models,
  onModelChange,
}: {
  onSend: (text: string) => void;
  onAnswerClarification: (text: string) => void;
  onStop: () => void;
  isStreaming: boolean;
  clarification: Clarification | null;
  model: string;
  models: string[];
  onModelChange: (m: string) => void;
}) {
  const [value, setValue] = useState("");
  const [focused, setFocused] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    if (clarification) textareaRef.current?.focus();
  }, [clarification]);

  const autosize = (el: HTMLTextAreaElement) => {
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  };

  const submit = () => {
    const text = value.trim();
    if (!text || isStreaming) return;
    if (clarification) onAnswerClarification(text);
    else onSend(text);
    setValue("");
    if (textareaRef.current) textareaRef.current.style.height = "auto";
  };

  return (
    <div className="mx-auto w-full max-w-3xl px-4 pb-4 pt-1">
      <div
        className={cn(
          "overflow-hidden rounded-xl border bg-surface shadow-panel transition-[border-color,box-shadow] duration-(--motion-fast)",
          clarification
            ? "border-attention/50"
            : focused
              ? "border-ring/40"
              : "border-border",
        )}
      >
        {clarification && (
          <div className="border-b border-attention/25 bg-attention/8 px-4 pt-3 pb-3">
            <div className="mb-2 flex items-start gap-2">
              <span className="mt-0.5 flex size-5 shrink-0 items-center justify-center rounded-full bg-attention/15 text-attention">
                <HelpCircle size={12} />
              </span>
              <p className="text-sm leading-relaxed text-fg">{clarification.question}</p>
            </div>
            {clarification.options.length > 0 && (
              <div className="flex flex-wrap gap-1.5 pl-7">
                {clarification.options.map((opt) => (
                  <button
                    key={opt}
                    type="button"
                    disabled={isStreaming}
                    onClick={() => onAnswerClarification(opt)}
                    className="rounded-md border border-attention/40 bg-elevated px-3 py-1.5 text-sm text-fg transition-colors duration-(--motion-quick) hover:bg-attention/15 disabled:opacity-50"
                  >
                    {opt}
                  </button>
                ))}
              </div>
            )}
            <p className="mt-2 pl-7 text-2xs text-subtle">
              {clarification.options.length > 0 ? "Or type your own answer." : "Type your answer below."}
            </p>
          </div>
        )}

        <div className="flex items-end gap-1 px-2 pt-2">
          <Tooltip content="Attach a file — not wired on the runtime yet.">
            <button
              type="button"
              disabled
              className="mb-1 flex size-10 shrink-0 items-center justify-center rounded-md text-subtle/50"
            >
              <Paperclip size={16} />
            </button>
          </Tooltip>
          <textarea
            ref={textareaRef}
            rows={1}
            value={value}
            onFocus={() => setFocused(true)}
            onBlur={() => setFocused(false)}
            onChange={(e) => {
              setValue(e.target.value);
              autosize(e.target);
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                submit();
              }
            }}
            placeholder={clarification ? "Type your answer…" : "Message Astra…"}
            className="max-h-[200px] min-h-10 flex-1 resize-none bg-transparent py-2.5 text-sm text-fg placeholder:text-subtle focus:outline-none"
          />
          {isStreaming ? (
            <button
              type="button"
              onClick={onStop}
              title="Stop"
              className="mb-1 flex size-10 shrink-0 items-center justify-center rounded-md bg-hover text-fg transition-transform duration-(--motion-micro) active:scale-95"
            >
              <Square size={13} fill="currentColor" />
            </button>
          ) : (
            <button
              type="button"
              onClick={submit}
              disabled={!value.trim()}
              title="Send"
              className="mb-1 flex size-10 shrink-0 items-center justify-center rounded-md bg-accent text-accent-fg transition-transform duration-(--motion-micro) active:scale-95 disabled:opacity-30"
            >
              <ArrowUp size={16} />
            </button>
          )}
        </div>

        <div className="flex items-center justify-between px-3 pb-2">
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <button
                type="button"
                className="flex items-center gap-1 rounded-sm px-1.5 py-1 font-mono text-2xs text-muted hover:bg-hover hover:text-fg"
              >
                {model || "Loading model…"}
                <ChevronDown size={11} />
              </button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start">
              <DropdownMenuLabel>This chat only</DropdownMenuLabel>
              {models.length === 0 && (
                <div className="px-2.5 py-2 text-xs text-subtle">No models listed</div>
              )}
              {models.map((m) => (
                <DropdownMenuItem
                  key={m}
                  onSelect={() => onModelChange(m)}
                  className={m === model ? "text-accent" : ""}
                >
                  <span className="font-mono text-xs">{m}</span>
                </DropdownMenuItem>
              ))}
            </DropdownMenuContent>
          </DropdownMenu>
          <p className="text-2xs text-subtle">Enter to send · Shift+Enter for a line</p>
        </div>
      </div>
    </div>
  );
}
