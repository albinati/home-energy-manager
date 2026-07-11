// Shared helpers for reading the committed dispatch plan (Fox battery windows)
// out of the scheduler timeline. Used by both the Live-power "next" glance and
// the standalone Plan widget so the kind/label/colour vocabulary stays in sync.
import type { SchedulerTimeline, TimelineSlot } from "./types";

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

// ── "Why now" — a single glanceable sentence for the Live power card that
// explains the CURRENT battery/inverter state and the reason behind it. Pure
// map over the ongoing timeline slot (lp_kind / dispatched_kind /
// decision_reason) + the live Fox mode. No fetch — the caller already polls
// both /scheduler/timeline and /cockpit/now. `tone` is a semantic colour
// token (never a literal): hold = neutral, import/cheap = --cheap,
// peak/export = --warn, solar = --ok.
export interface WhyNow {
  text: string;
  tone: string;   // css var(...) — a defined semantic token, no literal fallback
}

const WHY_TONE = {
  hold: "var(--text-dim)",
  cheap: "var(--cheap)",
  warn: "var(--warn)",
  ok: "var(--ok)",
} as const;

// Humanise a raw dispatch_decisions.reason into a short trailing clause. Only a
// small set of known machine tokens are surfaced; anything unrecognised is
// dropped rather than leaked verbatim onto a glanceable line.
function reasonClause(reason: string): string | null {
  const r = reason.toLowerCase();
  if (r.includes("charge_floor") || r.includes("charge floor") || r.includes("pess"))
    return "charge floor binding";
  if (r.includes("peak")) return "reserve for the evening peak";
  if (r.includes("reserve") || r.includes("backup")) return "battery held in reserve";
  if (r.includes("negative")) return "ahead of a negative-price window";
  return null;
}

export function whyNowPhrase(
  ongoing: TimelineSlot | null | undefined,
  foxMode: string | undefined,
): WhyNow | null {
  if (!ongoing && !foxMode) return null;
  const kind = (ongoing?.dispatched_kind || ongoing?.lp_kind || "").toLowerCase();
  const fox = (foxMode || "").toLowerCase().replace(/[_\s-]/g, "");
  const reason = (ongoing?.decision_reason || "").toLowerCase();
  const clause = reasonClause(reason);
  const with_ = (base: string) => (clause ? `${base} — ${clause}` : base);

  // Charging from free PV — the happiest state; green.
  if (kind === "solar_charge" || kind === "solar_preheat" || (fox !== "forcedischarge" && reason.includes("solar"))) {
    return { text: "Charging from solar", tone: WHY_TONE.ok };
  }
  // Force-discharge / exporting to the grid at a peak-export price; amber.
  if (fox === "forcedischarge" || kind === "peak_export" || kind === "pre_negative_export") {
    return { text: with_("Exporting to grid — peak export price"), tone: WHY_TONE.warn };
  }
  // Force-charge from the grid in a cheap or negative window; --cheap.
  if (fox === "forcecharge" || kind === "cheap" || kind === "negative") {
    const paid = kind === "negative" || reason.includes("negative");
    return {
      text: paid ? "Charging — paid to import (negative price)" : with_("Importing — charging in a cheap window"),
      tone: WHY_TONE.cheap,
    };
  }
  // Battery held in reserve (Backup) — neutral, usually ahead of the peak.
  if (fox === "backup") {
    return { text: `Holding — ${clause ?? "battery held in reserve"}`, tone: WHY_TONE.hold };
  }
  // Self-use — the resting state, battery covering household load; neutral.
  return { text: "Self-use — covering the house from the battery", tone: WHY_TONE.hold };
}
