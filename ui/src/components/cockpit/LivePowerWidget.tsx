import { useEffect, useState } from "preact/hooks";
import { PowerFlow } from "./PowerFlow";
import { Icon, type IconName } from "../common/Icon";
import { useAnimatedNumber } from "../../lib/useAnimatedNumber";
import { reducedMotion } from "../../lib/motion";
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

const RM = reducedMotion();

// The instrument panel: a hero NET grid-power number anchors the surface,
// the live power-flow is the centerpiece, everything else (action verb,
// battery SoC, Fox mode, tariff, scheduled windows) recedes to quiet
// monochrome. Domain colour appears only on the flow + the focal-value tint.
export function LivePowerWidget({ state, cockpit, timeline, execution, agile, metrics }: LivePowerWidgetProps) {
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
  const foxMode = cockpit.current_slot?.fox_mode ?? "—";
  const forced = timeline ? upcomingForcedWindows(timeline, 4) : [];
  const lpInfo = timeline ? { runId: timeline.run_id ?? null, runAt: timeline.run_at ?? null, planDate: timeline.plan_date ?? null } : null;
  const importP = agile?.current_import_p ?? null;
  const exportP = agile?.current_export_p ?? null;
  const importBand = classifyBand(importP, metrics?.cheap_threshold_pence, metrics?.peak_threshold_pence);

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
        <div class="livepower-fox-rates" title="Live Agile p/kWh — import is what you'd pay now, export is what you'd earn now">
          <span class="livepower-fox-rate">
            <span class="livepower-fox-rate-label">Import</span>
            <span class={`livepower-fox-rate-value livepower-rate--band-${importBand}`}>{importP != null ? `${importP.toFixed(2)}p` : "—"}</span>
          </span>
          <span class="livepower-fox-rate">
            <span class="livepower-fox-rate-label">Export</span>
            <span class="livepower-fox-rate-value livepower-rate--export">{exportP != null ? `${exportP.toFixed(2)}p` : "—"}</span>
          </span>
        </div>
        <div class="livepower-fox-row">
          <span class="livepower-fox-label">Fox mode</span>
          <span class={`livepower-fox-mode livepower-fox-mode--${foxMode.toLowerCase()}`}>
            <span class="livepower-fox-mode-dot" />
            {foxMode}
          </span>
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
                    {!start.isToday && <span class="livepower-fox-window-icon"><Icon name="schedule" size={12} /></span>}
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

// Kind conveyed by the leading coloured dot + band colour — no emoji.
function labelForKind(k: string | undefined): string {
  switch ((k || "").toLowerCase()) {
    case "cheap":         return "Force charge";
    case "negative":      return "Force charge";
    case "peak_export":   return "Force discharge";
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

function endLabelFor(lastSlotStartIso: string): string {
  try {
    const end = new Date(new Date(lastSlotStartIso).getTime() + 30 * 60 * 1000);
    return end.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  } catch {
    return "?";
  }
}

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
