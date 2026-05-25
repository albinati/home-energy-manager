import { useEffect, useRef, useState } from "preact/hooks";

// usePoll runs `fn` every `intervalMs` while the document is visible.
// It pauses on visibilitychange → hidden and resumes on visible.
// State returned: { data, error, loading, refresh }.

export interface PollState<T> {
  data: T | null;
  error: Error | null;
  loading: boolean;
  refresh: () => Promise<void>;
}

export function usePoll<T>(
  fn: () => Promise<T>,
  intervalMs: number,
  deps: ReadonlyArray<unknown> = [],
): PollState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
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

  return { data, error, loading, refresh };
}

// Fetch-once hook for endpoints we don't poll.
export function useFetch<T>(
  fn: () => Promise<T>,
  deps: ReadonlyArray<unknown> = [],
): PollState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
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
    setLoading(true);
    try {
      const next = await fnRef.current();
      if (!mountedRef.current) return;
      setData(next);
      setError(null);
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

  return { data, error, loading, refresh };
}
