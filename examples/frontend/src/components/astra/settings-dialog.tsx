import { useEffect, useMemo, useState, type ReactNode } from "react";
import { BarChart3, Check, Loader2, Palette, SlidersHorizontal } from "lucide-react";
import { toast } from "sonner";
import { api } from "@/lib/chat/api";
import { useChrome, type SettingsTab } from "@/lib/chat/chrome";
import type { AgentConfig } from "@/lib/chat/types";
import { useChatContext } from "@/lib/chat/use-chat";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <div>
      <label className="mb-1.5 block text-xs font-medium text-muted">{label}</label>
      {children}
      {hint && <p className="mt-1 text-2xs leading-relaxed text-subtle">{hint}</p>}
    </div>
  );
}

function Select({
  value,
  onChange,
  children,
}: {
  value: string;
  onChange: (v: string) => void;
  children: ReactNode;
}) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className="h-10 w-full rounded-md border border-border bg-elevated px-3 text-sm text-fg focus:border-ring/50 focus:outline-none focus:ring-2 focus:ring-ring/30"
    >
      {children}
    </select>
  );
}

function GeneralTab({ onSaved }: { onSaved: (c: AgentConfig) => void }) {
  const [providers, setProviders] = useState<Record<string, string[]>>({});
  const [form, setForm] = useState<AgentConfig | null>(null);
  const [apiKeyInput, setApiKeyInput] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    Promise.all([api.getProviders(), api.getConfig()])
      .then(([providerList, config]) => {
        setProviders(providerList as Record<string, string[]>);
        setForm(config as AgentConfig);
      })
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  const save = async () => {
    if (!form) return;
    setSaving(true);
    setError(null);
    try {
      const payload: Record<string, unknown> = {
        provider: form.provider,
        model: form.model,
        reasoning_effort: form.reasoning_effort || null,
        base_url_override: form.base_url_override || null,
      };
      if (apiKeyInput.trim()) payload.api_key = apiKeyInput.trim();
      const updated = (await api.updateConfig(payload)) as AgentConfig;
      setForm(updated);
      setApiKeyInput("");
      setSaved(true);
      onSaved(updated);
      toast.success("Config saved");
      setTimeout(() => setSaved(false), 1600);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center gap-2 py-8 text-sm text-subtle">
        <Loader2 size={14} className="animate-spin" /> Loading…
      </div>
    );
  }
  if (!form) return null;

  const modelChoices = providers[form.provider] || [];

  return (
    <div className="max-w-lg space-y-5">
      <Field label="Provider">
        <Select
          value={form.provider}
          onChange={(provider) =>
            setForm({ ...form, provider, model: providers[provider]?.[0] || form.model })
          }
        >
          {Object.keys(providers).map((p) => (
            <option key={p} value={p}>
              {p}
            </option>
          ))}
        </Select>
      </Field>
      <Field label="Default model" hint="Used when a chat does not pick its own model.">
        <Select value={form.model} onChange={(model) => setForm({ ...form, model })}>
          {!modelChoices.includes(form.model) && <option value={form.model}>{form.model}</option>}
          {modelChoices.map((m) => (
            <option key={m} value={m}>
              {m}
            </option>
          ))}
        </Select>
      </Field>
      <Field
        label="API key"
        hint={
          form.api_key_set
            ? `Currently set (ends in ${form.api_key_suffix ?? "…"})`
            : "Not set"
        }
      >
        <Input
          type="password"
          value={apiKeyInput}
          onChange={(e) => setApiKeyInput(e.target.value)}
          placeholder={form.api_key_set ? "Leave blank to keep current key" : "sk-…"}
        />
      </Field>
      <Field label="Reasoning effort" hint="Provider-dependent. Leave default if unsure.">
        <Select
          value={form.reasoning_effort || ""}
          onChange={(reasoning_effort) => setForm({ ...form, reasoning_effort })}
        >
          <option value="">Default</option>
          <option value="low">Low</option>
          <option value="medium">Medium</option>
          <option value="high">High</option>
        </Select>
      </Field>
      <Field label="Base URL override" hint="For a self-hosted or proxy endpoint.">
        <Input
          value={form.base_url_override || ""}
          onChange={(e) => setForm({ ...form, base_url_override: e.target.value })}
          placeholder="https://…"
        />
      </Field>
      {error && <p className="text-xs text-danger">{error}</p>}
      <Button onClick={save} disabled={saving}>
        {saving && <Loader2 size={14} className="animate-spin" />}
        {saved && <Check size={14} />}
        {saving ? "Saving…" : saved ? "Saved" : "Save changes"}
      </Button>
    </div>
  );
}

function UsageTab() {
  const { sessions } = useChatContext();
  const totals = useMemo(
    () =>
      sessions.reduce(
        (acc, s) => ({
          input: acc.input + (s.total_input_tokens || 0),
          output: acc.output + (s.total_output_tokens || 0),
          cost: acc.cost + (s.estimated_cost || 0),
          turns: acc.turns + (s.turn_count || 0),
        }),
        { input: 0, output: 0, cost: 0, turns: 0 },
      ),
    [sessions],
  );
  const sorted = useMemo(
    () =>
      [...sessions].sort(
        (a, b) =>
          (b.total_input_tokens || 0) +
          (b.total_output_tokens || 0) -
          ((a.total_input_tokens || 0) + (a.total_output_tokens || 0)),
      ),
    [sessions],
  );

  return (
    <div className="space-y-5">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <Stat label="Conversations" value={sessions.length} />
        <Stat label="Turns" value={totals.turns} />
        <Stat
          label="Tokens"
          value={`${totals.input.toLocaleString()} / ${totals.output.toLocaleString()}`}
        />
        <Stat label="Est. cost" value={`$${totals.cost.toFixed(4)}`} />
      </div>
      <p className="text-2xs leading-relaxed text-subtle">
        Computed from sessions currently loaded. Cost stays $0 until the runtime is given a pricing
        lookup.
      </p>
      <div className="overflow-hidden rounded-md border border-border">
        <table className="w-full text-left text-sm">
          <thead>
            <tr className="border-b border-border bg-elevated font-mono text-2xs uppercase tracking-wider text-subtle">
              <th className="px-3 py-2 font-medium">Title</th>
              <th className="px-3 py-2 font-medium">Turns</th>
              <th className="px-3 py-2 font-medium">Tokens</th>
              <th className="px-3 py-2 font-medium">Cost</th>
            </tr>
          </thead>
          <tbody>
            {sorted.slice(0, 25).map((s) => (
              <tr key={s.session_id} className="border-b border-border last:border-0">
                <td className="max-w-56 truncate px-3 py-2 text-muted">{s.title || "Untitled"}</td>
                <td className="px-3 py-2 font-mono text-xs text-subtle tabular-nums">
                  {s.turn_count ?? 0}
                </td>
                <td className="px-3 py-2 font-mono text-xs text-subtle tabular-nums">
                  {(s.total_input_tokens || 0) + (s.total_output_tokens || 0)}
                </td>
                <td className="px-3 py-2 font-mono text-xs text-subtle tabular-nums">
                  ${(s.estimated_cost || 0).toFixed(4)}
                </td>
              </tr>
            ))}
            {sorted.length === 0 && (
              <tr>
                <td colSpan={4} className="px-3 py-6 text-center text-xs text-subtle">
                  No conversations yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="rounded-md border border-border bg-elevated px-3 py-2.5">
      <div className="font-mono text-2xs uppercase tracking-wider text-subtle">{label}</div>
      <div className="mt-0.5 font-mono text-lg text-fg tabular-nums">{value}</div>
    </div>
  );
}

function AppearanceTab() {
  const { theme, setTheme } = useChrome();
  return (
    <div className="space-y-4">
      <p className="text-sm text-muted">Warm graphite in the dark. Bone paper in the light.</p>
      <div className="grid grid-cols-2 gap-2">
        {(["dark", "light"] as const).map((t) => (
          <button
            key={t}
            type="button"
            onClick={() => setTheme(t)}
            className={`rounded-lg border p-4 text-left ${
              theme === t ? "border-accent bg-elevated" : "border-border bg-surface hover:bg-elevated"
            }`}
          >
            <div className={`mb-3 h-16 overflow-hidden rounded-md border border-border`}>
              <div className={`flex h-full ${t === "dark" ? "theme-preview-dark" : "theme-preview-light"}`}>
                <div className={`w-8 ${t === "dark" ? "theme-preview-dark-rail" : "theme-preview-light-rail"}`} />
                <div className="flex-1 p-2">
                  <div
                    className={`mb-1 h-1.5 w-12 rounded-full ${t === "dark" ? "theme-preview-dark-bar" : "theme-preview-light-bar"}`}
                  />
                  <div
                    className={`h-1.5 w-20 rounded-full ${t === "dark" ? "theme-preview-dark-line" : "theme-preview-light-line"}`}
                  />
                </div>
              </div>
            </div>
            <div className="text-sm capitalize text-fg">{t}</div>
          </button>
        ))}
      </div>
    </div>
  );
}

const NAV: { id: SettingsTab; label: string; icon: typeof SlidersHorizontal }[] = [
  { id: "general", label: "General", icon: SlidersHorizontal },
  { id: "usage", label: "Usage", icon: BarChart3 },
  { id: "appearance", label: "Appearance", icon: Palette },
];

export function SettingsDialog({
  config,
  onConfigSaved,
}: {
  config: AgentConfig | null;
  onConfigSaved: (c: AgentConfig) => void;
}) {
  const chrome = useChrome();
  void config;

  return (
    <Dialog open={chrome.settingsOpen} onOpenChange={(o) => (o ? chrome.openSettings() : chrome.closeSettings())}>
      <DialogContent
        title="Settings"
        description="Runtime defaults, usage, and the surface."
        wide
      >
        <div className="flex min-h-0 flex-1 flex-col overflow-hidden md:flex-row">
          <nav className="flex gap-1 overflow-x-auto border-b border-border p-2 md:w-44 md:flex-col md:border-r md:border-b-0 md:p-3">
            {NAV.map((item) => (
              <button
                key={item.id}
                type="button"
                onClick={() => chrome.openSettings(item.id)}
                className={`flex items-center gap-2 rounded-md px-3 py-2 text-sm ${
                  chrome.settingsTab === item.id
                    ? "bg-hover text-fg"
                    : "text-muted hover:bg-elevated hover:text-fg"
                }`}
              >
                <item.icon size={15} />
                {item.label}
              </button>
            ))}
          </nav>
          <div className="min-h-0 flex-1 overflow-y-auto p-5">
            {chrome.settingsTab === "general" && (
              <GeneralTab onSaved={onConfigSaved} />
            )}
            {chrome.settingsTab === "usage" && <UsageTab />}
            {chrome.settingsTab === "appearance" && <AppearanceTab />}
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
