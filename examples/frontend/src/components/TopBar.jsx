import { useEffect, useRef, useState } from "react";
import { ChevronDown, Download, PanelRight, Settings, Share2, Paperclip } from "lucide-react";

function ModelPicker({ providers, provider, model, onChange }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  const choices = providers[provider] || [];

  useEffect(() => {
    const onClick = (e) => {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false);
    };
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, []);

  return (
    <div className="relative" ref={ref}>
      <button
        onClick={() => setOpen((o) => !o)}
        className="flex items-center gap-1 rounded-md px-2 py-1 text-xs text-text-secondary hover:bg-surface-2"
      >
        <span className="font-mono">{model}</span>
        <ChevronDown size={12} />
      </button>
      {open && (
        <div className="absolute left-0 top-full z-20 mt-1 w-56 rounded-md border border-border bg-surface-1 py-1 shadow-lg">
          <div className="px-2.5 py-1 text-[10px] font-medium uppercase tracking-wide text-text-tertiary">
            This chat only — see Settings for the default
          </div>
          {choices.length === 0 && <div className="px-2.5 py-1.5 text-xs text-text-tertiary">No models listed for this provider</div>}
          {choices.map((m) => (
            <button
              key={m}
              onClick={() => {
                onChange(m);
                setOpen(false);
              }}
              className={`flex w-full items-center px-2.5 py-1.5 text-left font-mono text-xs hover:bg-surface-2 ${
                m === model ? "text-accent" : "text-text-secondary"
              }`}
            >
              {m}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export default function TopBar({
  title,
  providers,
  provider,
  chatModel,
  onChatModelChange,
  onOpenSettings,
  onToggleInspector,
  inspectorOpen,
  onExport,
}) {
  const [exported, setExported] = useState(false);

  return (
    <div className="flex h-12 shrink-0 items-center gap-2 border-b border-border bg-surface-1 px-3">
      <span className="truncate text-sm text-text-primary">{title || "New chat"}</span>
      <ModelPicker providers={providers} provider={provider} model={chatModel} onChange={onChatModelChange} />

      <div className="ml-auto flex items-center gap-0.5">
        <button
          disabled
          title="Attach a file (not yet connected — see README)"
          className="flex h-7 w-7 cursor-not-allowed items-center justify-center rounded-md text-text-tertiary/50"
        >
          <Paperclip size={15} />
        </button>
        <button
          title={exported ? "Downloaded" : "Export conversation as Markdown"}
          onClick={() => {
            onExport?.();
            setExported(true);
            setTimeout(() => setExported(false), 1200);
          }}
          className="flex h-7 w-7 items-center justify-center rounded-md text-text-tertiary hover:bg-surface-2 hover:text-text-secondary"
        >
          <Download size={15} />
        </button>
        <button
          disabled
          title="Share (not yet connected — no public-link backend endpoint exists)"
          className="flex h-7 w-7 cursor-not-allowed items-center justify-center rounded-md text-text-tertiary/50"
        >
          <Share2 size={15} />
        </button>
        <button
          onClick={onToggleInspector}
          title="Run inspector"
          className={`flex h-7 w-7 items-center justify-center rounded-md hover:bg-surface-2 ${
            inspectorOpen ? "text-accent" : "text-text-tertiary hover:text-text-secondary"
          }`}
        >
          <PanelRight size={15} />
        </button>
        <button
          onClick={onOpenSettings}
          title="Settings"
          className="flex h-7 w-7 items-center justify-center rounded-md text-text-tertiary hover:bg-surface-2 hover:text-text-secondary"
        >
          <Settings size={15} />
        </button>
      </div>
    </div>
  );
}
