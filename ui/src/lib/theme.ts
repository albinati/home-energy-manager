// Theme system — 3-mode toggle (dark / light / auto), persisted in localStorage,
// honours prefers-color-scheme when in "auto". Applies a class on <html>:
//   theme-dark / theme-light. Components don't need to subscribe; CSS picks
//   it up via the `:root.theme-light` ruleset in tokens.css.

import { signal, effect } from "@preact/signals-core";

export type ThemeMode = "dark" | "light" | "auto";

const STORAGE_KEY = "hem-theme";
const VALID: ThemeMode[] = ["dark", "light", "auto"];

function loadStored(): ThemeMode {
  if (typeof window === "undefined") return "auto";
  const raw = window.localStorage.getItem(STORAGE_KEY);
  return VALID.includes(raw as ThemeMode) ? (raw as ThemeMode) : "auto";
}

function prefersDark(): boolean {
  if (typeof window === "undefined") return true;
  return window.matchMedia("(prefers-color-scheme: dark)").matches;
}

export const themeMode = signal<ThemeMode>(loadStored());
// Resolved theme — actual applied dark/light after "auto" resolution.
export const resolvedTheme = signal<"dark" | "light">(
  themeMode.value === "auto" ? (prefersDark() ? "dark" : "light") : themeMode.value,
);

// Subscribe to system preference changes while in "auto".
if (typeof window !== "undefined") {
  const mq = window.matchMedia("(prefers-color-scheme: dark)");
  const onSystemChange = () => {
    if (themeMode.value === "auto") {
      resolvedTheme.value = mq.matches ? "dark" : "light";
    }
  };
  mq.addEventListener?.("change", onSystemChange);
}

// Apply theme class + persist mode whenever either changes.
effect(() => {
  if (typeof document === "undefined") return;
  const mode = themeMode.value;
  const resolved = mode === "auto" ? (prefersDark() ? "dark" : "light") : mode;
  resolvedTheme.value = resolved;

  document.documentElement.classList.toggle("theme-light", resolved === "light");
  document.documentElement.classList.toggle("theme-dark", resolved === "dark");

  try {
    window.localStorage.setItem(STORAGE_KEY, mode);
  } catch {
    // ignore
  }
});

export function setThemeMode(mode: ThemeMode) {
  themeMode.value = mode;
}

export function cycleThemeMode() {
  const order: ThemeMode[] = ["dark", "light", "auto"];
  const idx = order.indexOf(themeMode.value);
  themeMode.value = order[(idx + 1) % order.length];
}

// Hook that returns the current resolved theme (dark/light) and triggers a
// re-render on change. Use in chart components so they re-setOption when the
// theme flips — ECharts otherwise freezes the colours it computed at init.
import { useComputed } from "@preact/signals";

export function useResolvedTheme(): "dark" | "light" {
  return useComputed(() => resolvedTheme.value).value;
}
