import type { DhwScheduleRow } from "../../lib/types";
import { formatRelativeSlot, endLabelFor } from "../../lib/slotLabels";
import "./tank-badges.css";

// Heat-tank scheduling badges — "Warmup 13:00–22:00 · 45°C",
// "Boost (neg) 02:00–04:00 · 65°C" — mirroring the battery force-charge flags.
// Each dhw_policy row IS already a coalesced window (start/end/target), so no
// merge is needed. Future-day rows render dimmed/dashed.

function kindOf(a?: string | null): string {
  if (a === "tank_setback") return "setback";
  if (a === "tank_negative_boost") return "boost";
  return "warmup";
}
function labelOf(a?: string | null): string {
  switch (a) {
    case "tank_setback": return "Setback";
    case "tank_negative_boost": return "Boost (neg)";
    case "tank_warmup": return "Warmup";
    default: return a || "—";
  }
}

interface Props {
  rows: DhwScheduleRow[] | null | undefined;
  nowIso?: string | null;
  limit?: number;
}

export function TankScheduleBadges({ rows, nowIso, limit }: Props) {
  const items = (rows || []).filter((r) => r.start_utc).slice(0, limit ?? 99);
  if (!items.length) return null;
  return (
    <span class="tank-windows">
      {items.map((r, i) => {
        const k = kindOf(r.action_type);
        const start = formatRelativeSlot(r.start_utc ?? undefined, nowIso);
        const end = endLabelFor(r.end_utc ?? undefined, false);
        const range = `${start.timeLabel}–${end}`;
        const temp = r.tank_temp_c != null ? ` · ${r.tank_temp_c}°C` : "";
        const dayPfx = start.dayLabel ? `${start.dayLabel} ` : "";
        return (
          <span key={i}
                class={`tank-window tank-window--${k}${start.isToday ? "" : " tank-window--future"}`}
                title={`${labelOf(r.action_type)} ${dayPfx}${range}${temp}`}>
            <span class="tank-window-dot" />
            {labelOf(r.action_type)} {dayPfx}{range}{temp}
          </span>
        );
      })}
    </span>
  );
}
