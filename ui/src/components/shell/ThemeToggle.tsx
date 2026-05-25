import { useComputed } from "@preact/signals";
import { themeMode, cycleThemeMode } from "../../lib/theme";
import "./theme-toggle.css";

const LABELS: Record<string, { icon: string; title: string }> = {
  dark:  { icon: "🌙", title: "Theme: dark (click for light)" },
  light: { icon: "☀️", title: "Theme: light (click for auto)" },
  auto:  { icon: "🌗", title: "Theme: auto (system) — click for dark" },
};

// Cycles dark → light → auto → dark. Persists in localStorage via theme.ts.
export function ThemeToggle() {
  const mode = useComputed(() => themeMode.value);
  const meta = LABELS[mode.value] || LABELS.dark;
  return (
    <button
      type="button"
      class="theme-toggle"
      onClick={() => cycleThemeMode()}
      title={meta.title}
      aria-label={meta.title}
    >
      <span class="theme-toggle-icon" aria-hidden="true">{meta.icon}</span>
      <span class="theme-toggle-label">{mode.value}</span>
    </button>
  );
}
