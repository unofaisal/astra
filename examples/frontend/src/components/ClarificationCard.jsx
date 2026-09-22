import { useState } from "react";
import { HelpCircle, Send } from "lucide-react";

export default function ClarificationCard({ clarification, onAnswer, disabled }) {
  const [freeText, setFreeText] = useState("");
  if (!clarification) return null;
  const { question, options, allowFreeText } = clarification;

  return (
    <div className="my-2 rounded-md border border-attention/40 bg-attention/10 p-3">
      <div className="mb-2 flex items-start gap-2">
        <HelpCircle size={16} className="mt-0.5 shrink-0 text-attention" />
        <p className="text-sm text-text-primary">{question}</p>
      </div>

      {options?.length > 0 && (
        <div className="mb-2 flex flex-wrap gap-1.5">
          {options.map((opt) => (
            <button
              key={opt}
              disabled={disabled}
              onClick={() => onAnswer(opt)}
              className="rounded-md border border-attention/50 bg-surface-2 px-3 py-1 text-sm text-text-primary hover:bg-attention/20 disabled:opacity-50"
            >
              {opt}
            </button>
          ))}
        </div>
      )}

      {(allowFreeText || !options?.length) && (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (freeText.trim() && !disabled) {
              onAnswer(freeText.trim());
              setFreeText("");
            }
          }}
          className="flex gap-1.5"
        >
          <input
            value={freeText}
            onChange={(e) => setFreeText(e.target.value)}
            disabled={disabled}
            placeholder="Type your answer…"
            className="flex-1 rounded-md border border-border bg-surface-2 px-2.5 py-1.5 text-sm text-text-primary placeholder:text-text-tertiary focus:border-accent focus:outline-none disabled:opacity-50"
          />
          <button
            type="submit"
            disabled={disabled || !freeText.trim()}
            className="flex items-center justify-center rounded-md bg-accent px-2.5 text-text-onaccent hover:bg-accent-hover disabled:opacity-40"
          >
            <Send size={14} />
          </button>
        </form>
      )}
    </div>
  );
}
