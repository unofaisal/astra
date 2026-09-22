import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Command } from "cmdk";
import {
  BarChart3,
  MessageSquare,
  Moon,
  PanelRight,
  Plus,
  Search,
  Settings,
  Sun,
  Trash2,
} from "lucide-react";
import { useChrome } from "@/lib/chat/chrome";
import { useChatContext } from "@/lib/chat/use-chat";

export function CommandPalette() {
  const chrome = useChrome();
  const chat = useChatContext();
  const [q, setQ] = useState("");

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        chrome.setCommandOpen(!chrome.commandOpen);
      }
      if (e.key === "Escape") chrome.setCommandOpen(false);
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "n" && !e.shiftKey) {
        const tag = (e.target as HTMLElement)?.tagName;
        if (tag === "INPUT" || tag === "TEXTAREA") return;
        e.preventDefault();
        chat.newSession();
      }
      if ((e.metaKey || e.ctrlKey) && e.key === "[") {
        e.preventDefault();
        chrome.toggleSidebar();
      }
      if ((e.metaKey || e.ctrlKey) && e.key === "]") {
        e.preventDefault();
        chrome.toggleInspector();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [chrome, chat]);

  const sessions = useMemo(() => chat.sessions.slice(0, 12), [chat.sessions]);

  if (!chrome.commandOpen) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center px-4 pt-24">
      <button
        type="button"
        aria-label="Dismiss"
        className="absolute inset-0 bg-bg/70"
        onClick={() => chrome.setCommandOpen(false)}
      />
      <Command
        className="relative z-10 w-full max-w-lg overflow-hidden rounded-xl border border-border bg-surface shadow-panel animate-enter [&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:pt-2 [&_[cmdk-group-heading]]:pb-1 [&_[cmdk-group-heading]]:font-mono [&_[cmdk-group-heading]]:text-2xs [&_[cmdk-group-heading]]:uppercase [&_[cmdk-group-heading]]:tracking-wider [&_[cmdk-group-heading]]:text-subtle"
        loop
      >
        <div className="flex items-center gap-2 border-b border-border px-3">
          <Search size={15} className="text-subtle" />
          <Command.Input
            autoFocus
            value={q}
            onValueChange={setQ}
            placeholder="Search chats or jump…"
            className="h-12 w-full bg-transparent text-sm text-fg placeholder:text-subtle focus:outline-none"
          />
        </div>
        <Command.List className="max-h-80 overflow-y-auto p-1">
          <Command.Empty className="px-3 py-6 text-center text-sm text-subtle">
            Nothing matches.
          </Command.Empty>
          <Command.Group heading="Jump">
            <Item
              icon={Plus}
              label="New chat"
              onSelect={() => {
                chat.newSession();
                chrome.setCommandOpen(false);
              }}
            />
            <Item
              icon={Settings}
              label="Settings"
              onSelect={() => chrome.openSettings("general")}
            />
            <Item
              icon={BarChart3}
              label="Usage"
              onSelect={() => chrome.openSettings("usage")}
            />
            <Item
              icon={PanelRight}
              label={chrome.inspectorOpen ? "Hide inspector" : "Show inspector"}
              onSelect={() => {
                chrome.toggleInspector();
                chrome.setCommandOpen(false);
              }}
            />
            <Item
              icon={chrome.theme === "dark" ? Sun : Moon}
              label={chrome.theme === "dark" ? "Light theme" : "Dark theme"}
              onSelect={() => {
                chrome.toggleTheme();
                chrome.setCommandOpen(false);
              }}
            />
          </Command.Group>
          {sessions.length > 0 && (
            <Command.Group heading="Chats">
              {sessions.map((s) => (
                <Item
                  key={s.session_id}
                  icon={MessageSquare}
                  label={s.title || "Untitled"}
                  onSelect={() => {
                    chat.selectSession(s.session_id);
                    chrome.setCommandOpen(false);
                  }}
                  trailing={
                    <button
                      type="button"
                      className="rounded-sm p-1 text-subtle hover:text-danger"
                      onClick={(e) => {
                        e.stopPropagation();
                        chat.deleteSession(s.session_id);
                      }}
                    >
                      <Trash2 size={12} />
                    </button>
                  }
                />
              ))}
            </Command.Group>
          )}
        </Command.List>
      </Command>
    </div>
  );
}

function Item({
  icon: Icon,
  label,
  onSelect,
  trailing,
}: {
  icon: typeof Search;
  label: string;
  onSelect: () => void;
  trailing?: ReactNode;
}) {
  return (
    <Command.Item
      value={label}
      onSelect={onSelect}
      className="flex cursor-pointer items-center gap-2 rounded-md px-2 py-2 text-sm text-muted aria-selected:bg-hover aria-selected:text-fg"
    >
      <Icon size={14} />
      <span className="min-w-0 flex-1 truncate">{label}</span>
      {trailing}
    </Command.Item>
  );
}
