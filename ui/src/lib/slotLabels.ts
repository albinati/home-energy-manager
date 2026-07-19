// Shared time-window label helpers for schedule badges (battery force-charge
// windows, DHW tank windows). Extracted from LivePowerWidget so the Heating
// widget + cockpit can render the same "Tomorrow 14:00–22:00" style.

export interface RelativeSlot {
  dayLabel: string;   // "" (today) | "Tomorrow" | "Mon" | "12 Jun"
  timeLabel: string;  // "14:00"
  isToday: boolean;
}

/** End time = the given slot/window-end ISO rendered as HH:MM.
 * For battery windows pass the LAST slot's START (we add 30 min); for tank
 * windows pass the row's end_utc directly (set addSlot=false). */
export function endLabelFor(iso: string | undefined, addSlot = true): string {
  if (!iso) return "?";
  try {
    const t = new Date(iso).getTime() + (addSlot ? 30 * 60 * 1000 : 0);
    return new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  } catch {
    return "?";
  }
}

// Tank (DHW) action vocabulary — shared by the tank schedule chips and the
// Live-power "next action" summary so they always speak the same words/kinds.
export function tankKindOf(action?: string | null): "warmup" | "setback" | "boost" {
  if (action === "tank_setback") return "setback";
  if (action === "tank_negative_boost") return "boost";
  return "warmup";
}
export function tankLabelOf(action?: string | null): string {
  switch (action) {
    case "tank_setback": return "Setback";
    case "tank_negative_boost": return "Boost (neg)";
    case "legionella_cycle": return "Legionella (firmware)";
    case "tank_warmup": return "Warmup";
    default: return action || "—";
  }
}

export function formatRelativeSlot(iso: string | undefined, nowIso?: string | null): RelativeSlot {
  if (!iso) return { dayLabel: "", timeLabel: "—", isToday: false };
  try {
    const slot = new Date(iso);
    const now = nowIso ? new Date(nowIso) : new Date();
    const timeLabel = slot.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
    const slotKey = `${slot.getFullYear()}-${slot.getMonth()}-${slot.getDate()}`;
    const nowKey = `${now.getFullYear()}-${now.getMonth()}-${now.getDate()}`;
    if (slotKey === nowKey) return { dayLabel: "", timeLabel, isToday: true };
    const dayDiff = Math.round(
      (Date.UTC(slot.getFullYear(), slot.getMonth(), slot.getDate())
        - Date.UTC(now.getFullYear(), now.getMonth(), now.getDate())) / 86400000,
    );
    if (dayDiff === 1) return { dayLabel: "Tomorrow", timeLabel, isToday: false };
    if (dayDiff > 1 && dayDiff < 7) {
      return { dayLabel: slot.toLocaleDateString([], { weekday: "short" }), timeLabel, isToday: false };
    }
    return {
      dayLabel: slot.toLocaleDateString([], { day: "2-digit", month: "short" }),
      timeLabel, isToday: false,
    };
  } catch {
    return { dayLabel: "", timeLabel: iso, isToday: false };
  }
}
