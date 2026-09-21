import * as React from "react";
import { cn } from "@/lib/utils";

export function IconButton({
  className,
  active,
  label,
  size = "md",
  ...props
}: React.ComponentProps<"button"> & {
  active?: boolean;
  label: string;
  size?: "sm" | "md";
}) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      className={cn(
        "inline-flex items-center justify-center rounded-md text-muted transition-colors duration-(--motion-quick) hover:bg-hover hover:text-fg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/70 disabled:cursor-not-allowed disabled:opacity-35",
        size === "md" ? "size-10" : "size-8",
        active && "bg-hover text-fg",
        className,
      )}
      {...props}
    />
  );
}
