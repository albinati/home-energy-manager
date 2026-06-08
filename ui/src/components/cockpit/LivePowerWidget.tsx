import { useEffect, useState } from "preact/hooks";
import { PowerFlow } from "./PowerFlow";
import { Icon, type IconName } from "../common/Icon";
import { useAnimatedNumber } from "../../lib/useAnimatedNumber";
import { reducedMotion } from "../../lib/motion";
import { kw, kwh, pct, gbp } from "../../lib/format";
import type { CockpitState, CockpitNow, SchedulerTimeline, ExecutionTodayResponse, AgileTodayResponse, MetricsResponse, DhwScheduleRow, TodayCumulativeResponse } from "../../lib/types";
import { formatRelativeSlot, tankLabelOf } from "../../lib/slotLabels";
import { upcomingForcedWindows, labelForKind, kindColorVar, type ForcedWindow } from "../../lib/planHelpers";
import "./cockpit.css";
import "./live-power.css";

interface LivePowerWidgetProps {
  state: CockpitState;
  cockpit: CockpitNow;
  timeline: SchedulerTimeline | null;
  execution: ExecutionTodayResponse | null;
  agile: AgileTodayResponse | null;
  metrics: MetricsResponse | null;
  dhwSchedule?: DhwScheduleRow[] | null;
  todayCumulative?: TodayCumulativeResponse | null;
}

const RM = reducedMotion();

// The instrument panel: a hero NET grid-power number anchors the surface,
// the live power-flow is the centerpiece, everything else (action verb,
// battery SoC, Fox mode, tariff, scheduled windows) recedes to quiet
// monochrome. Domain colour appears only on the flow + the focal-value tint.
export function LivePowerWidget({ state, cockpit, timeline, execution, agile, metrics, dhwSchedule, todayCumulative }: LivePowerWidgetProps) {
  const socPct = state.soc_pct ?? 0;
  const charging = state.battery_kw > 0.05;
  const discharging = state.battery_kw < -0.05;
  const dirLabel = charging ? "Charging" : discharging ? "Discharging" : "Idle";
  const dirIcon: IconName | null = charging ? "power-live" : discharging ? "export" : null;
  const dirColor = charging ? "var(--ok)" : discharging ? "var(--warn)" : "var(--text-mute)";

  // Hero NET grid power. ACCURACY: sign of grid_kw drives meaning, never the
  // magnitude — positive = import (red), negative = export (green),
  // |grid_kw| < 0.05 = balanced. Value/unit/source (/cockpit/now) unchanged.
  const gridKw = state.grid_kw;
  const importing = gridKw > 0.05;
  const exporting = gridKw < -0.05;
  const netLabel = importing ? "IMPORTING" : exporting ? "EXPORTING" : "SELF-SUPPLIED";
  // One-line caption so the big kW number reads unambiguously as grid power,
  // and "self-supplied" (formerly "balanced") is explained.
  const netCaption = importing ? "drawn from the grid"
    : exporting ? "sent to the grid"
    : "no grid flow — solar + battery are covering the house";
  const netColor = importing ? "var(--import)" : exporting ? "var(--export)" : "var(--text-dim)";
  const netKwAnim = useAnimatedNumber(Math.abs(gridKw));
  const socAnim = useAnimatedNumber(socPct);

  // SoC fill colour by threshold (meaning — unchanged).
  let fillColor = "var(--ok)";
  if (socPct < 20) fillColor = "var(--bad)";
  else if (socPct < 50) fillColor = "var(--warn)";

  const todayRange = computeTodayRange(execution);
  const nextEvent = nextSocEvent(timeline);
  // Fox windows feed the quiet "Next" glance below; the full plan (Fox mode,
  // windows, LP run) now lives in the standalone Plan widget.
  const forced = timeline ? upcomingForcedWindows(timeline, 4) : [];
  const importP = agile?.current_import_p ?? null;
  const exportP = agile?.current_export_p ?? cockpit.current_slot?.price_export_p ?? null;
  const importBand = classifyBand(importP, metrics?.cheap_threshold_pence, metrics?.peak_threshold_pence);
  // What the system does NEXT — the soonest upcoming battery + tank actions,
  // surfaced as one quiet line so "what's the plan about to do?" reads at a
  // glance (tank chips moved to the Heating tile; this keeps tank awareness here).
  const nextActions = buildNextActions(forced, dhwSchedule, cockpit.now_utc);

  return (
    <div class="livepower">
      {/* Hero — the one focal number of this surface */}
      <div class="livepower-hero">
        <div class="livepower-hero-eyebrow">
          <span class="livepower-hero-icon"><Icon name="power-live" size={14} /></span>
          <span class="live-pulse livepower-hero-dot" />
          {netLabel}
        </div>
        <div class="livepower-hero-num" style={{ color: netColor }}>
          {netKwAnim != null ? netKwAnim.toFixed(2) : "—"}
          <span class="livepower-hero-unit">kW</span>
        </div>
        <div class="livepower-hero-cap">{netCaption}</div>
        {nextActions.length > 0 && (
          <div class="livepower-next" title="The next scheduled battery + tank actions">
            <span class="livepower-next-label">Next</span>
            {nextActions.map((a, i) => (
              <span key={i} class="livepower-next-item" style={{ color: a.color }}>
                <span class="livepower-next-dot" style={{ background: a.color }} />
                {a.label} <span class="livepower-next-when">{a.when}</span>
              </span>
            ))}
          </div>
        )}
      </div>

      {/* The power-flow centerpiece + battery panel */}
      <div class="livepower-body">
        <div class="livepower-flow">
          <PowerFlow state={state} />
        </div>

        <aside class="livepower-batt">
          <div class="livepower-batt-cell">
            <BatteryShape pct={socPct} fillColor={fillColor} />
            <div class="livepower-batt-pct">{socAnim != null ? pct(socAnim, 0) : "—"}</div>
            <div class="livepower-batt-kwh">{kwh(state.soc_kwh)}</div>
          </div>
          <div class="livepower-batt-dir" style={{ color: dirColor }}>
            {dirIcon && <span class="livepower-batt-dir-icon"><Icon name={dirIcon} size={14} /></span>}
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

      {/* Secondary: tariff rates + Fox mode + scheduled windows + LP run */}
      <div class="livepower-fox">
        <div class="livepower-fox-rates" title="Live Agile p/kWh + how much you've imported/exported so far today (to now)">
          <span class="livepower-fox-rate">
            <span class="livepower-fox-rate-label">Import</span>
            <span class={`livepower-fox-rate-value livepower-rate--band-${importBand}`}>{importP != null ? `${importP.toFixed(2)}p` : "—"}</span>
            {todayCumulative && (
              <span class="livepower-fox-rate-sub">
                {kwh(todayCumulative.import_kwh)} today ·{" "}
                {todayCumulative.import_cost_gbp < -0.005
                  ? <span class="livepower-credit" title="Paid to import on negative-price slots">+{gbp(Math.abs(todayCumulative.import_cost_gbp))} credit</span>
                  : <span>{gbp(todayCumulative.import_cost_gbp)}</span>}
              </span>
            )}
          </span>
          <span class="livepower-fox-rate">
            <span class="livepower-fox-rate-label">Export</span>
            {exportP != null
              ? <span class="livepower-fox-rate-value livepower-rate--export">{exportP.toFixed(2)}p</span>
              : <span class="livepower-fox-rate-value livepower-fox-rate-value--none">no export</span>}
            {todayCumulative && (
              <span class="livepower-fox-rate-sub">
                {kwh(todayCumulative.export_kwh)} today ·{" "}
                <span class="livepower-rate--export">{gbp(todayCumulative.export_revenue_gbp)}</span>
              </span>
            )}
          </span>
        </div>
        {/* The committed dispatch plan (Fox battery windows + tank schedule +
            LP run) moved out to its own Plan widget next to Weather. The quiet
            "Next" glance at the top of this tile keeps the live awareness. */}
      </div>
    </div>
  );
}

interface NextAction { label: string; when: string; whenMs: number; color: string; }

// Merge the soonest upcoming battery window + the next future tank action into
// a single chronologically-ordered "Next" line (max 2 items). Reuses the same
// kind/label vocabulary as the chips; the dot colour here is SEMANTIC (charge =
// ok/green, drain = warn/amber), intentionally not the band hue the chips use.
function buildNextActions(
  forced: ForcedWindow[],
  dhw: DhwScheduleRow[] | null | undefined,
  nowIso: string | undefined,
): NextAction[] {
  const out: NextAction[] = [];
  const rel = (iso: string) => {
    const r = formatRelativeSlot(iso, nowIso);
    return `${r.dayLabel ? r.dayLabel + " " : ""}${r.timeLabel}`;
  };
  if (forced.length > 0) {
    const w = forced[0];
    out.push({ label: labelForKind(w.kind), when: rel(w.start_utc), whenMs: Date.parse(w.start_utc),
               color: kindColorVar(w.kind) });
  }
  const nowMs = nowIso ? Date.parse(nowIso) : Date.now();
  const nextTank = (dhw || [])
    .filter((r) => r.start_utc && Date.parse(r.start_utc) > nowMs)
    .sort((a, b) => Date.parse(a.start_utc!) - Date.parse(b.start_utc!))[0];
  if (nextTank?.start_utc) {
    out.push({ label: tankLabelOf(nextTank.action_type), when: rel(nextTank.start_utc),
               whenMs: Date.parse(nextTank.start_utc), color: "var(--warn)" });
  }
  // Drop any entry whose timestamp failed to parse (whenMs NaN) — a NaN in the
  // comparator leaves the sort order undefined and could surface the wrong action.
  return out.filter((a) => Number.isFinite(a.whenMs)).sort((a, b) => a.whenMs - b.whenMs).slice(0, 2);
}

type Band = "negative" | "cheap" | "standard" | "peak" | "unknown";

function classifyBand(p: number | null | undefined, cheapAt?: number, peakAt?: number): Band {
  if (p == null) return "unknown";
  if (p < 0) return "negative";
  if (cheapAt != null && p <= cheapAt) return "cheap";
  if (peakAt != null && p >= peakAt) return "peak";
  return "standard";
}

// Battery cell with a spring fill: on mount the level rises from 0 to the real
// SoC with a ~2% overshoot lock-in (--ease-lock). No in-cell glyph — direction
// reads from the text below. SoC value/units/colour-meaning unchanged.
function BatteryShape({ pct: socPct, fillColor }: { pct: number; fillColor: string }) {
  const clamped = Math.max(0, Math.min(100, socPct));
  const W = 60, H = 100, term = 8;
  const bodyTop = term, bodyH = H - term;
  const targetFillH = (clamped / 100) * (bodyH - 6);

  // Start at 0 on mount, spring to real value next frame (skip under RM).
  const [fillH, setFillH] = useState(RM ? targetFillH : 0);
  useEffect(() => {
    if (RM) { setFillH(targetFillH); return; }
    const id = requestAnimationFrame(() => setFillH(targetFillH));
    return () => cancelAnimationFrame(id);
  }, [targetFillH]);

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
        style={{ transition: RM ? "none" : "y 700ms var(--ease-lock), height 700ms var(--ease-lock), fill 200ms ease" }}
      />
    </svg>
  );
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

// formatRelativeSlot + endLabelFor now live in ../../lib/slotLabels (shared
// with the tank badges).
