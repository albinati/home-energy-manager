// Shared "selected period" state for the home dashboard. A single signal drives
// the Hero headline, the cost-breakdown bar, and the energy-flow chart so the
// whole page re-scopes together when the user steps day/week/month/year. The
// live-now strip (cockpit) deliberately ignores this — it's always "now".
//
// Mirrors the signal pattern in theme.ts.

import { signal } from "@preact/signals-core";
import { useComputed } from "@preact/signals";

export type Granularity = "day" | "week" | "month" | "year";

export interface PeriodState {
  gran: Granularity;
  // Always a YYYY-MM-DD local date. For month we use its YYYY-MM; for year its
  // YYYY; for day/week the date itself (week = the Monday-based week it falls in,
  // matching the backend's get_period_insights week logic).
  anchor: string;
}

function todayISO(): string {
  const d = new Date();
  return isoOf(d);
}

function isoOf(d: Date): string {
  // Local-date ISO (not UTC) so stepping never drifts across a TZ boundary.
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

function parse(anchor: string): Date {
  return new Date(`${anchor}T00:00:00`);
}

// Default view: today — so the intraday detail (forecast-vs-actual + tariff
// zones + export/import price slots) shows on the timelines without first
// having to step down from a coarser granularity.
export const selectedPeriod = signal<PeriodState>({ gran: "day", anchor: todayISO() });

/** The UTC calendar date — the day the "today" endpoints (/execution/today,
 * /pv/today, /grid/today) actually serve. In BST this lags the local date by
 * one hour after local midnight (00:00–00:59 local is still yesterday's UTC
 * day); in GMT the two always coincide. */
export function utcTodayISO(): string {
  return new Date().toISOString().slice(0, 10);
}

// Midnight rollover: the anchor is initialized once per page load, so a tab
// left open across local midnight stayed pinned on yesterday as "today"
// forever (until a manual reload). Re-anchor ONLY when the user was sitting
// on the old today — a deliberately-navigated past date must not jump under
// them. setInterval fires (throttled) in background tabs too, so a tab
// revisited in the morning has already rolled.
let _rolloverToday = todayISO();
if (typeof window !== "undefined") {
  setInterval(() => {
    const t = todayISO();
    if (t === _rolloverToday) return;
    const prev = _rolloverToday;
    _rolloverToday = t;
    const cur = selectedPeriod.value;
    if (cur.anchor === prev) selectedPeriod.value = { ...cur, anchor: t };
  }, 60_000);
}

/** Subscribe to the selected period inside a component (re-renders on change). */
export function usePeriod(): PeriodState {
  return useComputed(() => selectedPeriod.value).value;
}

/** Switch granularity, keeping the anchor (clamped to today if it'd be future). */
export function setGranularity(gran: Granularity): void {
  const cur = selectedPeriod.value.anchor;
  const t = todayISO();
  selectedPeriod.value = { gran, anchor: cur > t ? t : cur };
}

/** Jump straight to the current period, keeping the chosen granularity — the
 * "Today" affordance. Snaps the anchor to today so isCurrentPeriod() is true. */
export function goToNow(): void {
  lastStepDir.value = 0;
  selectedPeriod.value = { gran: selectedPeriod.value.gran, anchor: todayISO() };
}

/** Direction of the most recent navigator step — drives the charts' slide
 * entrance (see lib/navMotion.ts). 0 = jump (Today button, granularity swap,
 * midnight rollover), which renders without a directional slide. */
export const lastStepDir = signal<-1 | 0 | 1>(0);

/** Step the anchor backward (-1) or forward (+1) by one unit of the granularity. */
export function stepPeriod(dir: -1 | 1): void {
  const { gran, anchor } = selectedPeriod.value;
  const d = parse(anchor);
  if (gran === "day") d.setDate(d.getDate() + dir);
  else if (gran === "week") d.setDate(d.getDate() + 7 * dir);
  else if (gran === "month") { d.setDate(1); d.setMonth(d.getMonth() + dir); }
  else { d.setMonth(0, 1); d.setFullYear(d.getFullYear() + dir); }
  // Never step into the future.
  const next = isoOf(d);
  if (dir === 1 && next > todayISO()) return;
  lastStepDir.value = dir;
  selectedPeriod.value = { gran, anchor: next };
}

/** Inclusive [start, end] ISO date window for the selected period, end clamped
 * to today (no future). Used to scope the tariff comparison to the navigator. */
export function periodDateRange(p: PeriodState): { start: string; end: string } {
  const t = todayISO();
  const d = parse(p.anchor);
  let start: Date;
  let end: Date;
  if (p.gran === "day") {
    start = d; end = d;
  } else if (p.gran === "week") {
    start = new Date(d);
    start.setDate(d.getDate() - ((d.getDay() + 6) % 7)); // Monday
    end = new Date(start);
    end.setDate(start.getDate() + 6);
  } else if (p.gran === "month") {
    start = new Date(d.getFullYear(), d.getMonth(), 1);
    end = new Date(d.getFullYear(), d.getMonth() + 1, 0); // last day of month
  } else {
    start = new Date(d.getFullYear(), 0, 1);
    end = new Date(d.getFullYear(), 11, 31);
  }
  const startISO = isoOf(start);
  const endISO = isoOf(end);
  return { start: startISO, end: endISO > t ? t : endISO };
}

/** Maximum heatmap lookback. The day-of-week × hour grid is a multi-week
 * behavioural pattern, so a longer window adds little but each distinct window
 * is an uncached ~2s server-side physics rebuild that serialises on the DB lock
 * — capping bounds the worst case. Also the default the live profile is cached
 * under, so day/week reuse the always-warm entry. */
const HEATMAP_MAX_WINDOW_DAYS = 120;

/** Trailing window + optional end anchor for the load HEATMAP.
 *
 * day/week → NO anchor: the heatmap is a multi-week pattern, not a per-day
 * figure, so stepping day-by-day reuses the always-warm live 120-day profile
 * (no cold rebuild, no DB-lock pile-up — the cause of the navigation 504s).
 * month/year → anchored at the period end with a capped window; few distinct
 * anchors, so the 4-min TTL cache absorbs repeat views. */
export function periodWindow(p: PeriodState): { windowDays: number; endDate?: string } {
  if (p.gran === "day" || p.gran === "week") return { windowDays: HEATMAP_MAX_WINDOW_DAYS };
  const { start, end } = periodDateRange(p);
  const span = Math.round((parse(end).getTime() - parse(start).getTime()) / 86_400_000) + 1;
  return { windowDays: Math.min(Math.max(span, 28), HEATMAP_MAX_WINDOW_DAYS), endDate: end };
}

/** The last COMPLETE local day within the selected period — what the LP
 * scorecard (plan-vs-realised) should show. For the current period that's
 * yesterday; for a past period it's the period's end (already clamped to today
 * by periodDateRange). */
export function periodLastCompleteDay(p: PeriodState): string {
  const { end } = periodDateRange(p);
  const t = todayISO();
  if (end < t) return end;            // wholly past period → its last day is complete
  const d = parse(t);
  d.setDate(d.getDate() - 1);         // current period → yesterday
  return isoOf(d);
}

/** Query params for getEnergyPeriod(). */
export function periodFetchOpts(p: PeriodState): { date?: string; month?: string; year?: number } {
  if (p.gran === "month") return { month: p.anchor.slice(0, 7) };
  if (p.gran === "year") return { year: Number(p.anchor.slice(0, 4)) };
  return { date: p.anchor }; // day + week
}

/** True when the selected period contains today — used to disable "next". */
export function isCurrentPeriod(p: PeriodState): boolean {
  const t = todayISO();
  if (p.gran === "day") return p.anchor === t;
  if (p.gran === "month") return p.anchor.slice(0, 7) === t.slice(0, 7);
  if (p.gran === "year") return p.anchor.slice(0, 4) === t.slice(0, 4);
  // week: Monday-based window containing the anchor includes today?
  const d = parse(p.anchor);
  const monday = new Date(d);
  monday.setDate(d.getDate() - ((d.getDay() + 6) % 7)); // 0=Sun → 6, 1=Mon → 0
  const sunday = new Date(monday);
  sunday.setDate(monday.getDate() + 6);
  const today = parse(t);
  return today >= monday && today <= sunday;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** Instant local label for the navigator (the API also returns period_label). */
export function periodLabel(p: PeriodState): string {
  const d = parse(p.anchor);
  if (p.gran === "day") {
    return isCurrentPeriod(p) ? "Today" : `${d.getDate()} ${MONTHS[d.getMonth()]} ${d.getFullYear()}`;
  }
  if (p.gran === "month") return `${MONTHS[d.getMonth()]} ${d.getFullYear()}`;
  if (p.gran === "year") return String(d.getFullYear());
  // week
  const monday = new Date(d);
  monday.setDate(d.getDate() - ((d.getDay() + 6) % 7));
  if (isCurrentPeriod(p)) return "This week";
  return `Week of ${monday.getDate()} ${MONTHS[monday.getMonth()]}`;
}

/** Short noun used in the Hero eyebrow ("June", "This week", "2 Jun", "2026"). */
export function periodNoun(p: PeriodState): string {
  return periodLabel(p);
}

/** Chrome-stepper / scope-divider form (redesign): a scope word plus, when it
 * adds information, the concrete date — "Today · 8 Jun 2026",
 * "This week · 9–15 Jun 2026". Month/year labels already ARE the date. */
export function periodScope(p: PeriodState): { scope: string; date?: string } {
  const scope = periodLabel(p);
  if (p.gran === "day") {
    const d = parse(p.anchor);
    const date = `${d.getDate()} ${MONTHS[d.getMonth()]} ${d.getFullYear()}`;
    return isCurrentPeriod(p) ? { scope, date } : { scope };
  }
  if (p.gran === "week") {
    // Full Monday-based week, NOT periodDateRange (whose end clamps to today —
    // mid-week that would misread as "8–10 Jun").
    const d = parse(p.anchor);
    const s = new Date(d);
    s.setDate(d.getDate() - ((d.getDay() + 6) % 7)); // Monday
    const e = new Date(s);
    e.setDate(s.getDate() + 6);
    const date = s.getFullYear() !== e.getFullYear()
      ? `${s.getDate()} ${MONTHS[s.getMonth()]} ${s.getFullYear()} – ${e.getDate()} ${MONTHS[e.getMonth()]} ${e.getFullYear()}`
      : s.getMonth() === e.getMonth()
        ? `${s.getDate()}–${e.getDate()} ${MONTHS[e.getMonth()]} ${e.getFullYear()}`
        : `${s.getDate()} ${MONTHS[s.getMonth()]} – ${e.getDate()} ${MONTHS[e.getMonth()]} ${e.getFullYear()}`;
    return { scope, date };
  }
  return { scope };
}
