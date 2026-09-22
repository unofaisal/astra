import { useEffect, useState } from "react";
import { X, Loader2, Check } from "lucide-react";
import { api } from "../lib/api";

export default function SettingsModal({ open, onClose, onSaved }) {
  const [providers, setProviders] = useState({});
  const [form, setForm] = useState(null);
  const [apiKeyInput, setApiKeyInput] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (!open) return;
    setLoading(true);
    setSaved(false);
    setError(null);
    Promise.all([api.getProviders(), api.getConfig()])
      .then(([providerList, config]) => {
        setProviders(providerList);
        setForm(config);
        setApiKeyInput("");
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, [open]);

  if (!open) return null;

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      const payload = {
        provider: form.provider,
        model: form.model,
        reasoning_effort: form.reasoning_effort || null,
        base_url_override: form.base_url_override || null,
      };
      if (apiKeyInput.trim()) payload.api_key = apiKeyInput.trim();
      const updated = await api.updateConfig(payload);
      setForm(updated);
      setApiKeyInput("");
      setSaved(true);
      onSaved?.(updated);
      setTimeout(() => setSaved(false), 2000);
    } catch (e) {
      setError(e.message);
    } finally {
      setSaving(false);
    }
  };

  const modelChoices = providers[form?.provider] || [];

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4" onClick={onClose}>
      <div
        onClick={(e) => e.stopPropagation()}
        className="w-full max-w-md rounded-lg border border-border bg-surface-1 shadow-xl"
      >
        <div className="flex items-center justify-between border-b border-border px-4 py-3">
          <h2 className="text-sm font-medium text-text-primary">Settings</h2>
          <button onClick={onClose} className="rounded p-1 text-text-tertiary hover:bg-surface-2 hover:text-text-primary">
            <X size={16} />
          </button>
        </div>

        <div className="max-h-[70vh] overflow-y-auto p-4">
          {loading && (
            <div className="flex items-center gap-2 py-6 text-sm text-text-tertiary">
              <Loader2 size={14} className="animate-spin" /> Loading…
            </div>
          )}

          {!loading && form && (
            <div className="space-y-4">
              <Field label="Provider">
                <select
                  value={form.provider}
                  onChange={(e) => setForm({ ...form, provider: e.target.value, model: providers[e.target.value]?.[0] || "" })}
                  className="w-full rounded-md border border-border bg-surface-2 px-2.5 py-1.5 text-sm text-text-primary focus:border-accent/60 focus:outline-none"
                >
                  {Object.keys(providers).map((p) => (
                    <option key={p} value={p}>
                      {p}
                    </option>
                  ))}
                </select>
              </Field>

              <Field label="Default model" hint="Used for any chat that doesn't pick its own model inline.">
                <select
                  value={form.model}
                  onChange={(e) => setForm({ ...form, model: e.target.value })}
                  className="w-full rounded-md border border-border bg-surface-2 px-2.5 py-1.5 text-sm text-text-primary focus:border-accent/60 focus:outline-none"
                >
                  {!modelChoices.includes(form.model) && <option value={form.model}>{form.model}</option>}
                  {modelChoices.map((m) => (
                    <option key={m} value={m}>
                      {m}
                    </option>
                  ))}
                </select>
              </Field>

              <Field label="API key" hint={form.api_key_set ? `Currently set (ends in ${form.api_key_suffix ?? "…"})` : "Not set"}>
                <input
                  type="password"
                  value={apiKeyInput}
                  onChange={(e) => setApiKeyInput(e.target.value)}
                  placeholder={form.api_key_set ? "Leave blank to keep current key" : "sk-…"}
                  className="w-full rounded-md border border-border bg-surface-2 px-2.5 py-1.5 text-sm text-text-primary placeholder:text-text-tertiary focus:border-accent/60 focus:outline-none"
                />
              </Field>

              <Field label="Reasoning effort" hint="Provider-dependent; leave blank for default.">
                <select
                  value={form.reasoning_effort || ""}
                  onChange={(e) => setForm({ ...form, reasoning_effort: e.target.value })}
                  className="w-full rounded-md border border-border bg-surface-2 px-2.5 py-1.5 text-sm text-text-primary focus:border-accent/60 focus:outline-none"
                >
                  <option value="">Default</option>
                  <option value="low">Low</option>
                  <option value="medium">Medium</option>
                  <option value="high">High</option>
                </select>
              </Field>

              <Field label="Base URL override" hint="For self-hosted or proxy endpoints. Optional.">
                <input
                  value={form.base_url_override || ""}
                  onChange={(e) => setForm({ ...form, base_url_override: e.target.value })}
                  placeholder="https://…"
                  className="w-full rounded-md border border-border bg-surface-2 px-2.5 py-1.5 text-sm text-text-primary placeholder:text-text-tertiary focus:border-accent/60 focus:outline-none"
                />
              </Field>

              <p className="rounded-md border border-border/60 bg-surface-2/50 px-2.5 py-2 text-[11px] leading-relaxed text-text-tertiary">
                These settings apply process-wide (no per-user accounts yet — see README). Changes take effect on
                the next message, no restart needed.
              </p>

              {error && <p className="text-xs text-error">{error}</p>}
            </div>
          )}
        </div>

        <div className="flex items-center justify-end gap-2 border-t border-border px-4 py-3">
          <button onClick={onClose} className="rounded-md px-3 py-1.5 text-sm text-text-secondary hover:bg-surface-2">
            Cancel
          </button>
          <button
            onClick={save}
            disabled={saving || loading}
            className="flex items-center gap-1.5 rounded-md bg-accent px-3 py-1.5 text-sm text-text-onaccent hover:bg-accent-hover disabled:opacity-50"
          >
            {saving && <Loader2 size={13} className="animate-spin" />}
            {saved && <Check size={13} />}
            {saving ? "Saving…" : saved ? "Saved" : "Save"}
          </button>
        </div>
      </div>
    </div>
  );
}

function Field({ label, hint, children }) {
  return (
    <div>
      <label className="mb-1 block text-xs font-medium text-text-secondary">{label}</label>
      {children}
      {hint && <p className="mt-1 text-[11px] text-text-tertiary">{hint}</p>}
    </div>
  );
}
