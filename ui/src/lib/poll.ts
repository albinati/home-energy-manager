import { useEffect, useRef, useState } from "preact/hooks";

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
  const fnRef = useRef(fn);
  fnRef.current = fn;

  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const refresh = useRef(async () => {
    try {
      const next = await fnRef.current();
      if (!mountedRef.current) return;
      setData(next);
      setLastFetchAt(Date.now());
      setError(null);
    } catch (e) {
      if (!mountedRef.current) return;
      setError(e instanceof Error ? e : new Error(String(e)));
    } finally {
      if (mountedRef.current) setLoading(false);
    }
  }).current;

  useEffect(() => {
    let timer: number | null = null;
    let stopped = false;

    const tick = async () => {
      if (stopped) return;
      await refresh();
      if (stopped) return;
      timer = window.setTimeout(tick, intervalMs);
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

  return { data, error, loading, refresh, lastFetchAt, intervalMs };
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

  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const refresh = useRef(async () => {
    // Immutable cache hit (past period already fetched) → serve instantly.
    const k = keyRef.current;
    if (k != null && _immutableCache.has(k)) {
      if (!mountedRef.current) return;
      setData(_immutableCache.get(k) as T);
      setError(null);
      setLoading(false);
      return;
    }
    setLoading(true);
    try {
      const next = await fnRef.current();
      if (!mountedRef.current) return;
      setData(next);
      setError(null);
      if (keyRef.current != null) _immutableCache.set(keyRef.current, next);
    } catch (e) {
      if (!mountedRef.current) return;
      setError(e instanceof Error ? e : new Error(String(e)));
    } finally {
      if (mountedRef.current) setLoading(false);
    }
  }).current;

  useEffect(() => {
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  return { data, error, loading, refresh, lastFetchAt: null, intervalMs: 0 };
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
