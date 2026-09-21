import { Toaster } from "sonner";
import { AppShell } from "@/components/astra/app-shell";
import { TooltipProvider } from "@/components/ui/tooltip";

export function App() {
  return (
    <TooltipProvider>
      <AppShell />
      <Toaster
        theme="system"
        position="bottom-right"
        toastOptions={{
          className: "!bg-elevated !text-fg !border-border !shadow-panel !font-sans",
        }}
      />
    </TooltipProvider>
  );
}
