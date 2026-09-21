import type { ReactNode } from "react";
import * as DialogPrimitive from "@radix-ui/react-dialog";
import { cn } from "@/lib/utils";

export const Sheet = DialogPrimitive.Root;
export const SheetTrigger = DialogPrimitive.Trigger;
export const SheetClose = DialogPrimitive.Close;

export function SheetContent({
  side = "left",
  className,
  children,
}: {
  side?: "left" | "right";
  className?: string;
  children: ReactNode;
}) {
  return (
    <DialogPrimitive.Portal>
      <DialogPrimitive.Overlay className="fixed inset-0 z-50 bg-bg/70 animate-fade md:hidden" />
      <DialogPrimitive.Content
        className={cn(
          "fixed z-50 flex h-full flex-col border-border bg-surface shadow-panel",
          side === "left" ? "inset-y-0 left-0 w-72 border-r" : "inset-y-0 right-0 w-80 border-l",
          className,
        )}
      >
        {children}
      </DialogPrimitive.Content>
    </DialogPrimitive.Portal>
  );
}
