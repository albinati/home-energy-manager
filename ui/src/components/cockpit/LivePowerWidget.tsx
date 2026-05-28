import { PowerFlow } from "./PowerFlow";
import { kw, kwh, pct } from "../../lib/format";
import type { CockpitState, CockpitNow, SchedulerTimeline, ExecutionTodayResponse, AgileTodayResponse, MetricsResponse } from "../../lib/types";
import "./cockpit.css";
import "./live-power.css";

interface LivePowerWidgetProps {
  state: CockpitState;
  cockpit: CockpitNow;
  timeline: SchedulerTimeline | null;
  execution: ExecutionTodayResponse | null;
  agile: AgileTodayResponse | null;
  metrics: MetricsResponse | null;
}

// Composite widget: action verb + power flow on the left, battery details
// on the right. Replaces the standalone Battery widget AND the Right Now
// widget — one canonical surface for "what's happening with the energy".
export function LivePowerWidget({ state, cockpit, timeline, execution, agile, metrics }: LivePowerWidgetProps) {
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
  const forced = timeline ? upcomingForcedWindows(timeline, 4) : [];
  const lpInfo = timeline ? { runId: timeline.run_id ?? null, runAt: timeline.run_at ?? null, planDate: timeline.plan_date ?? null } : null;
  const importP = agile?.current_import_p ?? null;
  const exportP = agile?.current_export_p ?? null;
  const importBand = classifyBand(importP, metrics?.cheap_threshold_pence, metrics?.peak_threshold_pence);

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
        <div class="livepower-fox-rates" title="Live Agile p/kWh — import is what you'd pay now, export is what you'd earn now">
          <span class={`livepower-fox-rate livepower-fox-rate--import livepower-fox-rate--band-${importBand}`}>
            <span class="livepower-fox-rate-label">Import</span>
            <span class="livepower-fox-rate-value">{importP != null ? `${importP.toFixed(2)}p` : "—"}</span>
          </span>
          <span class="livepower-fox-rate livepower-fox-rate--export">
            <span class="livepower-fox-rate-label">Export</span>
            <span class="livepower-fox-rate-value">{exportP != null ? `${exportP.toFixed(2)}p` : "—"}</span>
          </span>
        </div>
        <div class="livepower-fox-row">
          <span class="livepower-fox-label">Fox mode</span>
          <span class={`livepower-fox-mode livepower-fox-mode--${foxMode.toLowerCase()}`}>{foxMode}</span>
          {forced.length > 0 && (
            <span class="livepower-fox-windows">
              {forced.map((w) => {
                const start = formatRelativeSlot(w.start_utc, cockpit.now_utc);
                const endTime = endLabelFor(w.end_utc);
                const range = w.slot_count > 1 ? `${start.timeLabel}–${endTime}` : start.timeLabel;
                const tooltip = start.isToday
                  ? `${w.kind} · ${range} · already uploaded to Fox (visible in the app now)`
                  : `${w.kind} · ${start.dayLabel} ${range} · LP plan only — uploads to Fox at 00:05 UTC on the day`;
                return (
                  <span key={w.start_utc}
                        class={`livepower-fox-window livepower-fox-window--${w.kind}${start.isToday ? "" : " livepower-fox-window--future"}`}
                        title={tooltip}>
                    {labelForKind(w.kind)} {start.dayLabel ? `${start.dayLabel} ` : ""}{range}
                  </span>
                );
              })}
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

// Collapse upcoming planned slots that ACTUALLY translate to a non-SelfUse
// Fox group (ForceCharge / ForceDischarge) into contiguous windows of the
// same kind. solar_charge and solar_preheat are LP annotations meaning
// "stay in SelfUse, expect solar to fill the battery naturally" — Fox
// keeps SelfUse, no upload, so they don't belong on a "scheduled events"
// strip the user cross-checks against the Fox ESS app.
interface ForcedWindow { kind: string; start_utc: string; end_utc: string; slot_count: number; }
function upcomingForcedWindows(timeline: SchedulerTimeline, limit: number): ForcedWindow[] {
  const out: ForcedWindow[] = [];
  const interesting = new Set(["cheap", "negative", "peak_export"]);
  let current: ForcedWindow | null = null;
  for (const s of timeline.planned || []) {
    const k = (s.dispatched_kind || s.lp_kind || "").toLowerCase();
    const iso = s.slot_time_utc;
    if (!iso) continue;
    if (interesting.has(k)) {
      if (current && current.kind === k) {
        current.end_utc = iso;
        current.slot_count += 1;
      } else {
        if (current) out.push(current);
        current = { kind: k, start_utc: iso, end_utc: iso, slot_count: 1 };
      }
    } else if (current) {
      out.push(current);
      current = null;
      if (out.length >= limit) break;
    }
  }
  if (current) out.push(current);
  return out.slice(0, limit);
}

function labelForKind(k: string | undefined): string {
  switch ((k || "").toLowerCase()) {
    case "cheap":         return "⚡ ForceCharge";
    case "negative":      return "🔵 ForceCharge";
    case "peak_export":   return "💸 ForceDischarge";
    default:              return k || "?";
  }
}

type Band = "negative" | "cheap" | "standard" | "peak" | "unknown";

function classifyBand(p: number | null | undefined, cheapAt?: number, peakAt?: number): Band {
  if (p == null) return "unknown";
  if (p < 0) return "negative";
  if (cheapAt != null && p <= cheapAt) return "cheap";
  if (peakAt != null && p >= peakAt) return "peak";
  return "standard";
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

// Window end = start of the slot AFTER the last counted one (slots are 30 min).
function endLabelFor(lastSlotStartIso: string): string {
  try {
    const end = new Date(new Date(lastSlotStartIso).getTime() + 30 * 60 * 1000);
    return end.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  } catch {
    return "?";
  }
}

// Compares a slot's local date to "now" and returns (dayLabel, timeLabel,
// isToday). Fox ESS only carries today's schedule — anything dated later
// is LP intent that won't appear in the Fox app until the next 00:05 UTC
// upload. The dayLabel surfaces that gap so the user knows where to look.
interface RelativeSlot { dayLabel: string; timeLabel: string; isToday: boolean; }
function formatRelativeSlot(iso: string | undefined, nowIso?: string | null): RelativeSlot {
  if (!iso) return { dayLabel: "", timeLabel: "—", isToday: false };
  try {
    const slot = new Date(iso);
    const now = nowIso ? new Date(nowIso) : new Date();
    const timeLabel = slot.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
    const slotKey = `${slot.getFullYear()}-${slot.getMonth()}-${slot.getDate()}`;
    const nowKey = `${now.getFullYear()}-${now.getMonth()}-${now.getDate()}`;
    if (slotKey === nowKey) return { dayLabel: "", timeLabel, isToday: true };
    // 1-day difference → "Tomorrow"; longer → short weekday
    const dayDiff = Math.round(
      (Date.UTC(slot.getFullYear(), slot.getMonth(), slot.getDate())
        - Date.UTC(now.getFullYear(), now.getMonth(), now.getDate())) / 86400000,
    );
    if (dayDiff === 1) return { dayLabel: "Tomorrow", timeLabel, isToday: false };
    if (dayDiff > 1 && dayDiff < 7) {
      return { dayLabel: slot.toLocaleDateString([], { weekday: "short" }), timeLabel, isToday: false };
    }
    return {
      dayLabel: slot.toLocaleDateString([], { day: "2-digit", month: "short" }),
      timeLabel, isToday: false,
    };
  } catch {
    return { dayLabel: "", timeLabel: iso, isToday: false };
  }
}
