import { useComputed } from "@preact/signals";
import { themeMode, cycleThemeMode } from "../../lib/theme";
import { Icon, type IconName } from "../common/Icon";
import "./theme-toggle.css";

const LABELS: Record<string, { icon: IconName; title: string }> = {
  dark:  { icon: "moon",  title: "Theme: dark (click for light)" },
  light: { icon: "solar", title: "Theme: light (click for auto)" },
  auto:  { icon: "moon",  title: "Theme: auto (system) — click for dark" },
};

// Cycles dark → light → auto → dark. Persists in localStorage via theme.ts.
// Chrome icon-btn form (redesign): a quiet 32px square, thin-line icon only —
// auto mode shows the moon dimmed + an "A" corner mark.
export function ThemeToggle() {
  const mode = useComputed(() => themeMode.value);
  const meta = LABELS[mode.value] || LABELS.dark;
  return (
    <button
      type="button"
      class={`icon-btn theme-toggle${mode.value === "auto" ? " theme-toggle--auto" : ""}`}
      onClick={() => cycleThemeMode()}
      title={meta.title}
      aria-label={meta.title}
    >
      <Icon name={meta.icon} size={16} />
      {mode.value === "auto" && <span class="theme-toggle-auto" aria-hidden="true">A</span>}
    </button>
  );
}
