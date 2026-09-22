import { useEffect, useMemo, useState } from "react";
import { ChatContext, useChat } from "@/lib/chat/use-chat";
import { useChrome } from "@/lib/chat/chrome";
import { api } from "@/lib/chat/api";
import type { AgentConfig } from "@/lib/chat/types";
import { useMedia } from "@/lib/utils";
import { useUrlSession } from "@/lib/use-url-session";
import { Sheet, SheetContent } from "@/components/ui/sheet";
import { CommandPalette } from "./command-palette";
import { Composer } from "./composer";
import { Inspector } from "./inspector";
import { MessageStream } from "./message-stream";
import { SettingsDialog } from "./settings-dialog";
import { Sidebar } from "./sidebar";
import { TopBar } from "./top-bar";

function ChatApp() {
  const chat = useChat();
  const chrome = useChrome();
  const { sessionParam, setSession } = useUrlSession();
  const isLg = useMedia("(min-width: 1024px)");
  const [providers, setProviders] = useState<Record<string, string[]>>({});
  const [config, setConfig] = useState<AgentConfig | null>(null);
  const [chatModelOverride, setChatModelOverride] = useState<string | null>(null);
  const restored = useState({ current: false })[0];

  useEffect(() => {
    api.getProviders().then((p) => setProviders(p as Record<string, string[]>)).catch(() => {});
    api.getConfig().then((c) => setConfig(c as AgentConfig)).catch(() => {});
  }, []);

  const handleSelect = (id: string) => {
    setChatModelOverride(null);
    return chat.selectSession(id);
  };
  const handleNew = () => {
    setChatModelOverride(null);
    chat.newSession();
  };

  useEffect(() => {
    if (chat.sessionsLoading) return;
    if (restored.current) return;
    restored.current = true;
    if (sessionParam) {
      handleSelect(sessionParam).catch(() => handleNew());
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chat.sessionsLoading]);

  useEffect(() => {
    if (!restored.current) return;
    if (chat.activeSessionId === (sessionParam ?? null)) return;
    setSession(chat.activeSessionId || null, { replace: !chat.activeSessionId });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chat.activeSessionId, restored, sessionParam]);

  const wrapped = useMemo(
    () => ({
      ...chat,
      selectSession: handleSelect,
      newSession: handleNew,
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [chat],
  );

  const active = chat.sessions.find((s) => s.session_id === chat.activeSessionId);
  const title =
    active?.title ||
    (chat.turns[0]?.role === "user" ? chat.turns[0].content.slice(0, 60) : null);

  useEffect(() => {
    document.title = title ? `${title} · Astra` : "Astra";
  }, [title]);

  const effectiveModel = chatModelOverride || config?.model || "";
  const modelChoices = config ? providers[config.provider] || [] : [];

  return (
    <ChatContext.Provider value={wrapped}>
      <div className="relative flex h-dvh w-full overflow-hidden bg-bg text-fg">
        <div className="relative hidden h-full md:flex">
          <Sidebar collapsed={chrome.sidebarCollapsed} />
        </div>

        <Sheet open={chrome.mobileNavOpen} onOpenChange={chrome.setMobileNavOpen}>
          <SheetContent side="left" className="md:hidden">
            <Sidebar collapsed={false} />
          </SheetContent>
        </Sheet>

        <div className="relative flex min-w-0 flex-1 flex-col">
          <TopBar title={title} turns={chat.turns} />
          <div className="flex min-h-0 flex-1">
            <div className="flex min-w-0 flex-1 flex-col">
              <div className="flex min-h-0 flex-1 flex-col overflow-y-auto">
                <MessageStream
                  onStarter={(text) => chat.sendMessage(text, { model: chatModelOverride })}
                  onRegenerate={() => chat.regenerate(chatModelOverride)}
                />
              </div>
              {chat.error && (
                <div className="mx-auto mb-2 w-full max-w-3xl px-4">
                  <div className="rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-xs text-danger">
                    {chat.error}
                  </div>
                </div>
              )}
              <Composer
                onSend={(text) => chat.sendMessage(text, { model: chatModelOverride })}
                onAnswerClarification={chat.answerClarification}
                onStop={chat.stop}
                isStreaming={chat.isStreaming}
                clarification={chat.pendingClarification}
                model={effectiveModel}
                models={modelChoices}
                onModelChange={setChatModelOverride}
              />
            </div>
            {chrome.inspectorOpen && isLg && (
              <Inspector onClose={() => chrome.setInspectorOpen(false)} />
            )}
            <Sheet
              open={chrome.inspectorOpen && !isLg}
              onOpenChange={(o) => chrome.setInspectorOpen(o)}
            >
              <SheetContent side="right" className="lg:hidden">
                <Inspector onClose={() => chrome.setInspectorOpen(false)} />
              </SheetContent>
            </Sheet>
          </div>
        </div>

        <SettingsDialog config={config} onConfigSaved={setConfig} />
        <CommandPalette />
      </div>
    </ChatContext.Provider>
  );
}

export function AppShell() {
  return <ChatApp />;
}
