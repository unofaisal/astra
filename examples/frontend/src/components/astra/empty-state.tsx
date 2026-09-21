import { Compass, FileText, ListTree, Wrench } from "lucide-react";
import { Logo } from "./logo";

const STARTERS = [
  {
    icon: Compass,
    label: "Capabilities",
    text: "What can you help me with?",
    hint: "A short map of what this surface is for.",
  },
  {
    icon: Wrench,
    label: "Tools",
    text: "What tools do you have available?",
    hint: "See the trace, not a brochure.",
  },
  {
    icon: FileText,
    label: "Report",
    text: "Summarize last week's usage in a short report.",
    hint: "Tables, totals, one opinion.",
  },
  {
    icon: ListTree,
    label: "Plan",
    text: "Help me plan out a multi-step task",
    hint: "A sequence you can actually run.",
  },
];

export function EmptyState({ onPick }: { onPick: (text: string) => void }) {
  return (
    <div className="mx-auto flex w-full max-w-2xl flex-1 flex-col justify-center px-5 py-10">
      <div className="mb-8 animate-enter">
        <div className="mb-5 flex size-12 items-center justify-center rounded-lg border border-border bg-elevated shadow-raised">
          <Logo size={26} />
        </div>
        <p className="mb-2 font-mono text-2xs uppercase tracking-[0.22em] text-subtle">Instrument</p>
        <h1 className="font-display text-4xl leading-none tracking-tight text-fg sm:text-5xl">
          What are we
          <br />
          working on?
        </h1>
        <p className="mt-4 max-w-md text-sm leading-relaxed text-muted">
          A control surface for agent runs — traces, tools, and pauses — not a companion.
        </p>
      </div>

      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
        {STARTERS.map((s, i) => (
          <button
            key={s.label}
            type="button"
            onClick={() => onPick(s.text)}
            style={{ animationDelay: `${120 + i * 40}ms` }}
            className="animate-enter group flex items-start gap-3 rounded-lg border border-border bg-surface p-4 text-left shadow-raised transition-colors duration-(--motion-quick) hover:border-border-strong hover:bg-elevated"
          >
            <s.icon size={16} className="mt-0.5 shrink-0 text-subtle group-hover:text-accent" />
            <span>
              <span className="block text-sm text-fg">{s.label}</span>
              <span className="mt-0.5 block text-xs text-muted">{s.hint}</span>
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}
