import { useMemo, useState } from "react";
import {
  BarChart3,
  Moon,
  PanelLeft,
  PanelLeftClose,
  Search,
  Settings,
  SquarePen,
  Sun,
  Trash2,
} from "lucide-react";
import { groupSessionsByRecency, relativeTime } from "@/lib/chat/transform";
import { useChrome } from "@/lib/chat/chrome";
import { useChatContext } from "@/lib/chat/use-chat";
import { IconButton } from "@/components/ui/icon-button";
import { Input } from "@/components/ui/input";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Wordmark } from "./logo";
import { Kbd } from "@/components/ui/kbd";

export function Sidebar({ collapsed }: { collapsed: boolean }) {
  const chat = useChatContext();
  const chrome = useChrome();
  const [query, setQuery] = useState("");

  const filtered = useMemo(() => {
    if (!query.trim()) return chat.sessions;
    const q = query.toLowerCase();
    return chat.sessions.filter((s) => (s.title || "Untitled").toLowerCase().includes(q));
  }, [chat.sessions, query]);

  const grouped = useMemo(() => groupSessionsByRecency(filtered), [filtered]);

  if (collapsed) {
    return (
      <aside className="flex h-full w-14 shrink-0 flex-col items-center border-r border-border bg-surface py-3">
        <IconButton label="Expand sidebar" onClick={chrome.toggleSidebar}>
          <PanelLeft size={18} />
        </IconButton>
        <IconButton label="New chat" className="mt-3" onClick={chat.newSession}>
          <SquarePen size={18} />
        </IconButton>
        <div className="mt-auto flex flex-col items-center gap-1">
          <IconButton
            label={chrome.theme === "dark" ? "Light theme" : "Dark theme"}
            onClick={chrome.toggleTheme}
          >
            {chrome.theme === "dark" ? <Sun size={16} /> : <Moon size={16} />}
          </IconButton>
          <IconButton label="Settings" onClick={() => chrome.openSettings("general")}>
            <Settings size={16} />
          </IconButton>
        </div>
      </aside>
    );
  }

  return (
    <aside className="flex h-full w-72 shrink-0 flex-col border-r border-border bg-surface">
      <div className="flex items-center gap-2 px-3 py-3">
        <Wordmark />
        <IconButton label="Collapse sidebar" className="ml-auto" size="sm" onClick={chrome.toggleSidebar}>
          <PanelLeftClose size={16} />
        </IconButton>
      </div>

      <div className="px-3">
        <button
          type="button"
          onClick={() => {
            chat.newSession();
            chrome.setMobileNavOpen(false);
          }}
          className="mb-2 flex h-10 w-full items-center justify-between rounded-md border border-border bg-elevated px-3 text-sm text-fg shadow-raised transition-colors duration-(--motion-quick) hover:border-border-strong hover:bg-hover"
        >
          <span className="flex items-center gap-2">
            <SquarePen size={15} />
            New chat
          </span>
          <Kbd>N</Kbd>
        </button>
        <div className="relative mb-3">
          <Search size={14} className="pointer-events-none absolute top-1/2 left-3 -translate-y-1/2 text-subtle" />
          <Input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search chats"
            className="pl-9"
          />
        </div>
      </div>

      <ScrollArea className="min-h-0 flex-1 px-2 pb-2">
        {chat.sessionsLoading && <div className="px-3 py-2 text-xs text-subtle">Loading…</div>}
        {!chat.sessionsLoading && grouped.length === 0 && (
          <div className="px-3 py-6 text-center text-xs text-subtle">No conversations yet</div>
        )}
        {grouped.map(([label, list]) => (
          <div key={label} className="mb-3">
            <div className="px-3 pb-1 font-mono text-2xs uppercase tracking-wider text-subtle">{label}</div>
            <div className="space-y-0.5">
              {list.map((s) => {
                const active = s.session_id === chat.activeSessionId;
                return (
                  <div key={s.session_id} className="group relative">
                    {active && (
                      <span className="absolute top-1/2 left-0 h-4 w-0.5 -translate-y-1/2 rounded-full bg-accent" />
                    )}
                    <button
                      type="button"
                      onClick={() => {
                        chat.selectSession(s.session_id);
                        chrome.setMobileNavOpen(false);
                      }}
                      className={`flex w-full items-center gap-2 rounded-md py-2 pr-9 pl-3 text-left text-sm transition-colors duration-(--motion-quick) ${
                        active ? "bg-hover text-fg" : "text-muted hover:bg-elevated hover:text-fg"
                      }`}
                    >
                      <span className="min-w-0 flex-1 truncate">{s.title || "Untitled"}</span>
                      <span className="shrink-0 font-mono text-2xs text-subtle tabular-nums">
                        {relativeTime(s.last_active)}
                      </span>
                    </button>
                    <button
                      type="button"
                      title="Delete"
                      onClick={() => chat.deleteSession(s.session_id)}
                      className="absolute top-1/2 right-1.5 flex size-8 -translate-y-1/2 items-center justify-center rounded-sm text-subtle opacity-0 hover:bg-hover hover:text-danger group-hover:opacity-100"
                    >
                      <Trash2 size={13} />
                    </button>
                  </div>
                );
              })}
            </div>
          </div>
        ))}
      </ScrollArea>

      <div className="border-t border-border p-2">
        <div className="mb-1 flex items-center gap-2 px-2 py-1">
          <span className="truncate font-mono text-2xs text-subtle">local-user</span>
        </div>
        <div className="flex items-center">
          <button
            type="button"
            onClick={() => chrome.openSettings("general")}
            className="flex flex-1 items-center gap-2 rounded-md px-2 py-2 text-sm text-muted hover:bg-hover hover:text-fg"
          >
            <Settings size={15} />
            Settings
          </button>
          <IconButton
            label="Usage"
            size="sm"
            onClick={() => chrome.openSettings("usage")}
          >
            <BarChart3 size={15} />
          </IconButton>
          <IconButton
            label={chrome.theme === "dark" ? "Light theme" : "Dark theme"}
            size="sm"
            onClick={chrome.toggleTheme}
          >
            {chrome.theme === "dark" ? <Sun size={15} /> : <Moon size={15} />}
          </IconButton>
        </div>
      </div>
    </aside>
  );
}
