import type { DhwScheduleRow } from "../../lib/types";
import { formatRelativeSlot, endLabelFor, tankKindOf, tankLabelOf } from "../../lib/slotLabels";
import "./tank-badges.css";

// Heat-tank scheduling badges — "Warmup 13:00–22:00 · 45°C",
// "Boost (neg) 02:00–04:00 · 65°C" — mirroring the battery force-charge flags.
// Each dhw_policy row IS already a coalesced window (start/end/target), so no
// merge is needed. Future-day rows render dimmed/dashed.
// kind/label vocabulary is shared via lib/slotLabels (tankKindOf/tankLabelOf).

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
        const k = tankKindOf(r.action_type);
        const start = formatRelativeSlot(r.start_utc ?? undefined, nowIso);
        const end = endLabelFor(r.end_utc ?? undefined, false);
        const range = `${start.timeLabel}–${end}`;
        const temp = r.tank_temp_c != null ? ` · ${r.tank_temp_c}°C` : "";
        const dayPfx = start.dayLabel ? `${start.dayLabel} ` : "";
        return (
          <span key={i}
                class={`tank-window tank-window--${k}${start.isToday ? "" : " tank-window--future"}`}
                title={`${tankLabelOf(r.action_type)} ${dayPfx}${range}${temp}`}>
            <span class="tank-window-dot" />
            {tankLabelOf(r.action_type)} {dayPfx}{range}{temp}
          </span>
        );
      })}
    </span>
  );
}
