import type { ReactNode } from "react";
import * as DialogPrimitive from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import { cn } from "@/lib/utils";
import { IconButton } from "./icon-button";

export const Dialog = DialogPrimitive.Root;
export const DialogTrigger = DialogPrimitive.Trigger;
export const DialogClose = DialogPrimitive.Close;

export function DialogContent({
  className,
  children,
  title,
  description,
  wide,
}: {
  className?: string;
  children: ReactNode;
  title: string;
  description?: string;
  wide?: boolean;
}) {
  return (
    <DialogPrimitive.Portal>
      <DialogPrimitive.Overlay className="fixed inset-0 z-50 bg-bg/70 animate-fade" />
      <DialogPrimitive.Content
        className={cn(
          "fixed top-1/2 left-1/2 z-50 flex h-[min(85vh,620px)] w-[min(100%-1.5rem,720px)] -translate-x-1/2 -translate-y-1/2 flex-col overflow-hidden rounded-xl border border-border bg-surface shadow-panel animate-enter",
          wide && "w-[min(100%-1.5rem,880px)]",
          className,
        )}
      >
        <div className="flex items-start justify-between gap-4 border-b border-border px-6 py-5">
          <div>
            <DialogPrimitive.Title className="font-display text-xl tracking-tight text-fg">
              {title}
            </DialogPrimitive.Title>
            {description && (
              <DialogPrimitive.Description className="mt-1 text-sm text-muted">
                {description}
              </DialogPrimitive.Description>
            )}
          </div>
          <DialogPrimitive.Close asChild>
            <IconButton label="Close" size="sm">
              <X size={16} />
            </IconButton>
          </DialogPrimitive.Close>
        </div>
        {/* `flex` here is load-bearing, not decorative: settings-dialog's
            content div below relies on `flex-1 min-h-0` to size itself
            against a bounded height so its own `overflow-y-auto` can
            kick in. Without `display:flex` on this wrapper, flex-sizing
            props on the child are inert (they only apply within a flex
            container), so content just grows past the dialog's fixed
            height and gets hard-clipped by `overflow-hidden` instead of
            scrolling — that was the "settings won't scroll" bug. */}
        <div className="flex min-h-0 flex-1 flex-col overflow-hidden">{children}</div>
      </DialogPrimitive.Content>
    </DialogPrimitive.Portal>
  );
}
