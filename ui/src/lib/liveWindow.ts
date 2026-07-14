// Shared "live window" state for the cockpit intraday charts.
//
// The three timelines (Consumption, Generation, Heating) are stacked full-width
// so a given time reads straight down the screen. If one panned and the others
// didn't they'd desync — the vertical alignment is the whole point. So the
// window lives in ONE module-level signal and every chart pans/follows together.
//
// follow mode (default): each tick re-centres [now - HALF, now + HALF] and
//   advances the now-marker.
// browse mode: entered when the USER pans/zooms; auto-recentre stops so we don't
//   yank the view while they read the past. A "back to now" affordance re-enters
//   follow.
import { signal } from "@preact/signals";
import type { RefObject } from "preact";
import type { EChartsType } from "echarts/core";
import { useEffect, useRef } from "preact/hooks";
import { useSignal } from "@preact/signals";
import { SLOT_MS } from "./charts";

// Live-window half-width, CONTINUOUS in viewport width so it tightens smoothly
// as you resize (a single hard breakpoint felt like "nothing happens until I
// cross it"). The window is sized to keep each 30-min slot at least
// MIN_PX_PER_SLOT wide, clamped to [MIN_HALF, MAX_HALF]. So a wide desktop shows
// the full ±6h; a portrait phone tightens to ~±3h; and dragging the window edge
// narrows it the whole way, not in a jump.
const MIN_HALF_MS = 2.5 * 3600_000; // floor: ±2.5h (5h window, ~10 slots)
const MAX_HALF_MS = 6 * 3600_000;   // cap:  ±6h
const MIN_PX_PER_SLOT = 32;

export function halfWindowMs(): number {
  if (typeof window === "undefined") return MAX_HALF_MS;
  // innerWidth is a proxy for the chart's own width (full-width widget minus
  // page padding) — good enough to drive slot density.
  const fitSlots = Math.max(6, window.innerWidth / MIN_PX_PER_SLOT);
  const half = (fitSlots * SLOT_MS) / 2;
  return Math.max(MIN_HALF_MS, Math.min(MAX_HALF_MS, half));
}

export interface LiveWindowState {
  startMs: number;
  endMs: number;
  follow: boolean;
}

// startMs/endMs = 0 until the first chart initialises the bounds.
export const liveWindow = signal<LiveWindowState>({ startMs: 0, endMs: 0, follow: true });

/** Centre a HALF-width window on nowMs, clamped to stay inside [dayStart, dayEnd]
 *  near the day's edges (early morning / late evening). */
export function centerWindow(
  nowMs: number,
  dayStartMs: number,
  dayEndMs: number,
): { startMs: number; endMs: number } {
  const half = halfWindowMs();
  const span = 2 * half;
  // If the whole day is narrower than the window, just show the day.
  if (dayEndMs - dayStartMs <= span) return { startMs: dayStartMs, endMs: dayEndMs };
  let startMs = nowMs - half;
  let endMs = nowMs + half;
  if (startMs < dayStartMs) { startMs = dayStartMs; endMs = dayStartMs + span; }
  if (endMs > dayEndMs) { endMs = dayEndMs; startMs = dayEndMs - span; }
  return { startMs, endMs };
}

/** Re-enter follow mode (the "● now" chip / Today button call this). */
export function backToNow(): void {
  liveWindow.value = { ...liveWindow.value, follow: true };
}

export interface LiveWindowBounds {
  dayStartMs: number;
  dayEndMs: number;
  nowMs: number;
}

/** Read the chart's current visible x-window (timestamps). Prefers the inside
 *  dataZoom's startValue/endValue; falls back to the x-axis scale extent, which
 *  stays correct even if ECharts tracks a user roam as start/end percent and
 *  leaves startValue/endValue unpopulated. Returns null if neither is readable. */
function readChartWindow(chart: EChartsType): { startMs: number; endMs: number } | null {
  const opt = chart.getOption() as { dataZoom?: Array<{ startValue?: number; endValue?: number }> };
  const dz = opt.dataZoom?.[0];
  if (dz && dz.startValue != null && dz.endValue != null) {
    return { startMs: dz.startValue, endMs: dz.endValue };
  }
  try {
    const model = (chart as unknown as {
      getModel?: () => {
        getComponent?: (t: string, i: number) => { axis?: { scale?: { getExtent?: () => [number, number] } } };
      };
    }).getModel?.();
    const ext = model?.getComponent?.("xAxis", 0)?.axis?.scale?.getExtent?.();
    if (ext && Number.isFinite(ext[0]) && Number.isFinite(ext[1])) {
      return { startMs: ext[0], endMs: ext[1] };
    }
  } catch { /* internal API shape changed — fall through */ }
  return null;
}

/**
 * Per-chart adapter. Drives the chart's dataZoom window from the shared signal,
 * ticks it forward while following, and writes user gestures back into the
 * signal (dropping out of follow). Returns { follow } for the "● now" chip.
 *
 * `boundsRef` is read fresh each tick so the hook sees the latest now/day.
 */
export function useLiveWindow(
  chartRef: RefObject<EChartsType | null>,
  boundsRef: RefObject<LiveWindowBounds | null>,
): { follow: boolean } {
  const follow = useSignal(liveWindow.value.follow);
  // True while WE are calling dispatchAction, so the dataZoom listener can tell
  // a programmatic pan from a user pan.
  const programmatic = useRef(false);
  // The last window WE applied. A value-match backup for the microtask guard: if
  // ECharts emits the datazoom event on a later tick (guard already cleared), the
  // echo still matches this and is ignored — so a follow recentre can't be
  // misread as a user pan (which would wrongly drop follow).
  const lastApplied = useRef<{ startMs: number; endMs: number } | null>(null);

  // Apply a window to this chart. Desktop uses the inside dataZoom (cheap
  // dispatchAction, no series rebuild); touch has no dataZoom so the window is
  // the axis min/max — a tiny xAxis-only merge, also cheap.
  const applyWindow = (startMs: number, endMs: number) => {
    const chart = chartRef.current;
    if (!chart) return;
    programmatic.current = true;
    lastApplied.current = { startMs, endMs };
    const opt = chart.getOption() as { dataZoom?: unknown[] };
    if (Array.isArray(opt.dataZoom) && opt.dataZoom.length > 0) {
      chart.dispatchAction({ type: "dataZoom", startValue: startMs, endValue: endMs });
    } else {
      chart.setOption({ xAxis: { min: startMs, max: endMs } });
    }
    // Clear on the next microtask — after ECharts fires its dataZoom event.
    queueMicrotask(() => { programmatic.current = false; });
  };

  // React to shared-signal changes (another chart panned, or backToNow fired).
  // Only live-mode charts (boundsRef set) apply the window — a bar-mode chart on
  // a category axis must never receive epoch-ms min/max.
  useEffect(() => {
    return liveWindow.subscribe((s) => {
      follow.value = s.follow;
      if (boundsRef.current && s.startMs && s.endMs) applyWindow(s.startMs, s.endMs);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // The follow tick — recentre on now every 10s while following & visible.
  useEffect(() => {
    let timer: number | null = null;
    let stopped = false;

    let seedAttempts = 0;
    const tick = () => {
      if (stopped) return;
      const b = boundsRef.current;
      // The widget populates boundsRef in an effect that runs AFTER this hook's,
      // so on mount it's still null. Retry fast (150ms) for the first ~2s so the
      // window seeds promptly once bounds land — otherwise the signal stays
      // {0,0} for up to 10s (touch pan dead, charts unaligned). A bar-mode chart
      // never gets bounds, so cap the fast phase and relax to 10s rather than
      // busy-loop forever.
      if (!b) {
        seedAttempts++;
        timer = window.setTimeout(tick, seedAttempts < 15 ? 150 : 10_000);
        return;
      }
      const s = liveWindow.value;
      const w = centerWindow(b.nowMs, b.dayStartMs, b.dayEndMs);
      if (!s.startMs) {
        liveWindow.value = { ...w, follow: s.follow };            // initial seed
      } else if (s.follow && (w.startMs !== s.startMs || w.endMs !== s.endMs)) {
        liveWindow.value = { ...w, follow: true };                // advance while following
      }
      timer = window.setTimeout(tick, 10_000);
    };

    const onVis = () => {
      if (document.visibilityState === "visible") {
        if (timer != null) window.clearTimeout(timer);
        tick();
      } else if (timer != null) {
        window.clearTimeout(timer);
        timer = null;
      }
    };

    // (Initial seed is handled by tick()'s !s.startMs branch as soon as bounds
    // land — see above.)
    // Re-fit the window on resize/rotation: the half-width shrinks continuously
    // with viewport, so this tightens the view as the user drags the edge — but
    // only while following, so a resize never yanks someone browsing the past.
    // Debounced (a drag-resize fires many events) via a short timeout.
    let resizeTimer: number | null = null;
    const onResize = () => {
      if (resizeTimer != null) window.clearTimeout(resizeTimer);
      resizeTimer = window.setTimeout(() => {
        const b = boundsRef.current;
        if (b && liveWindow.value.follow) {
          liveWindow.value = { ...centerWindow(b.nowMs, b.dayStartMs, b.dayEndMs), follow: true };
        }
      }, 120);
    };

    tick();
    document.addEventListener("visibilitychange", onVis);
    window.addEventListener("resize", onResize);
    return () => {
      stopped = true;
      if (timer != null) window.clearTimeout(timer);
      if (resizeTimer != null) window.clearTimeout(resizeTimer);
      document.removeEventListener("visibilitychange", onVis);
      window.removeEventListener("resize", onResize);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Listen for USER dataZoom (drag/wheel/pinch) → drop out of follow, remember
  // the window. Ignore our own dispatched pans (programmatic guard).
  //
  // The chart is created imperatively in a LATER widget effect, so it's null when
  // this effect first runs. A `[chartRef.current]` dep can't fix that (a ref
  // mutation doesn't re-run effects), and would leave the listener unattached
  // until an unrelated re-render — so a desktop pan before the next poll would be
  // silently discarded. Instead retry-attach until the chart exists, once.
  useEffect(() => {
    let attached: EChartsType | null = null;
    let retry: number | null = null;
    const onZoom = () => {
      if (programmatic.current) return;
      const chart = chartRef.current;
      if (!chart) return;
      const w = readChartWindow(chart);
      if (!w) return;
      // Value-match backup for the microtask guard (see lastApplied): a follow
      // recentre's echo matches within snap error → not a user pan.
      const la = lastApplied.current;
      if (la && Math.abs(w.startMs - la.startMs) < 5_000 && Math.abs(w.endMs - la.endMs) < 5_000) return;
      liveWindow.value = { startMs: w.startMs, endMs: w.endMs, follow: false };
    };
    const attach = () => {
      const chart = chartRef.current;
      if (chart) { chart.on("datazoom", onZoom); attached = chart; return; }
      retry = window.setTimeout(attach, 100);
    };
    attach();
    return () => {
      if (retry != null) window.clearTimeout(retry);
      if (attached) attached.off("datazoom", onZoom);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return { follow: follow.value };
}
