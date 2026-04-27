import { createContext, useContext } from "react";
import { BUILTIN_THEMES, defaultTheme } from "./presets";
import type { DashboardTheme } from "./types";

export interface ThemeContextValue {
  availableThemes: Array<{ description: string; label: string; name: string }>;
  setTheme: (name: string) => void;
  theme: DashboardTheme;
  themeName: string;
}

export const ThemeContext = createContext<ThemeContextValue>({
  theme: defaultTheme,
  themeName: "default",
  availableThemes: Object.values(BUILTIN_THEMES).map((t) => ({
    name: t.name,
    label: t.label,
    description: t.description,
  })),
  setTheme: () => {},
});

export function useTheme(): ThemeContextValue {
  return useContext(ThemeContext);
}
