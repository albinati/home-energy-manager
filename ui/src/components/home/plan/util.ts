import type { PlanWindowStatus } from "../../../lib/types";

/** done / ongoing / upcoming for a [start,end) UTC window against `nowUtc`. */
export function windowStatus(startUtc: string, endUtc: string, nowUtc: string): PlanWindowStatus {
  const n = Date.parse(nowUtc);
  const s = Date.parse(startUtc);
  const e = Date.parse(endUtc);
  if (!Number.isFinite(n) || !Number.isFinite(s) || !Number.isFinite(e)) return "upcoming";
  if (n >= e) return "done";
  if (n >= s) return "ongoing";
  return "upcoming";
}

export const num = (v: number | null | undefined, dp = 1): string =>
  v == null || !Number.isFinite(v) ? "—" : v.toFixed(dp);

export const signed = (v: number | null | undefined, dp = 0): string =>
  v == null ? "—" : `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toFixed(dp)}`;

export const range = (a: string, b: string): string => `${a}–${b}`;

/** "13" -> "13:00" for the policy's integer local hours. */
export const hh = (h: number | null | undefined): string =>
  h == null ? "—" : `${String(h).padStart(2, "0")}:00`;
