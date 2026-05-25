import { kw, kwh, pct } from "../../lib/format";
import type { CockpitState, SchedulerTimeline, ExecutionTodayResponse } from "../../lib/types";

interface BatteryWidgetProps {
  state: CockpitState;
  timeline: SchedulerTimeline | null;
  execution: ExecutionTodayResponse | null;
}

// Horizontal-bar battery widget. Shows:
//   - SoC% prominent
//   - Capacity bar with current fill
//   - Current direction (charging/discharging/idle) with kW
//   - Today's min → max SoC range (from execution_log)
//   - Next planned charge/discharge window (from timeline)
export function BatteryWidget({ state, timeline, execution }: BatteryWidgetProps) {
  const socPct = state.soc_pct ?? 0;
  const charging = state.battery_kw > 0.05;
  const discharging = state.battery_kw < -0.05;
  const dirLabel = charging ? "charging" : discharging ? "discharging" : "idle";
  const dirColor = charging ? "var(--ok)" : discharging ? "var(--warn)" : "var(--text-mute)";
  const dirArrow = charging ? "↑" : discharging ? "↓" : "•";

  const todayRange = computeTodayRange(execution);
  const nextEvent = nextSocEvent(timeline);

  // Color the bar by band.
  let barColor = "var(--ok)";
  if (socPct < 20) barColor = "var(--bad)";
  else if (socPct < 50) barColor = "var(--warn)";

  return (
    <div class="battery-widget">
      <div class="battery-head">
        <div>
          <div class="battery-label">Battery</div>
          <div class="battery-pct">{pct(socPct, 0)}</div>
        </div>
        <div class="battery-dir" style={{ color: dirColor }}>
          <span class="battery-dir-arrow">{dirArrow}</span>
          <span class="battery-dir-label">{dirLabel}</span>
          {Math.abs(state.battery_kw) > 0.05 && (
            <span class="battery-dir-kw">{kw(Math.abs(state.battery_kw))}</span>
          )}
        </div>
      </div>

      <div class="battery-bar" role="meter" aria-valuenow={socPct} aria-valuemin={0} aria-valuemax={100}>
        <div class="battery-bar-fill" style={{ width: `${Math.max(0, Math.min(100, socPct))}%`, background: barColor }} />
        {todayRange && (
          <>
            <div class="battery-bar-mark" style={{ left: `${todayRange.min}%` }} title={`Today min ${todayRange.min}%`} />
            <div class="battery-bar-mark" style={{ left: `${todayRange.max}%` }} title={`Today max ${todayRange.max}%`} />
          </>
        )}
      </div>

      <div class="battery-stats">
        <div class="battery-stat">
          <span class="battery-stat-label">Energy</span>
          <span class="battery-stat-value">{kwh(state.soc_kwh)}</span>
        </div>
        {todayRange && (
          <div class="battery-stat">
            <span class="battery-stat-label">Today range</span>
            <span class="battery-stat-value">{todayRange.min}% → {todayRange.max}%</span>
          </div>
        )}
        {nextEvent && (
          <div class="battery-stat">
            <span class="battery-stat-label">Next</span>
            <span class="battery-stat-value" style={{ color: nextEvent.color }}>{nextEvent.label}</span>
          </div>
        )}
      </div>
    </div>
  );
}

function computeTodayRange(exec: ExecutionTodayResponse | null): { min: number; max: number } | null {
  if (!exec?.slots) return null;
  let min = Infinity;
  let max = -Infinity;
  for (const s of exec.slots) {
    if (s.soc_percent == null) continue;
    if (s.soc_percent < min) min = s.soc_percent;
    if (s.soc_percent > max) max = s.soc_percent;
  }
  if (!Number.isFinite(min) || !Number.isFinite(max)) return null;
  return { min: Math.round(min), max: Math.round(max) };
}

function nextSocEvent(timeline: SchedulerTimeline | null): { label: string; color: string } | null {
  if (!timeline?.planned) return null;
  for (const slot of timeline.planned) {
    const kind = (slot.dispatched_kind || slot.lp_kind || "").toLowerCase();
    if (kind === "cheap" || kind === "negative" || kind === "solar_charge" || kind === "solar_preheat") {
      const when = formatLocalTime(slot.slot_time_utc);
      return { label: `Charge at ${when}`, color: "var(--ok)" };
    }
    if (kind === "peak_export") {
      const when = formatLocalTime(slot.slot_time_utc);
      return { label: `Discharge at ${when}`, color: "var(--warn)" };
    }
  }
  return null;
}

function formatLocalTime(iso: string | undefined): string {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  } catch {
    return iso;
  }
}
