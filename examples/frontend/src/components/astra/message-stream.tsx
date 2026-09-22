import { useEffect, useMemo, useRef } from "react";
import { useChatContext } from "@/lib/chat/use-chat";
import { groupTurnsForDisplay } from "@/lib/chat/transform";
import { EmptyState } from "./empty-state";
import { Message } from "./message";

export function MessageStream({
  onStarter,
  onRegenerate,
}: {
  onStarter: (text: string) => void;
  onRegenerate: () => void;
}) {
  const chat = useChatContext();
  const bottomRef = useRef<HTMLDivElement>(null);

  // Consecutive assistant-role turns (multi-round tool-calling, or a
  // clarification pause immediately followed by its resume) are merged
  // into one display group here — see groupTurnsForDisplay's docstring.
  // This runs identically whether chat.turns came from live streaming
  // or a reloaded session, so the chain reads the same way either way.
  const groups = useMemo(() => groupTurnsForDisplay(chat.turns), [chat.turns]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [groups]);

  if (groups.length === 0) {
    return <EmptyState onPick={onStarter} />;
  }

  const lastAssistantIdx = [...groups].reverse().findIndex((t) => t.role === "assistant");
  const lastAssistantRealIdx = lastAssistantIdx === -1 ? -1 : groups.length - 1 - lastAssistantIdx;

  return (
    <div className="mx-auto w-full max-w-3xl flex-1 space-y-6 px-4 py-8">
      {groups.map((turn, i) => (
        <Message
          key={turn.id}
          turn={turn}
          isLastAssistant={i === lastAssistantRealIdx && !chat.isStreaming}
          onRegenerate={onRegenerate}
        />
      ))}
      <div ref={bottomRef} />
    </div>
  );
}
