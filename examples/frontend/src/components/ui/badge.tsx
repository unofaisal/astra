import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

export function Badge({
  className,
  tone = "neutral",
  children,
}: {
  className?: string;
  tone?: "neutral" | "accent" | "attention" | "success" | "danger";
  children: ReactNode;
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-sm px-1.5 py-0.5 font-mono text-2xs",
        tone === "neutral" && "bg-hover text-muted",
        tone === "accent" && "bg-accent/15 text-accent",
        tone === "attention" && "bg-attention/15 text-attention",
        tone === "success" && "bg-success/15 text-success",
        tone === "danger" && "bg-danger/15 text-danger",
        className,
      )}
    >
      {children}
    </span>
  );
}
