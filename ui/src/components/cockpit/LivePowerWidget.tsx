import { PowerFlow } from "./PowerFlow";
import { kw, kwh, pct } from "../../lib/format";
import type { CockpitState, CockpitNow, SchedulerTimeline, ExecutionTodayResponse, TimelineSlot } from "../../lib/types";
import "./cockpit.css";
import "./live-power.css";

interface LivePowerWidgetProps {
  state: CockpitState;
  cockpit: CockpitNow;
  timeline: SchedulerTimeline | null;
  execution: ExecutionTodayResponse | null;
}

// Composite widget: action verb + power flow on the left, battery details
// on the right. Replaces the standalone Battery widget AND the Right Now
// widget — one canonical surface for "what's happening with the energy".
export function LivePowerWidget({ state, cockpit, timeline, execution }: LivePowerWidgetProps) {
  const action = inferAction(state);
  const socPct = state.soc_pct ?? 0;
  const charging = state.battery_kw > 0.05;
  const discharging = state.battery_kw < -0.05;
  const dirLabel = charging ? "Charging" : discharging ? "Discharging" : "Idle";
  const dirArrow = charging ? "⚡" : discharging ? "↓" : "·";
  const dirColor = charging ? "var(--ok)" : discharging ? "var(--warn)" : "var(--text-mute)";

  let fillColor = "var(--ok)";
  if (socPct < 20) fillColor = "var(--bad)";
  else if (socPct < 50) fillColor = "var(--warn)";

  const todayRange = computeTodayRange(execution);
  const nextEvent = nextSocEvent(timeline);
  const foxMode = cockpit.current_slot?.fox_mode ?? "—";
  const forced = timeline ? upcomingForced(timeline, 12) : [];
  const lpInfo = timeline ? { runId: timeline.run_id ?? null, runAt: timeline.run_at ?? null, planDate: timeline.plan_date ?? null } : null;

  return (
    <div class="livepower">
      <div class="livepower-action">
        <span class="livepower-action-icon" style={{ background: action.color, boxShadow: `0 0 14px ${action.color}55` }}>
          {action.icon}
        </span>
        <div class="livepower-action-text">
          <div class="livepower-action-title" style={{ color: action.color }}>{action.title}</div>
          <div class="livepower-action-sub">{action.sub}</div>
        </div>
      </div>

      <div class="livepower-body">
        <div class="livepower-flow">
          <PowerFlow state={state} />
        </div>

        <aside class="livepower-batt">
          <div class="livepower-batt-cell">
            <BatteryShape pct={socPct} fillColor={fillColor} charging={charging} discharging={discharging} />
            <div class="livepower-batt-pct">{pct(socPct, 0)}</div>
            <div class="livepower-batt-kwh">{kwh(state.soc_kwh)}</div>
          </div>
          <div class="livepower-batt-dir" style={{ color: dirColor }}>
            <span class="livepower-batt-dir-icon">{dirArrow}</span>
            <span>{dirLabel}</span>
            {Math.abs(state.battery_kw) > 0.05 && (
              <span class="livepower-batt-dir-kw">{` · ${kw(Math.abs(state.battery_kw))}`}</span>
            )}
          </div>
          <div class="livepower-batt-meta">
            {todayRange && (
              <div class="livepower-batt-meta-row">
                <span class="livepower-batt-meta-label">Today</span>
                <span class="livepower-batt-meta-value">{`${todayRange.min}% → ${todayRange.max}%`}</span>
              </div>
            )}
            {nextEvent && (
              <div class="livepower-batt-meta-row">
                <span class="livepower-batt-meta-label">Next</span>
                <span class="livepower-batt-meta-value" style={{ color: nextEvent.color }}>{nextEvent.label}</span>
              </div>
            )}
          </div>
        </aside>
      </div>

      <div class="livepower-fox">
        <div class="livepower-fox-row">
          <span class="livepower-fox-label">Fox mode</span>
          <span class={`livepower-fox-mode livepower-fox-mode--${foxMode.toLowerCase()}`}>{foxMode}</span>
          {forced.length > 0 && (
            <span class="livepower-fox-windows">
              {forced.slice(0, 3).map((f) => (
                <span key={f.slot_time_utc} class={`livepower-fox-window livepower-fox-window--${f.dispatched_kind || f.lp_kind || "std"}`}
                      title={`${f.dispatched_kind || f.lp_kind || "?"} @ ${formatLocalTime(f.slot_time_utc)}`}>
                  {labelForKind(f.dispatched_kind || f.lp_kind)} {formatLocalTime(f.slot_time_utc)}
                </span>
              ))}
            </span>
          )}
        </div>
        {lpInfo && (lpInfo.runId != null || lpInfo.runAt) && (
          <div class="livepower-fox-debug">
            <span title="Last LP run id + wall time + plan target date">
              LP&nbsp;
              {lpInfo.runId != null && <strong>#{lpInfo.runId}</strong>}
              {lpInfo.runAt && <> · {formatLocalTime(lpInfo.runAt)}</>}
              {lpInfo.planDate && <> · plan {lpInfo.planDate}</>}
            </span>
          </div>
        )}
      </div>
    </div>
  );
}

// Return the next N planned slots whose dispatched kind ACTUALLY translates
// to a non-SelfUse Fox group (ForceCharge / ForceDischarge). solar_charge
// and solar_preheat are LP annotations meaning "stay in SelfUse, expect
// solar to fill the battery naturally" — Fox keeps SelfUse, no upload, so
// they don't belong on a "scheduled events" strip the user cross-checks
// against the Fox ESS app.
function upcomingForced(timeline: SchedulerTimeline, limit: number): TimelineSlot[] {
  const out: TimelineSlot[] = [];
  const interesting = new Set(["cheap", "negative", "peak_export"]);
  for (const s of timeline.planned || []) {
    const k = (s.dispatched_kind || s.lp_kind || "").toLowerCase();
    if (interesting.has(k)) out.push(s);
    if (out.length >= limit) break;
  }
  return out;
}

function labelForKind(k: string | undefined): string {
  switch ((k || "").toLowerCase()) {
    case "cheap":         return "⚡ ForceCharge";
    case "negative":      return "🔵 ForceCharge";
    case "peak_export":   return "💸 ForceDischarge";
    default:              return k || "?";
  }
}

function BatteryShape({ pct, fillColor, charging, discharging }: {
  pct: number; fillColor: string; charging: boolean; discharging: boolean;
}) {
  const clamped = Math.max(0, Math.min(100, pct));
  const W = 60;
  const H = 100;
  const term = 8;
  const bodyTop = term;
  const bodyH = H - term;
  const fillH = (clamped / 100) * (bodyH - 6);

  return (
    <svg viewBox={`0 0 ${W} ${H}`} width={W} height={H} class="livepower-batt-svg" aria-hidden="true">
      <defs>
        <linearGradient id="lp-batt-fill" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color={fillColor} stop-opacity="0.85" />
          <stop offset="100%" stop-color={fillColor} stop-opacity="1" />
        </linearGradient>
      </defs>
      <rect x={(W - 24) / 2} y={2} width={24} height={term - 2} rx="2" fill="var(--border-strong)" />
      <rect x={3} y={bodyTop} width={W - 6} height={bodyH} rx="5"
            fill="var(--bg)" stroke="var(--border-strong)" stroke-width="2" />
      <rect
        x={6}
        y={bodyTop + bodyH - 3 - fillH}
        width={W - 12}
        height={fillH}
        rx="3"
        fill="url(#lp-batt-fill)"
        style={{ transition: "y 600ms ease, height 600ms ease, fill 200ms ease" }}
      />
      {(charging || discharging) && (
        <text x={W / 2} y={bodyTop + bodyH / 2 + 7} text-anchor="middle" font-size="26" fill="white"
              style={{ filter: "drop-shadow(0 0 4px " + (charging ? "var(--ok)" : "var(--warn)") + ")" }}>
          {charging ? "⚡" : "↓"}
        </text>
      )}
    </svg>
  );
}

interface Action { title: string; sub: string; icon: string; color: string; }

function inferAction(s: CockpitState): Action {
  const grid = s.grid_kw, batt = s.battery_kw, solar = s.solar_kw, E = 0.1;
  const importing = grid > E, exporting = grid < -E;
  const charging = batt > E, discharging = batt < -E;
  const producing = solar > E;
  if (discharging && exporting) return { title: "Exporting from battery", sub: `${kw(-batt + Math.max(0, solar))} flowing to the grid`, icon: "⚡", color: "var(--peak-export)" };
  if (exporting && !discharging) return { title: "Exporting solar surplus", sub: `${kw(-grid)} to grid · ${kw(s.load_kw)} house · ${kw(solar)} solar`, icon: "☀", color: "var(--export)" };
  if (charging && importing) return { title: "Charging from grid", sub: `${kw(grid)} import · battery climbing at ${kw(batt)}`, icon: "⚡", color: "var(--cheap)" };
  if (charging && producing) return { title: "Charging from solar", sub: `${kw(solar)} solar · battery climbing at ${kw(batt)}`, icon: "⚡", color: "var(--pv)" };
  if (discharging) return { title: "Battery → house", sub: `${kw(-batt)} from battery · ${kw(s.load_kw)} house load`, icon: "🔋", color: "var(--warn)" };
  if (importing) return { title: "Importing from grid", sub: `${kw(grid)} import · ${kw(s.load_kw)} house`, icon: "⬇", color: "var(--import)" };
  if (producing) return { title: "Self-using solar", sub: `${kw(solar)} solar covering ${kw(s.load_kw)} house`, icon: "☀", color: "var(--pv)" };
  return { title: "Holding", sub: `${kw(s.load_kw)} house · battery ${pct(s.soc_pct, 0)} · waiting`, icon: "•", color: "var(--text-mute)" };
}

function computeTodayRange(exec: ExecutionTodayResponse | null): { min: number; max: number } | null {
  if (!exec?.slots) return null;
  let mn = Infinity, mx = -Infinity;
  for (const s of exec.slots) {
    if (s.soc_percent == null) continue;
    if (s.soc_percent < mn) mn = s.soc_percent;
    if (s.soc_percent > mx) mx = s.soc_percent;
  }
  if (!Number.isFinite(mn) || !Number.isFinite(mx)) return null;
  return { min: Math.round(mn), max: Math.round(mx) };
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
  try { return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false }); }
  catch { return iso; }
}
