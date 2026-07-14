// Motion + gesture glue for the sliding day navigation.
//
// useStepSlide: a short directional entrance (content arrives from the side
// you stepped toward) applied when the period anchor changes via the stepper.
// Pure CSS class + keyframes (styles/base.css); the global .hem-reduce-motion
// rules collapse it like every other animation.
//
// useSwipe: horizontal touch swipe → callbacks. Passive listeners only (no
// preventDefault), so ECharts tooltips and vertical page scroll keep working;
// the direction/velocity thresholds keep chart-scrubbing from triggering it.
import { useEffect, useRef, useState } from "preact/hooks";
import type { RefObject } from "preact";
import { lastStepDir } from "./period";
import { liveWindow, type LiveWindowBounds } from "./liveWindow";
import { isCoarsePointer } from "./charts";

export function useStepSlide(anchor: string): string {
  const [cls, setCls] = useState("");
  const prev = useRef(anchor);
  useEffect(() => {
    if (prev.current === anchor) return;
    prev.current = anchor;
    const dir = lastStepDir.value;
    if (!dir) return;
    setCls(dir === -1 ? "step-slide-left" : "step-slide-right");
    const t = setTimeout(() => setCls(""), 320);
    return () => clearTimeout(t);
  }, [anchor]);
  return cls;
}

export function useSwipe(
  ref: RefObject<HTMLElement>,
  onLeft: () => void,
  onRight: () => void,
): void {
  const cbs = useRef({ onLeft, onRight });
  cbs.current = { onLeft, onRight };
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    let x0 = 0, y0 = 0, t0 = 0;
    const start = (e: TouchEvent) => {
      const t = e.touches[0];
      x0 = t.clientX; y0 = t.clientY; t0 = Date.now();
    };
    const end = (e: TouchEvent) => {
      const t = e.changedTouches[0];
      const dx = t.clientX - x0, dy = t.clientY - y0;
      // Fast, decisively-horizontal gesture only.
      if (Date.now() - t0 > 600) return;
      if (Math.abs(dx) < 56 || Math.abs(dx) < 2 * Math.abs(dy)) return;
      if (dx > 0) cbs.current.onRight(); else cbs.current.onLeft();
    };
    el.addEventListener("touchstart", start, { passive: true });
    el.addEventListener("touchend", end, { passive: true });
    return () => {
      el.removeEventListener("touchstart", start);
      el.removeEventListener("touchend", end);
    };
  }, [ref]);
}

/**
 * Touch pan for the live-window charts (coarse pointers only). A HORIZONTAL
 * finger-drag pans the shared window into the past/future; a VERTICAL drag is
 * left alone so the page scrolls normally. We take over (preventDefault) ONLY
 * once the gesture is decisively horizontal — so we never block vertical scroll,
 * but we do stop a horizontal drag from triggering browser back/overscroll.
 *
 * Writes straight to the `liveWindow` signal (follow:false); the chart's
 * useLiveWindow applies it. No ECharts dataZoom is involved on touch, so there's
 * no double-handling and no touch-roam eating the page scroll.
 */
export function useChartPan(
  ref: RefObject<HTMLElement>,
  boundsRef: RefObject<LiveWindowBounds | null>,
): void {
  useEffect(() => {
    const el = ref.current;
    // Coarse pointers only — on a fine/hybrid pointer the inside dataZoom owns
    // pan, and attaching here too would double-handle a touchscreen drag.
    if (!el || !isCoarsePointer()) return;
    let x0 = 0, y0 = 0, w0s = 0, w0e = 0, mode = 0; // 0 undecided, 1 horiz, 2 vert
    const start = (e: TouchEvent) => {
      const t = e.touches[0];
      x0 = t.clientX; y0 = t.clientY; mode = 0;
      const lw = liveWindow.value; w0s = lw.startMs; w0e = lw.endMs;
    };
    const move = (e: TouchEvent) => {
      if (e.touches.length !== 1) return; // two-finger → leave (pinch/none)
      const t = e.touches[0];
      const dx = t.clientX - x0, dy = t.clientY - y0;
      if (mode === 0) {
        if (Math.abs(dx) < 8 && Math.abs(dy) < 8) return;
        mode = Math.abs(dx) > Math.abs(dy) ? 1 : 2;
      }
      if (mode !== 1) return;         // vertical → let the page scroll
      const b = boundsRef.current;
      if (!b || !w0s || !w0e) return;
      e.preventDefault();             // horizontal → we own it
      const span = w0e - w0s;
      const timePerPx = span / el.getBoundingClientRect().width;
      let ns = w0s - dx * timePerPx;  // drag right → reveal earlier
      let ne = w0e - dx * timePerPx;
      if (ns < b.dayStartMs) { ns = b.dayStartMs; ne = ns + span; }
      if (ne > b.dayEndMs) { ne = b.dayEndMs; ns = ne - span; }
      liveWindow.value = { startMs: ns, endMs: ne, follow: false };
    };
    el.addEventListener("touchstart", start, { passive: true });
    el.addEventListener("touchmove", move, { passive: false }); // preventDefault on horiz
    return () => {
      el.removeEventListener("touchstart", start);
      el.removeEventListener("touchmove", move);
    };
  }, [ref]);
}
