import { useState } from "react";
import { ChevronRight, Brain } from "lucide-react";

export default function ReasoningBlock({ content, streaming }) {
  const [open, setOpen] = useState(false);
  if (!content) return null;

  return (
    <div className="my-2 overflow-hidden rounded-md border border-border/60">
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-text-tertiary hover:text-text-secondary"
      >
        <ChevronRight size={13} className={`transition-transform ${open ? "rotate-90" : ""}`} />
        <Brain size={13} />
        <span className="text-xs">{streaming ? "Thinking…" : "Thought process"}</span>
      </button>
      {open && (
        <div className="whitespace-pre-wrap border-t border-border/60 px-3 py-2 font-mono text-[12px] leading-relaxed text-text-tertiary">
          {content}
        </div>
      )}
    </div>
  );
}
