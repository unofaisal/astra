import { useRef, useState } from "react";
import { ArrowUp, Square, Paperclip } from "lucide-react";

export default function Composer({ onSend, onStop, isStreaming, disabled }) {
  const [value, setValue] = useState("");
  const textareaRef = useRef(null);

  const autosize = (el) => {
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  };

  const submit = () => {
    const text = value.trim();
    if (!text || isStreaming) return;
    onSend(text);
    setValue("");
    if (textareaRef.current) textareaRef.current.style.height = "auto";
  };

  const onKeyDown = (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  };

  return (
    <div className="mx-auto w-full max-w-[760px] px-4 pb-4">
      <div className="flex items-end gap-2 rounded-lg border border-border bg-surface-1 p-2 focus-within:border-accent/60">
        <button
          type="button"
          title="Attach a file (not yet connected — see README)"
          disabled
          className="flex h-8 w-8 shrink-0 cursor-not-allowed items-center justify-center rounded-md text-text-tertiary/50"
        >
          <Paperclip size={16} />
        </button>

        <textarea
          ref={textareaRef}
          rows={1}
          value={value}
          disabled={disabled}
          onChange={(e) => {
            setValue(e.target.value);
            autosize(e.target);
          }}
          onKeyDown={onKeyDown}
          placeholder="Message astra…"
          className="max-h-[200px] flex-1 resize-none bg-transparent py-1.5 text-sm text-text-primary placeholder:text-text-tertiary focus:outline-none"
        />

        {isStreaming ? (
          <button
            onClick={onStop}
            title="Stop"
            className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-surface-3 text-text-primary hover:bg-border-strong"
          >
            <Square size={13} fill="currentColor" />
          </button>
        ) : (
          <button
            onClick={submit}
            disabled={!value.trim() || disabled}
            title="Send"
            className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-accent text-text-onaccent hover:bg-accent-hover disabled:opacity-30"
          >
            <ArrowUp size={15} />
          </button>
        )}
      </div>
      <p className="mt-1.5 text-center text-[11px] text-text-tertiary">
        Enter to send · Shift+Enter for a new line
      </p>
    </div>
  );
}
