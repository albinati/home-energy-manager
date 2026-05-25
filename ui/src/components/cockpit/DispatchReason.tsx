import type { CockpitNow } from "../../lib/types";
import { slotKindLabel, slotKindColorVar, hhmm, kw, pct } from "../../lib/format";
import "./dispatch-reason.css";

interface DispatchReasonProps {
  now: CockpitNow;
  decisionReason?: string | null;
}

// Card answer to "what's HEM doing right now and why". Leads with a big
// action statement inferred from the live grid/battery/solar flows, then
// surfaces the current slot's tariff context and the LP's reasoning.
export function DispatchReason({ now, decisionReason }: DispatchReasonProps) {
  const action = inferAction(now);
  const slot = now.current_slot;
  const kind = slot.kind || inferKind(slot.price_import_p, now.thresholds?.cheap_p ?? 12, now.thresholds?.peak_p ?? 28);
  const kindColor = slotKindColorVar(kind);

  return (
    <div class="rightnow">
      <div class="rightnow-headline">
        <span class="rightnow-icon" style={{ background: action.color, boxShadow: `0 0 14px ${action.color}55` }}>
          {action.icon}
        </span>
        <div>
          <div class="rightnow-title" style={{ color: action.color }}>{action.title}</div>
          <div class="rightnow-sub">{action.sub}</div>
        </div>
      </div>

      <div class="rightnow-meta">
        <MetaChip label={slotKindLabel(kind)} color={kindColor} icon="●" />
      </div>

      {decisionReason && (
        <div class="rightnow-reason">
          <span class="rightnow-reason-label">Why</span>
          <span class="rightnow-reason-text">{decisionReason}</span>
        </div>
      )}

      {now.next_transition && (
        <div class="rightnow-next">
          <span class="rightnow-next-label">Next change</span>
          <span class="rightnow-next-when">{hhmm(now.next_transition.t_utc)}</span>
          <span class="rightnow-next-mode">→ {now.next_transition.new_fox_mode}</span>
        </div>
      )}
    </div>
  );
}

function MetaChip({ label, color, icon }: { label: string; color?: string; icon?: string }) {
  return (
    <span class="rightnow-chip" style={color ? { color, borderColor: color + "55", background: color + "12" } : undefined}>
      {icon && <span class="rightnow-chip-icon">{icon}</span>}
      {label}
    </span>
  );
}

interface Action {
  title: string;
  sub: string;
  icon: string;
  color: string;
}

// Infer the dominant action from the live flows. Prefers the most
// economically relevant flow when several are non-zero.
function inferAction(now: CockpitNow): Action {
  const s = now.state;
  const grid = s.grid_kw;       // + import, - export
  const batt = s.battery_kw;    // + charge, - discharge
  const solar = s.solar_kw;
  const E = 0.1;                 // kW threshold

  const importing = grid > E;
  const exporting = grid < -E;
  const charging = batt > E;
  const discharging = batt < -E;
  const producing = solar > E;

  // Discharge + export = peak export
  if (discharging && exporting) {
    return {
      title: "Exporting from battery",
      sub: `${kw(-batt + Math.max(0, solar))} flowing to the grid`,
      icon: "⚡",
      color: "var(--peak-export)",
    };
  }
  // Pure solar export (battery either full or holding)
  if (exporting && !discharging) {
    return {
      title: "Exporting solar surplus",
      sub: `${kw(-grid)} to grid · ${kw(s.load_kw)} house · ${kw(solar)} solar`,
      icon: "☀",
      color: "var(--export)",
    };
  }
  // Charging from grid (cheap import)
  if (charging && importing) {
    return {
      title: "Charging from grid",
      sub: `${kw(grid)} import · battery climbing at ${kw(batt)}`,
      icon: "⚡",
      color: "var(--cheap)",
    };
  }
  // Charging from solar
  if (charging && producing) {
    return {
      title: "Charging from solar",
      sub: `${kw(solar)} solar · battery climbing at ${kw(batt)}`,
      icon: "⚡",
      color: "var(--pv)",
    };
  }
  // Discharging to house (battery covering load)
  if (discharging) {
    return {
      title: "Battery → house",
      sub: `${kw(-batt)} from battery · ${kw(s.load_kw)} house load`,
      icon: "🔋",
      color: "var(--warn)",
    };
  }
  // Importing (no battery action)
  if (importing) {
    return {
      title: "Importing from grid",
      sub: `${kw(grid)} import · ${kw(s.load_kw)} house · battery holding`,
      icon: "⬇",
      color: "var(--import)",
    };
  }
  // Self-use only
  if (producing) {
    return {
      title: "Self-using solar",
      sub: `${kw(solar)} solar covering ${kw(s.load_kw)} house`,
      icon: "☀",
      color: "var(--pv)",
    };
  }
  // Idle
  return {
    title: "Holding",
    sub: `${kw(s.load_kw)} house · battery ${pct(s.soc_pct, 0)} · waiting`,
    icon: "•",
    color: "var(--text-mute)",
  };
}

function inferKind(p: number, cheap: number, peak: number): string {
  if (p < 0) return "negative";
  if (p < cheap) return "cheap";
  if (p >= peak) return "peak";
  return "standard";
}
