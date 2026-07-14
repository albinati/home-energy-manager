// Display formatters. Keep all locale-y / unit-y logic here so pages stay
// declarative. Numbers null/undefined → "—" so missing data renders cleanly.
import { agoLabel } from "./freshness";

const nbsp = " ";

export function kw(n: number | null | undefined, digits = 2): string {
  if (n == null || !Number.isFinite(n)) return "—";
  return n.toFixed(digits) + nbsp + "kW";
}

export function kwh(n: number | null | undefined, digits = 1): string {
  if (n == null || !Number.isFinite(n)) return "—";
  return n.toFixed(digits) + nbsp + "kWh";
}

export function watts(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "—";
  if (Math.abs(n) >= 1000) return (n / 1000).toFixed(2) + nbsp + "kW";
  return Math.round(n) + nbsp + "W";
}

export function pct(n: number | null | undefined, digits = 0): string {
  if (n == null || !Number.isFinite(n)) return "—";
  return n.toFixed(digits) + "%";
}

export function tempC(n: number | null | undefined, digits = 1): string {
  if (n == null || !Number.isFinite(n)) return "—";
  return n.toFixed(digits) + nbsp + "°C";
}

export function pence(n: number | null | undefined, digits = 1): string {
  if (n == null || !Number.isFinite(n)) return "—";
  return n.toFixed(digits) + nbsp + "p";
}

export function gbp(n: number | null | undefined, digits = 2): string {
  if (n == null || !Number.isFinite(n)) return "—";
  const sign = n < 0 ? "−" : "";
  return sign + "£" + Math.abs(n).toFixed(digits);
}

export function gbpSigned(n: number | null | undefined, digits = 2): string {
  if (n == null || !Number.isFinite(n)) return "—";
  const sign = n >= 0 ? "+" : "−";
  return sign + "£" + Math.abs(n).toFixed(digits);
}

export function hhmm(iso: string | null | undefined): string {
  if (!iso) return "—";
  // new Date(bad) never throws — it yields an Invalid Date whose toLocale*
  // renders "Invalid Date". Guard on the time value instead of try/catch.
  const d = new Date(iso);
  if (!Number.isFinite(d.getTime())) return iso;
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
}

/** Local day header for journal-style day grouping, e.g. "Sun, 12 Jul".
 *  Falls back to the raw date part for an unparseable timestamp. */
export function localDay(iso: string): string {
  const d = new Date(iso);
  if (!Number.isFinite(d.getTime())) return iso.slice(0, 10);
  return d.toLocaleDateString([], { weekday: "short", day: "2-digit", month: "short" });
}

/** Group already-sorted rows into consecutive local-day buckets (journal lists).
 *  Preserves the input order; a day that reappears later starts a new group. */
export function groupByDay<T>(items: T[], getIso: (item: T) => string): { day: string; items: T[] }[] {
  const groups: { day: string; items: T[] }[] = [];
  for (const item of items) {
    const day = localDay(getIso(item));
    const last = groups[groups.length - 1];
    if (last && last.day === day) last.items.push(item);
    else groups.push({ day, items: [item] });
  }
  return groups;
}

export function relTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    const d = new Date(iso).getTime();
    if (!Number.isFinite(d)) return iso;
    // Single-sourced wording + thresholds (freshness.ts) so every "N ago" in the
    // app agrees (the audit found four different "now/fresh" cutoffs).
    return agoLabel(Math.max(0, Date.now() - d));
  } catch {
    return iso;
  }
}

export function slotKindLabel(kind: string | null | undefined): string {
  switch ((kind || "").toLowerCase()) {
    case "negative": return "Negative price";
    case "cheap": return "Cheap";
    case "solar_charge": return "Solar charge";
    case "solar_preheat": return "Solar pre-heat";
    case "standard": return "Standard";
    case "peak": return "Peak";
    case "peak_export": return "Peak export";
    case "idle": return "Idle";
    default: return kind || "—";
  }
}

export function slotKindColorVar(kind: string | null | undefined): string {
  switch ((kind || "").toLowerCase()) {
    case "negative": return "var(--neg-price)";
    case "cheap":
    case "solar_charge":
    case "solar_preheat": return "var(--cheap)";
    case "peak": return "var(--peak)";
    case "peak_export": return "var(--peak-export)";
    default: return "var(--standard)";
  }
}
