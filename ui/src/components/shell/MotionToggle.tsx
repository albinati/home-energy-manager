import { useState } from "preact/hooks";
import { motionPref, setMotionPref, type MotionPref } from "../../lib/motion";
import "./theme-toggle.css";

const NEXT: Record<MotionPref, MotionPref> = { on: "auto", auto: "off", off: "on" };
const META: Record<MotionPref, { icon: string; title: string }> = {
  on:   { icon: "✨", title: "Motion: on (click for auto/system)" },
  auto: { icon: "🌀", title: "Motion: auto — follows your OS Reduce-Motion (click to turn off)" },
  off:  { icon: "⏸", title: "Motion: off (click to turn on)" },
};

// Cycles on → auto → off. Default is "on" (overrides OS Reduce-Motion) since the
// signature power-flow/hero/chart animations are the point of this dashboard.
// Reload on change so every module-load motion gate re-evaluates.
export function MotionToggle() {
  const [p, setP] = useState<MotionPref>(motionPref());
  const meta = META[p];
  return (
    <button
      type="button"
      class="theme-toggle"
      title={meta.title}
      aria-label={meta.title}
      onClick={() => {
        const n = NEXT[p];
        setMotionPref(n);
        setP(n);
        location.reload();
      }}
    >
      <span class="theme-toggle-icon" aria-hidden="true">{meta.icon}</span>
      <span class="theme-toggle-label">{p}</span>
    </button>
  );
}
