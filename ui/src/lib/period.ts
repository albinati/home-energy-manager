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

// Default view: the current month (the figure backed by the most metered data).
export const selectedPeriod = signal<PeriodState>({ gran: "month", anchor: todayISO() });

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
