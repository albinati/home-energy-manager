import { kw, kwh, pct } from "../../lib/format";
import type { CockpitState, SchedulerTimeline, ExecutionTodayResponse } from "../../lib/types";

interface BatteryWidgetProps {
  state: CockpitState;
  timeline: SchedulerTimeline | null;
  execution: ExecutionTodayResponse | null;
}

// Stylised battery cell with animated fill + direction icon. Today's
// min/max SoC and the next planned charge/discharge come from the LP
// timeline + execution history.
export function BatteryWidget({ state, timeline, execution }: BatteryWidgetProps) {
  const socPct = state.soc_pct ?? 0;
  const charging = state.battery_kw > 0.05;
  const discharging = state.battery_kw < -0.05;
  const dirLabel = charging ? "Charging" : discharging ? "Discharging" : "Idle";
  const dirColor = charging ? "var(--ok)" : discharging ? "var(--warn)" : "var(--text-mute)";

  const todayRange = computeTodayRange(execution);
  const nextEvent = nextSocEvent(timeline);

  // Fill colour by SoC band
  let fillColor = "var(--ok)";
  if (socPct < 20) fillColor = "var(--bad)";
  else if (socPct < 50) fillColor = "var(--warn)";

  return (
    <div class="battery-widget">
      <div class="battery-widget-main">
        <BatteryShape pct={socPct} fillColor={fillColor} charging={charging} discharging={discharging} />
        <div class="battery-widget-readout">
          <div class="battery-widget-pct">{pct(socPct, 0)}</div>
          <div class="battery-widget-kwh">{kwh(state.soc_kwh)}</div>
          <div class="battery-widget-dir" style={{ color: dirColor }}>
            <DirIcon charging={charging} discharging={discharging} />
            <span class="battery-widget-dir-label">{dirLabel}</span>
            {Math.abs(state.battery_kw) > 0.05 && (
              <span class="battery-widget-dir-kw">{kw(Math.abs(state.battery_kw))}</span>
            )}
          </div>
        </div>
      </div>

      <div class="battery-widget-meta">
        {todayRange && (
          <div class="battery-widget-meta-row">
            <span class="battery-widget-meta-label">Today</span>
            <span class="battery-widget-meta-value">
              {todayRange.min}% → {todayRange.max}%
            </span>
          </div>
        )}
        {nextEvent && (
          <div class="battery-widget-meta-row">
            <span class="battery-widget-meta-label">Next</span>
            <span class="battery-widget-meta-value" style={{ color: nextEvent.color }}>
              {nextEvent.label}
            </span>
          </div>
        )}
      </div>
    </div>
  );
}

// Battery cell shape — vertical rectangle with terminal at top. Fill animates
// to current SoC. When charging/discharging an overlay shimmer plays.
function BatteryShape({ pct, fillColor, charging, discharging }: {
  pct: number; fillColor: string; charging: boolean; discharging: boolean;
}) {
  const clamped = Math.max(0, Math.min(100, pct));
  const W = 80;
  const H = 130;
  const term = 12;     // terminal height
  const bodyTop = term;
  const bodyH = H - term;
  const fillH = (clamped / 100) * (bodyH - 6);

  return (
    <svg viewBox={`0 0 ${W} ${H}`} width="80" height="130" class="battery-svg" aria-hidden="true">
      <defs>
        <linearGradient id="batt-shine" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="white" stop-opacity="0.18" />
          <stop offset="100%" stop-color="white" stop-opacity="0" />
        </linearGradient>
        <linearGradient id="batt-fill-grad" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color={fillColor} stop-opacity="0.85" />
          <stop offset="100%" stop-color={fillColor} stop-opacity="1" />
        </linearGradient>
      </defs>

      {/* Terminal */}
      <rect x={(W - 32) / 2} y={2} width={32} height={term - 2} rx="2" fill="var(--border-strong)" />
      {/* Body outline */}
      <rect x={4} y={bodyTop} width={W - 8} height={bodyH} rx="6"
            fill="var(--bg)" stroke="var(--border-strong)" stroke-width="2" />
      {/* Fill */}
      <rect
        x={7}
        y={bodyTop + bodyH - 3 - fillH}
        width={W - 14}
        height={fillH}
        rx="4"
        fill="url(#batt-fill-grad)"
        style={{ transition: "y 600ms ease, height 600ms ease, fill 200ms ease" }}
      />
      {/* Shine highlight */}
      <rect x={7} y={bodyTop + 3} width={W - 14} height={(bodyH - 6) / 2} rx="4" fill="url(#batt-shine)" />

      {/* Direction overlay */}
      {(charging || discharging) && (
        <g class={charging ? "battery-charge-overlay" : "battery-discharge-overlay"}>
          <text
            x={W / 2}
            y={bodyTop + bodyH / 2 + 8}
            text-anchor="middle"
            font-size="34"
            fill="white"
            style={{ filter: "drop-shadow(0 0 6px " + (charging ? "var(--ok)" : "var(--warn)") + ")" }}
          >
            {charging ? "⚡" : "↓"}
          </text>
        </g>
      )}
    </svg>
  );
}

function DirIcon({ charging, discharging }: { charging: boolean; discharging: boolean }) {
  if (charging) return <span class="battery-widget-dir-icon">⚡</span>;
  if (discharging) return <span class="battery-widget-dir-icon">↓</span>;
  return <span class="battery-widget-dir-icon">•</span>;
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
      return { label: `Charge ${when}`, color: "var(--ok)" };
    }
    if (kind === "peak_export") {
      const when = formatLocalTime(slot.slot_time_utc);
      return { label: `Export ${when}`, color: "var(--warn)" };
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
