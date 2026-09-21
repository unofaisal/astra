import { create } from "zustand";

export type SettingsTab = "general" | "usage" | "appearance";

type ChromeState = {
  sidebarCollapsed: boolean;
  mobileNavOpen: boolean;
  inspectorOpen: boolean;
  settingsOpen: boolean;
  settingsTab: SettingsTab;
  commandOpen: boolean;
  theme: "dark" | "light";
  setSidebarCollapsed: (v: boolean) => void;
  toggleSidebar: () => void;
  setMobileNavOpen: (v: boolean) => void;
  setInspectorOpen: (v: boolean) => void;
  toggleInspector: () => void;
  openSettings: (tab?: SettingsTab) => void;
  closeSettings: () => void;
  setCommandOpen: (v: boolean) => void;
  setTheme: (t: "dark" | "light") => void;
  toggleTheme: () => void;
};

function readTheme(): "dark" | "light" {
  if (typeof window === "undefined") return "dark";
  const stored = localStorage.getItem("astra:theme");
  return stored === "light" ? "light" : "dark";
}

function applyTheme(theme: "dark" | "light") {
  if (typeof document === "undefined") return;
  document.documentElement.dataset.theme = theme;
}

export const useChrome = create<ChromeState>((set, get) => ({
  sidebarCollapsed:
    typeof window !== "undefined" && localStorage.getItem("astra:sidebarCollapsed") === "1",
  mobileNavOpen: false,
  inspectorOpen: false,
  settingsOpen: false,
  settingsTab: "general",
  commandOpen: false,
  theme: readTheme(),
  setSidebarCollapsed: (v) => {
    localStorage.setItem("astra:sidebarCollapsed", v ? "1" : "0");
    set({ sidebarCollapsed: v });
  },
  toggleSidebar: () => {
    const next = !get().sidebarCollapsed;
    localStorage.setItem("astra:sidebarCollapsed", next ? "1" : "0");
    set({ sidebarCollapsed: next });
  },
  setMobileNavOpen: (v) => set({ mobileNavOpen: v }),
  setInspectorOpen: (v) => set({ inspectorOpen: v }),
  toggleInspector: () => set({ inspectorOpen: !get().inspectorOpen }),
  openSettings: (tab = "general") => set({ settingsOpen: true, settingsTab: tab, commandOpen: false }),
  closeSettings: () => set({ settingsOpen: false }),
  setCommandOpen: (v) => set({ commandOpen: v }),
  setTheme: (theme) => {
    localStorage.setItem("astra:theme", theme);
    applyTheme(theme);
    set({ theme });
  },
  toggleTheme: () => {
    const theme = get().theme === "dark" ? "light" : "dark";
    localStorage.setItem("astra:theme", theme);
    applyTheme(theme);
    set({ theme });
  },
}));

if (typeof window !== "undefined") {
  applyTheme(readTheme());
}
