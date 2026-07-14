// Shared data-freshness signal.
//
// Landing already polls /cockpit/now every 10s and that payload carries a
// per-source freshness map (fox/daikin/agile/...). The AlertStrip lives in
// the app shell (all routes) and wants the same staleness info — publishing
// the latest map through a signal lets it read freshness WITHOUT a duplicate
// /cockpit/now poll. Off the cockpit route the signal simply goes stale/null
// and the strip skips the staleness chip (no data ≠ alarm).
import { signal } from "@preact/signals";
import type { FreshnessEntry } from "./types";

export const cockpitFreshness = signal<Record<string, FreshnessEntry> | null>(null);

// Epoch ms of the last publish — lets readers ignore a map that itself is
// old (e.g. user navigated away from the cockpit and the poll stopped).
export const cockpitFreshnessAt = signal<number | null>(null);

export function publishFreshness(map: Record<string, FreshnessEntry> | undefined | null): void {
  cockpitFreshness.value = map ?? null;
  cockpitFreshnessAt.value = map ? Date.now() : null;
}

// Connection health of the fast cockpit poll (its failCount + last success).
// When the poll itself is failing, no per-source freshness map arrives at all —
// the audit's "silent stale on error" gap. Publishing this lets the page-level
// AlertStrip say "reconnecting…" instead of showing frozen numbers with no cue.
export interface CockpitConn { failCount: number; lastFetchAt: number | null; }
export const cockpitConn = signal<CockpitConn | null>(null);

export function publishCockpitConn(c: CockpitConn | null): void {
  cockpitConn.value = c;
}

// --- Single source of truth for "how fresh / how old" ---------------------
//
// The audit found seven idioms and no shared threshold — "now/fresh" was
// variously 60 s, 90 s, 30 min, or a backend window. These are THE boundaries;
// every "ago" label and freshness tone should read from here so the whole UI
// agrees.
export const FRESH_MS = 90_000;      // ≤ this reads as "just now"
export const STALE_MS = 5 * 60_000;  // > this is visibly stale (amber)

export type FreshnessTone = "live" | "aging" | "stale";

/** Age of an epoch-ms timestamp in ms (Infinity when absent). */
export function ageMs(atMs: number | null | undefined): number {
  return atMs == null ? Infinity : Math.max(0, Date.now() - atMs);
}

/** One consistent tone from an age. live ≤ FRESH, aging ≤ STALE, else stale. */
export function toneForAge(age: number): FreshnessTone {
  if (age <= FRESH_MS) return "live";
  if (age <= STALE_MS) return "aging";
  return "stale";
}

/** One consistent relative-age label. Used by relTime + the page cue so the
 *  wording and thresholds never drift apart again. */
export function agoLabel(age: number): string {
  if (!Number.isFinite(age)) return "—";
  if (age < FRESH_MS) return "just now";
  const s = age / 1000;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}
