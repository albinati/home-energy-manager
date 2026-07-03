// Day-bundle cache + neighbour prefetch for the sliding day navigation.
//
// Stepping the period navigator used to feel like a page swap because every
// arrow click paid 4 sequential-feeling fetches before anything rendered.
// This module makes the step instant:
//   * SETTLED past days (< yesterday) land in poll.ts's immutable cache under
//     the exact key EnergyChartWidget already reads (`energychart:day:<d>`).
//   * NOT-yet-settled days (yesterday before the ~04:30 backfill, and the
//     BST midnight-hour view of the new day) go into a short-TTL session map
//     instead — fresh enough to feel instant, never frozen for the session.
//   * prefetchNeighbourDays() warms anchor±1 during idle time, so the arrow
//     the user is most likely to press next is already loaded.
import {
  getExecutionToday,
  getPvToday,
  getGridToday,
  getDaikinConsumption,
} from "./endpoints";
import { getImmutableCache, setImmutableCache } from "./poll";
import type {
  ExecutionTodayResponse,
  PvTodayResponse,
  GridTodayResponse,
  DaikinConsumptionResponse,
} from "./types";

export interface DayBundle {
  exec: ExecutionTodayResponse | null;
  pv: PvTodayResponse | null;
  grid: GridTodayResponse | null;
  daikin: DaikinConsumptionResponse | null;
}

const TTL_MS = 5 * 60_000;
const _ttlCache = new Map<string, { at: number; v: DayBundle }>();
const _inflight = new Map<string, Promise<DayBundle>>();

const immutableKey = (d: string) => `energychart:day:${d}`;

function localISO(date: Date): string {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
}
function shiftISO(d: string, days: number): string {
  const dt = new Date(`${d}T00:00:00`);
  dt.setDate(dt.getDate() + days);
  return localISO(dt);
}
function yesterdayISO(): string {
  return shiftISO(localISO(new Date()), -1);
}

/** Cached bundle for a past day, or undefined. Checks the immutable cache
 * (settled days) first, then the TTL map (recent days). */
export function getCachedDay(d: string): DayBundle | undefined {
  const im = getImmutableCache<DayBundle>(immutableKey(d));
  if (im) return im;
  const hit = _ttlCache.get(d);
  if (hit && Date.now() - hit.at < TTL_MS) return hit.v;
  return undefined;
}

/** Fetch (or serve cached) the full per-slot bundle for a PAST local day.
 * Settled days are stored immutably; recent ones on the TTL map. Concurrent
 * calls for the same day share one round of requests. */
export function fetchDayBundle(d: string): Promise<DayBundle> {
  const cached = getCachedDay(d);
  if (cached) return Promise.resolve(cached);
  const inflight = _inflight.get(d);
  if (inflight) return inflight;
  const p = Promise.all([
    getExecutionToday(d).catch(() => null),
    getPvToday(d).catch(() => null),
    getGridToday(d).catch(() => null),
    getDaikinConsumption("day", { date: d }).catch(() => null),
  ]).then(([exec, pv, grid, daikin]) => {
    const v: DayBundle = { exec, pv, grid, daikin };
    // An all-null bundle means the network is down — don't cache the outage.
    if (exec || pv || grid || daikin) {
      if (d < yesterdayISO()) setImmutableCache(immutableKey(d), v);
      else {
        if (_ttlCache.size > 32) _ttlCache.clear();
        _ttlCache.set(d, { at: Date.now(), v });
      }
    }
    return v;
  });
  _inflight.set(d, p);
  p.finally(() => { if (_inflight.get(d) === p) _inflight.delete(d); });
  return p;
}

/** Idle-warm the days adjacent to `anchor` (day granularity only): the
 * previous day always; the next day when it isn't today/future (today is the
 * polled live view, never bundle-cached). Returns a cancel function. */
export function prefetchNeighbourDays(anchor: string): () => void {
  const today = localISO(new Date());
  const targets = [shiftISO(anchor, -1)];
  const next = shiftISO(anchor, 1);
  if (next < today) targets.push(next);
  const idle: (cb: () => void) => number =
    typeof requestIdleCallback === "function"
      ? (cb) => requestIdleCallback(cb, { timeout: 4000 })
      : (cb) => window.setTimeout(cb, 1200);
  const cancelIdle =
    typeof cancelIdleCallback === "function" ? cancelIdleCallback : clearTimeout;
  const handle = idle(() => {
    for (const d of targets) if (!getCachedDay(d)) void fetchDayBundle(d);
  });
  return () => cancelIdle(handle);
}
