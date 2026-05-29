// Motion preference. The signature power-flow / hero / chart animations are the
// point of this UI, but they were gated purely on the OS `prefers-reduced-
// motion` — so a desktop with "Reduce Motion" on (often enabled unknowingly,
// or via battery savers) froze the whole experience. This adds an explicit
// override stored in localStorage, defaulting to ON for this personal
// dashboard. "auto" follows the OS; "off" forces reduced.
export type MotionPref = "on" | "auto" | "off";

const KEY = "hem.motion";

export function motionPref(): MotionPref {
  try {
    const v = localStorage.getItem(KEY);
    if (v === "on" || v === "auto" || v === "off") return v;
  } catch { /* ignore */ }
  return "on"; // default: motion on (overrides OS reduce-motion)
}

export function setMotionPref(p: MotionPref): void {
  try { localStorage.setItem(KEY, p); } catch { /* ignore */ }
  applyMotionClass();
}

function osReduced(): boolean {
  return typeof window !== "undefined"
    && typeof window.matchMedia === "function"
    && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

// The single source of truth used by every animation gate (JS + CSS class).
export function reducedMotion(): boolean {
  const p = motionPref();
  if (p === "on") return false;
  if (p === "off") return true;
  return osReduced(); // "auto"
}

// Toggle the html class so the CSS reduce-motion rules apply only when OUR
// logic says reduced — not whenever the OS says so (which the override beats).
export function applyMotionClass(): void {
  if (typeof document === "undefined") return;
  document.documentElement.classList.toggle("hem-reduce-motion", reducedMotion());
}
