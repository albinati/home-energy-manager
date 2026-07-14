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

  // Apply a window to this chart. Desktop uses the inside dataZoom (cheap
  // dispatchAction, no series rebuild); touch has no dataZoom so the window is
  // the axis min/max — a tiny xAxis-only merge, also cheap.
  const applyWindow = (startMs: number, endMs: number) => {
    const chart = chartRef.current;
    if (!chart) return;
    programmatic.current = true;
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
  useEffect(() => {
    return liveWindow.subscribe((s) => {
      follow.value = s.follow;
      if (s.startMs && s.endMs) applyWindow(s.startMs, s.endMs);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // The follow tick — recentre on now every 10s while following & visible.
  useEffect(() => {
    let timer: number | null = null;
    let stopped = false;

    const tick = () => {
      if (stopped) return;
      const b = boundsRef.current;
      const s = liveWindow.value;
      if (b && s.follow) {
        const w = centerWindow(b.nowMs, b.dayStartMs, b.dayEndMs);
        if (w.startMs !== s.startMs || w.endMs !== s.endMs) {
          liveWindow.value = { ...w, follow: true };
        }
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

    // Seed the window on first mount if it's still empty.
    const b0 = boundsRef.current;
    if (b0 && !liveWindow.value.startMs) {
      liveWindow.value = { ...centerWindow(b0.nowMs, b0.dayStartMs, b0.dayEndMs), follow: true };
    }
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
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    const onZoom = () => {
      if (programmatic.current) return;
      const opt = chart.getOption() as { dataZoom?: Array<{ startValue?: number; endValue?: number }> };
      const dz = opt.dataZoom?.[0];
      if (dz?.startValue == null || dz?.endValue == null) return;
      liveWindow.value = { startMs: dz.startValue, endMs: dz.endValue, follow: false };
    };
    chart.on("datazoom", onZoom);
    return () => { chart.off("datazoom", onZoom); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chartRef.current]);

  return { follow: follow.value };
}
