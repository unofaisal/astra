import { useState } from "react";
import { X, Activity, Sparkles, Wrench, Coins } from "lucide-react";
import StatusIcon from "./StatusIcon";
import { formatElapsed } from "../lib/transform";

function Stat({ label, value }) {
  return (
    <div className="rounded-md border border-border bg-surface-2 px-2.5 py-2">
      <div className="text-[10px] uppercase tracking-wide text-text-tertiary">{label}</div>
      <div className="mt-0.5 font-mono text-sm text-text-primary">{value}</div>
    </div>
  );
}

function InspectorTab({ session, turns }) {
  if (!session) {
    return <p className="p-4 text-xs text-text-tertiary">Send a message to see run details here.</p>;
  }

  const allToolCalls = turns.flatMap((t) => t.toolCalls || []);

  return (
    <div className="space-y-4 p-3">
      <div className="grid grid-cols-2 gap-2">
        <Stat label="Turns" value={session.turn_count ?? 0} />
        <Stat label="Messages" value={session.message_count ?? 0} />
        <Stat label="Input tokens" value={session.total_input_tokens ?? 0} />
        <Stat label="Output tokens" value={session.total_output_tokens ?? 0} />
        <Stat label="Est. cost" value={session.estimated_cost != null ? `$${session.estimated_cost.toFixed(4)}` : "—"} />
        <Stat label="Ended" value={session.ended_reason || "in progress"} />
      </div>

      <div>
        <div className="mb-1.5 flex items-center gap-1.5 text-xs font-medium text-text-secondary">
          <Wrench size={12} /> Tool calls ({allToolCalls.length})
        </div>
        {allToolCalls.length === 0 && <p className="text-xs text-text-tertiary">None yet.</p>}
        <div className="space-y-1">
          {allToolCalls.map((tc) => (
            <div key={tc.callId} className="flex items-center gap-2 rounded-md border border-border bg-surface-2 px-2 py-1.5">
              <StatusIcon status={tc.status} size={12} />
              <span className="flex-1 truncate font-mono text-xs text-text-secondary">{tc.toolName}</span>
              {tc.elapsedMs != null && <span className="text-[10px] text-text-tertiary">{formatElapsed(tc.elapsedMs)}</span>}
            </div>
          ))}
        </div>
      </div>

      <div>
        <div className="mb-1.5 text-xs font-medium text-text-secondary">Raw session</div>
        <pre className="max-h-64 overflow-auto rounded-md border border-border bg-surface-2 p-2 font-mono text-[10px] leading-relaxed text-text-tertiary">
          {JSON.stringify(session, null, 2)}
        </pre>
      </div>
    </div>
  );
}

function ArtifactsTab() {
  return (
    <div className="flex flex-1 flex-col items-center justify-center gap-2 p-6 text-center">
      <Sparkles size={22} className="text-text-tertiary" />
      <p className="text-sm text-text-secondary">Artifacts — coming soon</p>
      <p className="max-w-[220px] text-xs text-text-tertiary">
        This panel will render generated documents, code, and other structured content the agent produces, separate
        from the conversation. Requires the backend to emit an artifact-type event — not implemented yet.
      </p>
    </div>
  );
}

export default function InspectorPanel({ open, onClose, session, turns }) {
  const [tab, setTab] = useState("run");
  if (!open) return null;

  return (
    <div className="flex h-full w-80 shrink-0 flex-col border-l border-border bg-surface-1">
      <div className="flex items-center gap-1 border-b border-border px-2">
        <TabButton icon={Activity} label="Run" active={tab === "run"} onClick={() => setTab("run")} />
        <TabButton icon={Sparkles} label="Artifacts" active={tab === "artifacts"} onClick={() => setTab("artifacts")} />
        <button onClick={onClose} className="ml-auto rounded p-1.5 text-text-tertiary hover:bg-surface-2 hover:text-text-primary">
          <X size={15} />
        </button>
      </div>
      <div className="flex flex-1 flex-col overflow-y-auto">
        {tab === "run" ? <InspectorTab session={session} turns={turns} /> : <ArtifactsTab />}
      </div>
    </div>
  );
}

function TabButton({ icon: Icon, label, active, onClick }) {
  return (
    <button
      onClick={onClick}
      className={`flex items-center gap-1.5 border-b-2 px-2.5 py-2.5 text-xs ${
        active ? "border-accent text-text-primary" : "border-transparent text-text-tertiary hover:text-text-secondary"
      }`}
    >
      <Icon size={13} />
      {label}
    </button>
  );
}
