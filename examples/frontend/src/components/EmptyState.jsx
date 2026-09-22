import { Compass, Wrench, FileText, Sparkle } from "lucide-react";

const STARTERS = [
  { icon: Compass, text: "What can you help me with?" },
  { icon: Wrench, text: "What tools do you have available?" },
  { icon: FileText, text: "Summarize this in a short report" },
  { icon: Sparkle, text: "Help me plan out a multi-step task" },
];

export default function EmptyState({ onPick }) {
  return (
    <div className="mx-auto flex w-full max-w-[620px] flex-1 flex-col items-center justify-center px-4 text-center">
      <div className="mb-3 flex h-11 w-11 items-center justify-center rounded-lg border border-border bg-surface-1">
        <div className="h-2.5 w-2.5 rounded-full bg-accent" />
      </div>
      <h1 className="mb-1 text-lg font-medium text-text-primary">How can I help?</h1>
      <p className="mb-6 text-sm text-text-tertiary">Start a conversation, or try one of these.</p>
      <div className="grid w-full grid-cols-1 gap-2 sm:grid-cols-2">
        {STARTERS.map(({ icon: Icon, text }) => (
          <button
            key={text}
            onClick={() => onPick(text)}
            className="flex items-center gap-2.5 rounded-md border border-border bg-surface-1 px-3 py-2.5 text-left text-sm text-text-secondary hover:border-border-strong hover:bg-surface-2 hover:text-text-primary"
          >
            <Icon size={15} className="shrink-0 text-text-tertiary" />
            {text}
          </button>
        ))}
      </div>
    </div>
  );
}
