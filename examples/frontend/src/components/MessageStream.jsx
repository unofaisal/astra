import { useEffect, useRef } from "react";
import Message from "./Message";
import ClarificationCard from "./ClarificationCard";
import EmptyState from "./EmptyState";

export default function MessageStream({ turns, pendingClarification, onAnswerClarification, onRegenerate, isStreaming, onStarterPrompt }) {
  const bottomRef = useRef(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [turns, pendingClarification]);

  if (turns.length === 0) {
    return <EmptyState onPick={onStarterPrompt} />;
  }

  const lastAssistantIdx = [...turns].reverse().findIndex((t) => t.role === "assistant");
  const lastAssistantRealIdx = lastAssistantIdx === -1 ? -1 : turns.length - 1 - lastAssistantIdx;

  return (
    <div className="mx-auto w-full max-w-[760px] flex-1 space-y-5 px-4 py-6">
      {turns.map((turn, i) => (
        <Message key={turn.id} turn={turn} isLastAssistant={i === lastAssistantRealIdx && !isStreaming} onRegenerate={onRegenerate} />
      ))}
      {pendingClarification && (
        <ClarificationCard clarification={pendingClarification} onAnswer={onAnswerClarification} disabled={isStreaming} />
      )}
      <div ref={bottomRef} />
    </div>
  );
}
