import { Download, Menu, PanelRight, Search, Share2 } from "lucide-react";
import { toast } from "sonner";
import { useChrome } from "@/lib/chat/chrome";
import type { Turn } from "@/lib/chat/types";
import { IconButton } from "@/components/ui/icon-button";
import { Tooltip } from "@/components/ui/tooltip";

function exportAsMarkdown(turns: Turn[], title: string | null) {
  const lines = [`# ${title || "Conversation"}`, ""];
  for (const t of turns) {
    if (t.role === "user") {
      lines.push(`### You`, "", t.content, "");
    } else {
      lines.push(`### Assistant`, "");
      if (t.reasoning) lines.push("<details><summary>Thinking</summary>", "", t.reasoning, "", "</details>", "");
      for (const tc of t.toolCalls || []) {
        lines.push(`> \`${tc.toolName}\` — ${tc.status}`, "");
      }
      lines.push(t.content, "");
    }
  }
  const blob = new Blob([lines.join("\n")], { type: "text/markdown" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `${(title || "conversation").replace(/[^a-z0-9]+/gi, "-")}.md`;
  a.click();
  URL.revokeObjectURL(url);
}

export function TopBar({ title, turns }: { title: string | null; turns: Turn[] }) {
  const chrome = useChrome();

  return (
    <header className="relative z-10 flex h-14 shrink-0 items-center gap-2 border-b border-border bg-surface/80 px-2 backdrop-blur-sm sm:px-3">
      <IconButton
        label="Open chats"
        className="md:hidden"
        onClick={() => chrome.setMobileNavOpen(true)}
      >
        <Menu size={18} />
      </IconButton>
      <div className="min-w-0 flex-1">
        <h1 className="truncate text-sm text-fg">{title || "New chat"}</h1>
      </div>
      <IconButton label="Search" onClick={() => chrome.setCommandOpen(true)}>
        <Search size={16} />
      </IconButton>
      <Tooltip content="Export as Markdown">
        <IconButton
          label="Export"
          onClick={() => {
            if (turns.length === 0) {
              toast.message("Nothing to export yet");
              return;
            }
            exportAsMarkdown(turns, title);
            toast.success("Downloaded Markdown");
          }}
        >
          <Download size={16} />
        </IconButton>
      </Tooltip>
      <Tooltip content="Share — no public-link endpoint yet.">
        <IconButton label="Share" disabled>
          <Share2 size={16} />
        </IconButton>
      </Tooltip>
      <IconButton
        label="Run inspector"
        active={chrome.inspectorOpen}
        onClick={chrome.toggleInspector}
      >
        <PanelRight size={16} />
      </IconButton>
    </header>
  );
}
