// Shared data-freshness signal.
//
// Landing already polls /cockpit/now every 20s and that payload carries a
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
