import { useState } from "react";
import { Activity, FileStack, X } from "lucide-react";
import { formatTokens } from "@/lib/chat/transform";
import { useChatContext } from "@/lib/chat/use-chat";
import { IconButton } from "@/components/ui/icon-button";
import { ScrollArea } from "@/components/ui/scroll-area";

function Stat({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="rounded-md border border-border bg-elevated px-3 py-2.5">
      <div className="font-mono text-2xs uppercase tracking-wider text-subtle">{label}</div>
      <div className="mt-1 font-mono text-sm text-fg tabular-nums">{value}</div>
    </div>
  );
}

export function Inspector({ onClose }: { onClose: () => void }) {
  const chat = useChatContext();
  const [tab, setTab] = useState<"run" | "artifacts">("run");
  const session = chat.sessions.find((s) => s.session_id === chat.activeSessionId) || null;

  return (
    <aside className="flex h-full w-80 shrink-0 flex-col border-l border-border bg-surface">
      <div className="flex items-center gap-1 border-b border-border px-2">
        <TabBtn icon={Activity} label="Run" active={tab === "run"} onClick={() => setTab("run")} />
        <TabBtn
          icon={FileStack}
          label="Artifacts"
          active={tab === "artifacts"}
          onClick={() => setTab("artifacts")}
        />
        <IconButton label="Close inspector" size="sm" className="ml-auto" onClick={onClose}>
          <X size={15} />
        </IconButton>
      </div>
      <ScrollArea className="min-h-0 flex-1">
        {tab === "run" ? (
          !session && chat.turns.length === 0 ? (
            <p className="p-4 text-xs text-subtle">Send a message to see run details.</p>
          ) : (
            <div className="space-y-4 p-3">
              <div className="grid grid-cols-2 gap-2">
                <Stat label="Turns" value={session?.turn_count ?? chat.turns.filter((t) => t.role === "user").length} />
                <Stat label="Messages" value={session?.message_count ?? chat.turns.length} />
                <Stat label="Input" value={formatTokens(session?.total_input_tokens ?? 0)} />
                <Stat label="Output" value={formatTokens(session?.total_output_tokens ?? 0)} />
                <Stat
                  label="Est. cost"
                  value={
                    session?.estimated_cost != null ? `$${session.estimated_cost.toFixed(4)}` : "$0.00"
                  }
                />
                <Stat label="Ended" value={session?.ended_reason || "in progress"} />
              </div>
            </div>
          )
        ) : (
          <div className="flex flex-col items-center justify-center gap-2 px-6 py-16 text-center">
            <FileStack size={22} className="text-subtle" />
            <p className="text-sm text-muted">Artifacts — coming soon</p>
            <p className="max-w-xs text-xs leading-relaxed text-subtle">
              Generated documents and code will land here, separate from the transcript, once the
              runtime emits an artifact event.
            </p>
          </div>
        )}
      </ScrollArea>
    </aside>
  );
}

function TabBtn({
  icon: Icon,
  label,
  active,
  onClick,
}: {
  icon: typeof Activity;
  label: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`flex items-center gap-1.5 border-b-2 px-3 py-3 text-xs ${
        active ? "border-accent text-fg" : "border-transparent text-subtle hover:text-muted"
      }`}
    >
      <Icon size={13} />
      {label}
    </button>
  );
}
