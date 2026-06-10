import {
  usePeriod,
  setGranularity,
  stepPeriod,
  isCurrentPeriod,
  periodLabel,
  type Granularity,
} from "../../lib/period";
import "./period-nav.css";

// Single shared period selector that drives the whole home dashboard:
//   ‹  [period label]  ›        day | week | month | year
// Stepping is bounded — "next" is disabled once the selected period contains
// today (you can't browse the future). Granularity is a segmented toggle.
//
// Two render variants share the same global period signal (redesign P4c):
//   • "chrome" — compact, lives inside the sticky TopNav on period-scoped
//     routes (cockpit, insights). Hidden on narrow screens where the topnav
//     collapses to a column (a 3-row sticky header would eat the viewport).
//   • "page"  — the in-flow selector at the top of the route; shown ONLY on
//     narrow screens, where it replaces the hidden chrome control.
const GRANS: { key: Granularity; label: string }[] = [
  { key: "day", label: "Day" },
  { key: "week", label: "Week" },
  { key: "month", label: "Month" },
  { key: "year", label: "Year" },
];

export function PeriodNavigator({ variant = "page" }: { variant?: "page" | "chrome" }) {
  const p = usePeriod();
  const atNow = isCurrentPeriod(p);

  return (
    <div class={`pnav pnav--${variant}`} role="group" aria-label="Period selector">
      <div class="pnav-stepper">
        <button class="pnav-arrow" onClick={() => stepPeriod(-1)} aria-label="Previous period">‹</button>
        <span class="pnav-label" aria-live="polite">{periodLabel(p)}</span>
        <button class="pnav-arrow" onClick={() => stepPeriod(1)} disabled={atNow}
                aria-label="Next period" title={atNow ? "Already at the current period" : undefined}>›</button>
      </div>
      <div class="pnav-grans" role="tablist" aria-label="Granularity">
        {GRANS.map((g) => (
          <button key={g.key}
                  class={`pnav-gran${p.gran === g.key ? " is-active" : ""}`}
                  onClick={() => setGranularity(g.key)}
                  role="tab" aria-selected={p.gran === g.key}>
            {g.label}
          </button>
        ))}
      </div>
    </div>
  );
}
