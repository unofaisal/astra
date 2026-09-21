import * as React from "react";
import { cn } from "@/lib/utils";

export function Input({ className, ...props }: React.ComponentProps<"input">) {
  return (
    <input
      className={cn(
        "h-10 w-full rounded-md border border-border bg-elevated px-3 text-sm text-fg placeholder:text-subtle transition-colors duration-(--motion-quick) focus:border-ring/50 focus:outline-none focus:ring-2 focus:ring-ring/30 disabled:opacity-50",
        className,
      )}
      {...props}
    />
  );
}
