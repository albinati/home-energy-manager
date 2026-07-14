import { useEffect, useRef, useState } from "preact/hooks";
import { signal } from "@preact/signals-core";
import { useComputed } from "@preact/signals";

// Count of in-flight tracked fetches (opts.track). A page can render ONE shared
// "updating…" affordance off this so independent per-card fetches with very
// different latencies read as a single coordinated refresh instead of a
// piecemeal, half-stale page (the Insights navigation jank).
const _inflight = signal(0);

/** Reactive count of tracked fetches currently in flight. */
export function useInflight(): number {
  return useComputed(() => _inflight.value).value;
}

// usePoll runs `fn` every `intervalMs` while the document is visible.
// It pauses on visibilitychange → hidden and resumes on visible.
// State returned: { data, error, loading, refresh }.

export interface PollState<T> {
  data: T | null;
  error: Error | null;
  loading: boolean;
  refresh: () => Promise<void>;
  // Epoch ms of the last successful fetch (for "next refresh in Ns" UIs).
  lastFetchAt: number | null;
  intervalMs: number;
  // Consecutive failures since the last success (0 when healthy). Lets a widget
  // (or a page-level cue) show "reconnecting…" and, with lastFetchAt, decide the
  // on-screen data is stale.
  failCount: number;
}

// Error backoff: a hard-down endpoint must not be hammered at full rate forever.
// On each consecutive failure the next attempt is delayed by
// interval·2^fails, capped, with jitter to de-sync many failing polls.
const MAX_BACKOFF_MS = 120_000;
function backoffDelay(intervalMs: number, fails: number): number {
  const base = Math.min(intervalMs * 2 ** fails, MAX_BACKOFF_MS);
  return base + Math.floor(Math.random() * Math.min(1000, base * 0.25));
}

export function usePoll<T>(
  fn: () => Promise<T>,
  intervalMs: number,
  deps: ReadonlyArray<unknown> = [],
): PollState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [lastFetchAt, setLastFetchAt] = useState<number | null>(null);
  const [failCount, setFailCount] = useState<number>(0);
  const fnRef = useRef(fn);
  fnRef.current = fn;
  // Live count read by tick() for the backoff delay (state is snapshotted in the
  // closure, so keep the authoritative value in a ref).
  const failRef = useRef(0);

  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  // Returns true on success — tick() uses it to pick the next delay.
  const refresh = useRef(async (): Promise<boolean> => {
    try {
      const next = await fnRef.current();
      if (!mountedRef.current) return true;
      setData(next);
      setLastFetchAt(Date.now());
      setError(null);
      failRef.current = 0;
      setFailCount(0);
      return true;
    } catch (e) {
      if (!mountedRef.current) return false;
      setError(e instanceof Error ? e : new Error(String(e)));
      failRef.current += 1;
      setFailCount(failRef.current);
      return false;
    } finally {
      if (mountedRef.current) setLoading(false);
    }
  }).current;

  useEffect(() => {
    let timer: number | null = null;
    let stopped = false;

    const tick = async () => {
      if (stopped) return;
      const ok = await refresh();
      if (stopped) return;
      // Healthy → the configured cadence; failing → exponential backoff so a
      // dead endpoint isn't hammered (the last good data stays on screen and
      // failCount lets the consumer flag it stale).
      const delay = ok ? intervalMs : backoffDelay(intervalMs, failRef.current);
      timer = window.setTimeout(tick, delay);
    };

    const onVisibility = () => {
      if (document.visibilityState === "visible") {
        // immediate refresh on return + restart loop
        if (timer != null) window.clearTimeout(timer);
        tick();
      } else if (timer != null) {
        window.clearTimeout(timer);
        timer = null;
      }
    };

    tick();
    document.addEventListener("visibilitychange", onVisibility);

    return () => {
      stopped = true;
      if (timer != null) window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisibility);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs, ...deps]);

  const refreshVoid = useRef(async () => {
    await refresh();
  }).current;
  return { data, error, loading, refresh: refreshVoid, lastFetchAt, intervalMs, failCount };
}

// Immutable response cache for COMPLETED past periods. "Past is past" — a
// period that doesn't contain today never changes, so once fetched we keep it
// forever (module-level Map → survives the unmount/remount that route changes
// cause). The CURRENT period is never cached (callers pass immutable=false when
// isCurrentPeriod), so live data always refreshes. Keyed by a caller-supplied
// (endpoint, gran, anchor) string.
const _immutableCache = new Map<string, unknown>();

/** Read a value the caller previously stored via {@link setImmutableCache}.
 * Used by hand-rolled fetch effects (e.g. EnergyChartWidget) that can't use
 * the useFetch cache path. */
export function getImmutableCache<T>(key: string | null | undefined): T | undefined {
  return key ? (_immutableCache.get(key) as T | undefined) : undefined;
}

/** Store a value for a completed past period. No-op for a null key. */
export function setImmutableCache(key: string | null | undefined, value: unknown): void {
  if (key) _immutableCache.set(key, value);
}

export interface FetchOpts {
  // Stable key for the immutable cache, e.g. `fair:${gran}:${anchor}`.
  cacheKey?: string | null;
  // Only cache + serve-from-cache when true (caller passes !isCurrentPeriod so
  // the live current period always refetches).
  immutable?: boolean;
  // Count this fetch in the shared in-flight tally (useInflight) so the page can
  // show one coordinated "updating…" cue. A cache hit never counts (no fetch).
  track?: boolean;
  // Refetch when the tab becomes visible again (default true). Set false for a
  // costly / side-effecting endpoint that should fire once per visit, not on
  // every tab return (e.g. GET /scheduler/status triggers a live Octopus fetch).
  refetchOnVisible?: boolean;
}

// Fetch-once hook for endpoints we don't poll. With `opts.immutable` + a
// `cacheKey` it serves a completed past period instantly from the module cache
// and never refetches it (see _immutableCache).
export function useFetch<T>(
  fn: () => Promise<T>,
  deps: ReadonlyArray<unknown> = [],
  opts: FetchOpts = {},
): PollState<T> {
  const cacheKey = opts.immutable ? (opts.cacheKey ?? null) : null;
  const initial = cacheKey != null ? (_immutableCache.get(cacheKey) as T | undefined) : undefined;

  const [data, setData] = useState<T | null>(initial ?? null);
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState<boolean>(initial === undefined);
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const keyRef = useRef(cacheKey);
  keyRef.current = cacheKey;
  const trackRef = useRef(!!opts.track);
  trackRef.current = !!opts.track;

  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  // Monotonic request generation: when deps change (or a visibility refetch
  // fires) while an older request is still in flight, the older response must
  // NOT overwrite the newer one — only the latest generation may commit state.
  const genRef = useRef(0);

  const refresh = useRef(async () => {
    // Immutable cache hit (past period already fetched) → serve instantly. No
    // network → never counts toward the in-flight tally.
    const k = keyRef.current;
    if (k != null && _immutableCache.has(k)) {
      if (!mountedRef.current) return;
      genRef.current++; // invalidate any slower in-flight fetch
      setData(_immutableCache.get(k) as T);
      setError(null);
      setLoading(false);
      return;
    }
    const gen = ++genRef.current;
    setLoading(true);
    const tracked = trackRef.current;
    if (tracked) _inflight.value++;
    try {
      const next = await fnRef.current();
      if (!mountedRef.current || gen !== genRef.current) return;
      setData(next);
      setError(null);
      if (keyRef.current != null) _immutableCache.set(keyRef.current, next);
    } catch (e) {
      if (!mountedRef.current || gen !== genRef.current) return;
      setError(e instanceof Error ? e : new Error(String(e)));
    } finally {
      // Always balances the increment, even if the component unmounted mid-fetch.
      if (tracked) _inflight.value--;
      // A stale generation must not clear the newer request's loading state.
      if (mountedRef.current && gen === genRef.current) setLoading(false);
    }
  }).current;

  const onVisibleRefetch = opts.refetchOnVisible !== false;
  useEffect(() => {
    refresh();
    // Refresh when the tab becomes visible again, so a page left open doesn't
    // show stale numbers on return. An immutable past-period cache hit inside
    // refresh() serves from memory (no network), so this never churns history —
    // only live (current-period / uncached) fetches actually re-hit the API.
    // Opt out (refetchOnVisible: false) for costly/side-effecting endpoints.
    if (!onVisibleRefetch) return;
    const onVisibility = () => {
      if (document.visibilityState === "visible") refresh();
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => document.removeEventListener("visibilitychange", onVisibility);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  return { data, error, loading, refresh, lastFetchAt: null, intervalMs: 0, failCount: 0 };
}

// useAfterPaint returns false on first render, then flips true once the browser
// is idle after the initial paint. Gate below-the-fold / non-critical fetches
// on it so the hero + live power paint first and the heavy Fox/Octopus calls
// (lifetime rollup, tariff comparison) stream in a beat later instead of
// competing with the critical above-the-fold data.
export function useAfterPaint(timeoutMs = 1500): boolean {
  const [ready, setReady] = useState(false);
  useEffect(() => {
    const w = window as unknown as {
      requestIdleCallback?: (cb: () => void, opts?: { timeout: number }) => number;
      cancelIdleCallback?: (id: number) => void;
    };
    if (w.requestIdleCallback) {
      const id = w.requestIdleCallback(() => setReady(true), { timeout: timeoutMs });
      return () => w.cancelIdleCallback?.(id);
    }
    const id = window.setTimeout(() => setReady(true), 200);
    return () => clearTimeout(id);
  }, [timeoutMs]);
  return ready;
}
