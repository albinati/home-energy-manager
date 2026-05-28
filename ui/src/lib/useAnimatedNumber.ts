import { useEffect, useRef, useState } from "preact/hooks";

// Smoothly tween a numeric value when it changes — for the hero counters
// and lifetime stats so refreshes feel alive instead of snapping. Uses
// requestAnimationFrame + easeOutCubic for a financial-app feel (fast
// start, gentle settle). When the value is null the hook returns null;
// when it transitions from null → number it snaps in (no "from-zero"
// blow-up on first mount).
export function useAnimatedNumber(
  target: number | null | undefined,
  durationMs: number = 700,
): number | null {
  const [display, setDisplay] = useState<number | null>(target ?? null);
  const fromRef = useRef<number>(target ?? 0);
  const startRef = useRef<number>(0);
  const rafRef = useRef<number | null>(null);
  const lastTargetRef = useRef<number | null | undefined>(target);

  useEffect(() => {
    // null/undefined → snap to null and stop any in-flight tween
    if (target == null) {
      if (rafRef.current != null) cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
      setDisplay(null);
      lastTargetRef.current = target;
      return;
    }
    // First-non-null mount — snap to value, no tween
    if (lastTargetRef.current == null) {
      setDisplay(target);
      fromRef.current = target;
      lastTargetRef.current = target;
      return;
    }
    // Identical value — no work
    if (lastTargetRef.current === target) return;

    fromRef.current = display ?? lastTargetRef.current ?? target;
    startRef.current = performance.now();
    lastTargetRef.current = target;

    const tick = (now: number) => {
      const elapsed = now - startRef.current;
      const t = Math.min(1, elapsed / durationMs);
      // easeOutCubic — quick start, gentle finish
      const eased = 1 - Math.pow(1 - t, 3);
      const next = fromRef.current + (target - fromRef.current) * eased;
      setDisplay(next);
      if (t < 1) {
        rafRef.current = requestAnimationFrame(tick);
      } else {
        rafRef.current = null;
      }
    };
    if (rafRef.current != null) cancelAnimationFrame(rafRef.current);
    rafRef.current = requestAnimationFrame(tick);

    return () => {
      if (rafRef.current != null) cancelAnimationFrame(rafRef.current);
    };
    // We intentionally exclude `display` from deps — including it would
    // re-arm the effect on every animation frame.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target, durationMs]);

  return display;
}
