// Shared helpers for reading the committed dispatch plan (Fox battery windows)
// out of the scheduler timeline. Used by both the Live-power "next" glance and
// the standalone Plan widget so the kind/label/colour vocabulary stays in sync.
import type { SchedulerTimeline } from "./types";

export interface ForcedWindow {
  kind: string;
  start_utc: string;
  end_utc: string;
  slot_count: number;
}

// Collapse the planned slots into contiguous "interesting" battery windows
// (force-charge on cheap/negative, force-discharge on peak_export, drain ahead
// of a negative window), oldest-first, capped at `limit`.
export function upcomingForcedWindows(timeline: SchedulerTimeline, limit: number): ForcedWindow[] {
  const out: ForcedWindow[] = [];
  const interesting = new Set(["cheap", "negative", "peak_export", "pre_negative_export"]);
  let current: ForcedWindow | null = null;
  for (const s of timeline.planned || []) {
    const k = (s.dispatched_kind || s.lp_kind || "").toLowerCase();
    const iso = s.slot_time_utc;
    if (!iso) continue;
    if (interesting.has(k)) {
      if (current && current.kind === k) {
        current.end_utc = iso;
        current.slot_count += 1;
      } else {
        if (current) out.push(current);
        current = { kind: k, start_utc: iso, end_utc: iso, slot_count: 1 };
      }
    } else if (current) {
      out.push(current);
      current = null;
      if (out.length >= limit) break;
    }
  }
  if (current) out.push(current);
  return out.slice(0, limit);
}

// Battery window kind → the same semantic colour the rest of the surface uses
// (charge = ok/green, discharge/drain = warn/amber).
export function kindColorVar(k: string | undefined): string {
  switch ((k || "").toLowerCase()) {
    case "cheap":
    case "negative":            return "var(--ok)";
    case "peak_export":
    case "pre_negative_export": return "var(--warn)";
    default:                    return "var(--text-dim)";
  }
}

// Kind conveyed by the leading coloured dot + band colour — no emoji.
export function labelForKind(k: string | undefined): string {
  switch ((k || "").toLowerCase()) {
    case "cheap":         return "Force charge";
    case "negative":      return "Force charge";
    case "peak_export":   return "Force discharge";
    case "pre_negative_export": return "Drain (pre-neg)";
    default:              return k || "?";
  }
}
