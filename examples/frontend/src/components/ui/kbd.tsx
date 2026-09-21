import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

export function Kbd({ className, children }: { className?: string; children: ReactNode }) {
  return (
    <kbd
      className={cn(
        "inline-flex h-5 min-w-5 items-center justify-center rounded-xs border border-border bg-elevated px-1 font-mono text-2xs text-muted",
        className,
      )}
    >
      {children}
    </kbd>
  );
}
