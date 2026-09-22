import { useEffect, useMemo, useState } from "react";
import Sidebar from "./components/Sidebar";
import TopBar from "./components/TopBar";
import MessageStream from "./components/MessageStream";
import Composer from "./components/Composer";
import SettingsModal from "./components/SettingsModal";
import InspectorPanel from "./components/InspectorPanel";
import { useChat } from "./lib/useChat";
import { api } from "./lib/api";

const DEFAULT_USER = "local-user"; // fixed, hardcoded — matches the backend's no-auth default (see README)

function exportAsMarkdown(turns, title) {
  const lines = [`# ${title || "Conversation"}`, ""];
  for (const t of turns) {
    if (t.role === "user") {
      lines.push(`### You`, "", t.content, "");
    } else {
      lines.push(`### Assistant`, "");
      if (t.reasoning) lines.push("<details><summary>Thinking</summary>", "", t.reasoning, "", "</details>", "");
      for (const tc of t.toolCalls || []) {
        lines.push(`> 🔧 \`${tc.toolName}\` — ${tc.status}`, "");
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

export default function App() {
  const chat = useChat(DEFAULT_USER);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [inspectorOpen, setInspectorOpen] = useState(false);
  const [providers, setProviders] = useState({});
  const [config, setConfig] = useState(null);
  const [chatModelOverride, setChatModelOverride] = useState(null);

  useEffect(() => {
    api.getProviders().then(setProviders).catch(() => {});
    api.getConfig().then(setConfig).catch(() => {});
  }, []);

  // Reset the per-chat model override only when the user actually
  // switches to a DIFFERENT conversation — not as a side effect of a
  // brand-new chat acquiring its first real session_id after the first
  // message completes (activeSessionId naturally goes null -> real-id
  // at that point, but it's still the same conversation from the
  // user's perspective, and any model they picked before sending
  // should keep applying to their next message in it too).
  const handleSelectSession = (id) => {
    setChatModelOverride(null);
    chat.selectSession(id);
  };
  const handleNewSession = () => {
    setChatModelOverride(null);
    chat.newSession();
  };

  const activeSessionDetail = useMemo(
    () => chat.sessions.find((s) => s.session_id === chat.activeSessionId) || null,
    [chat.sessions, chat.activeSessionId]
  );

  const title = activeSessionDetail?.title || (chat.turns[0]?.role === "user" ? chat.turns[0].content.slice(0, 60) : null);

  const effectiveProvider = config?.provider;
  const effectiveModel = chatModelOverride || config?.model;

  return (
    <div className="flex h-screen w-screen overflow-hidden bg-bg">
      <Sidebar
        sessions={chat.sessions}
        sessionsLoading={chat.sessionsLoading}
        activeSessionId={chat.activeSessionId}
        onSelect={handleSelectSession}
        onNew={handleNewSession}
        onDelete={chat.deleteSession}
        collapsed={sidebarCollapsed}
        onToggleCollapse={() => setSidebarCollapsed((c) => !c)}
        user={DEFAULT_USER}
      />

      <div className="flex min-w-0 flex-1 flex-col">
        <TopBar
          title={title}
          providers={providers}
          provider={effectiveProvider}
          chatModel={effectiveModel || "…"}
          onChatModelChange={setChatModelOverride}
          onOpenSettings={() => setSettingsOpen(true)}
          onToggleInspector={() => setInspectorOpen((o) => !o)}
          inspectorOpen={inspectorOpen}
          onExport={() => exportAsMarkdown(chat.turns, title)}
        />

        <div className="flex min-h-0 flex-1">
          <div className="flex min-w-0 flex-1 flex-col">
            <div className="flex min-h-0 flex-1 flex-col overflow-y-auto">
              <MessageStream
                turns={chat.turns}
                pendingClarification={chat.pendingClarification}
                onAnswerClarification={chat.answerClarification}
                onRegenerate={() => chat.regenerate(chatModelOverride)}
                isStreaming={chat.isStreaming}
                onStarterPrompt={(text) => chat.sendMessage(text, { model: chatModelOverride })}
              />
            </div>

            {chat.error && (
              <div className="mx-auto mb-2 w-full max-w-[760px] px-4">
                <div className="rounded-md border border-error/30 bg-error/10 px-3 py-2 text-xs text-error">{chat.error}</div>
              </div>
            )}

            <Composer
              onSend={(text) => chat.sendMessage(text, { model: chatModelOverride })}
              onStop={chat.stop}
              isStreaming={chat.isStreaming}
              disabled={!!chat.pendingClarification}
            />
          </div>

          <InspectorPanel
            open={inspectorOpen}
            onClose={() => setInspectorOpen(false)}
            session={activeSessionDetail}
            turns={chat.turns}
          />
        </div>
      </div>

      <SettingsModal open={settingsOpen} onClose={() => setSettingsOpen(false)} onSaved={setConfig} />
    </div>
  );
}
