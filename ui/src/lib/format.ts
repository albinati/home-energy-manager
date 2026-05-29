// Display formatters. Keep all locale-y / unit-y logic here so pages stay
// declarative. Numbers null/undefined → "—" so missing data renders cleanly.

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
  try {
    const d = new Date(iso);
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  } catch {
    return iso;
  }
}

export function relTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  try {
    const d = new Date(iso).getTime();
    if (!Number.isFinite(d)) return iso;
    const diffS = (Date.now() - d) / 1000;
    if (diffS < 60) return "just now";
    if (diffS < 3600) return Math.round(diffS / 60) + "m ago";
    if (diffS < 86400) return Math.round(diffS / 3600) + "h ago";
    return Math.round(diffS / 86400) + "d ago";
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
