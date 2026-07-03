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
