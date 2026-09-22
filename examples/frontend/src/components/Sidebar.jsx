import { useMemo, useState } from "react";
import { PanelLeftClose, PanelLeft, SquarePen, Search, Trash2, User } from "lucide-react";
import { groupSessionsByRecency } from "../lib/transform";

export default function Sidebar({ sessions, sessionsLoading, activeSessionId, onSelect, onNew, onDelete, collapsed, onToggleCollapse, user }) {
  const [query, setQuery] = useState("");

  const filtered = useMemo(() => {
    if (!query.trim()) return sessions;
    const q = query.toLowerCase();
    return sessions.filter((s) => (s.title || "Untitled").toLowerCase().includes(q));
  }, [sessions, query]);

  const grouped = useMemo(() => groupSessionsByRecency(filtered), [filtered]);

  if (collapsed) {
    return (
      <div className="flex h-full w-12 flex-col items-center border-r border-border bg-surface-1 py-3">
        <button onClick={onToggleCollapse} className="mb-3 rounded-md p-2 text-text-tertiary hover:bg-surface-2 hover:text-text-primary">
          <PanelLeft size={17} />
        </button>
        <button onClick={onNew} className="rounded-md p-2 text-text-tertiary hover:bg-surface-2 hover:text-text-primary">
          <SquarePen size={17} />
        </button>
      </div>
    );
  }

  return (
    <div className="flex h-full w-64 shrink-0 flex-col border-r border-border bg-surface-1">
      <div className="flex items-center gap-1.5 p-2.5">
        <div className="flex h-6 w-6 items-center justify-center rounded bg-accent/15">
          <div className="h-2 w-2 rounded-full bg-accent" />
        </div>
        <span className="text-sm font-medium text-text-primary">astra</span>
        <button
          onClick={onToggleCollapse}
          className="ml-auto rounded-md p-1.5 text-text-tertiary hover:bg-surface-2 hover:text-text-primary"
          title="Collapse sidebar"
        >
          <PanelLeftClose size={16} />
        </button>
      </div>

      <div className="px-2.5">
        <button
          onClick={onNew}
          className="mb-2 flex w-full items-center gap-2 rounded-md border border-border bg-surface-2 px-2.5 py-2 text-sm text-text-primary hover:border-border-strong hover:bg-surface-3"
        >
          <SquarePen size={15} />
          New chat
        </button>
        <div className="relative mb-2">
          <Search size={13} className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-text-tertiary" />
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search chats"
            className="w-full rounded-md border border-border bg-surface-2 py-1.5 pl-7 pr-2 text-xs text-text-primary placeholder:text-text-tertiary focus:border-accent/60 focus:outline-none"
          />
        </div>
      </div>

      <div className="flex-1 overflow-y-auto px-2.5 pb-2">
        {sessionsLoading && <div className="px-1 py-2 text-xs text-text-tertiary">Loading…</div>}
        {!sessionsLoading && grouped.length === 0 && (
          <div className="px-1 py-2 text-xs text-text-tertiary">No conversations yet</div>
        )}
        {grouped.map(([label, list]) => (
          <div key={label} className="mb-3">
            <div className="mb-1 px-1 text-[10px] font-medium uppercase tracking-wide text-text-tertiary">{label}</div>
            <div className="space-y-0.5">
              {list.map((s) => (
                <div
                  key={s.session_id}
                  className={`group flex items-center gap-1 rounded-md px-2 py-1.5 text-sm ${
                    s.session_id === activeSessionId ? "bg-surface-3 text-text-primary" : "text-text-secondary hover:bg-surface-2"
                  }`}
                >
                  <button onClick={() => onSelect(s.session_id)} className="flex-1 truncate text-left">
                    {s.title || "Untitled"}
                  </button>
                  <button
                    onClick={() => onDelete(s.session_id)}
                    className="shrink-0 rounded p-1 text-text-tertiary opacity-0 hover:bg-surface-3 hover:text-error group-hover:opacity-100"
                    title="Delete"
                  >
                    <Trash2 size={12} />
                  </button>
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>

      <div className="flex items-center gap-2 border-t border-border p-2.5">
        <div className="flex h-6 w-6 items-center justify-center rounded-full bg-surface-3 text-text-tertiary">
          <User size={13} />
        </div>
        <span className="text-xs text-text-secondary">{user}</span>
      </div>
    </div>
  );
}
