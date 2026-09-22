import { Check, Loader2, X, HelpCircle, Clock } from "lucide-react";

// Status is always icon + color + (usually) text together — never color
// alone — for accessibility and because a single amber dot doesn't mean
// anything on its own.
const CONFIG = {
  running: { Icon: Loader2, className: "text-accent animate-spin", label: "Running" },
  success: { Icon: Check, className: "text-success", label: "Done" },
  error: { Icon: X, className: "text-error", label: "Error" },
  pending: { Icon: Clock, className: "text-text-tertiary", label: "Pending" },
  awaiting_clarification: { Icon: HelpCircle, className: "text-attention", label: "Needs input" },
  cancelled: { Icon: X, className: "text-text-tertiary", label: "Cancelled" },
};

export default function StatusIcon({ status, size = 14 }) {
  const cfg = CONFIG[status] || CONFIG.pending;
  const { Icon } = cfg;
  return <Icon size={size} className={cfg.className} aria-label={cfg.label} />;
}

export function statusLabel(status) {
  return (CONFIG[status] || CONFIG.pending).label;
}
