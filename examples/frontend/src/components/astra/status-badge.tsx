import { Check, Clock, HelpCircle, Loader2, X } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import type { ToolStatus } from "@/lib/chat/types";

const CONFIG: Record<
  ToolStatus,
  { Icon: typeof Check; tone: "neutral" | "accent" | "attention" | "success" | "danger"; label: string; spin?: boolean }
> = {
  running: { Icon: Loader2, tone: "accent", label: "Running", spin: true },
  success: { Icon: Check, tone: "success", label: "Done" },
  error: { Icon: X, tone: "danger", label: "Error" },
  pending: { Icon: Clock, tone: "neutral", label: "Pending" },
  awaiting_clarification: { Icon: HelpCircle, tone: "attention", label: "Needs input" },
  cancelled: { Icon: X, tone: "neutral", label: "Cancelled" },
};

export function StatusBadge({ status }: { status: ToolStatus }) {
  const cfg = CONFIG[status] || CONFIG.pending;
  const { Icon } = cfg;
  return (
    <Badge tone={cfg.tone}>
      <Icon size={10} className={cfg.spin ? "animate-spin" : undefined} />
      {cfg.label}
    </Badge>
  );
}

export function StatusIcon({ status, size = 14 }: { status: ToolStatus; size?: number }) {
  const cfg = CONFIG[status] || CONFIG.pending;
  const { Icon } = cfg;
  const color =
    cfg.tone === "accent"
      ? "text-accent"
      : cfg.tone === "success"
        ? "text-success"
        : cfg.tone === "danger"
          ? "text-danger"
          : cfg.tone === "attention"
            ? "text-attention"
            : "text-subtle";
  return <Icon size={size} className={`${color} ${cfg.spin ? "animate-spin" : ""}`} aria-label={cfg.label} />;
}
