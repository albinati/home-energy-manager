import type { LwtScheduleRow } from "../../lib/types";
import { formatRelativeSlot, endLabelFor } from "../../lib/slotLabels";
import "./tank-badges.css";

// LWT-offset pre-heat badges (#481) — "Pre-heat +3°C 10:30–15:00",
// "Setback −2°C 15:00–20:30". Mirrors the tank schedule chips: each row is
// already a coalesced offset window from the dispatch layer, so no merge here.
// Positive offset = boost (warm, pre-heat) → reuse the yellow "warmup" pill;
// negative = setback (coast) → the grey "setback" pill. Future-day rows dim.

interface Props {
  rows: LwtScheduleRow[] | null | undefined;
  nowIso?: string | null;
  limit?: number;
}

function offsetLabel(off: number): string {
  // Proper signed °C with a real minus sign for negatives.
  const sign = off > 0 ? "+" : off < 0 ? "−" : "";
  return `${sign}${Math.abs(off)}°C`;
}

export function LwtScheduleBadges({ rows, nowIso, limit }: Props) {
  const items = (rows || [])
    .filter((r) => r.start_utc && r.lwt_offset != null && r.lwt_offset !== 0)
    .slice(0, limit ?? 99);
  if (!items.length) return null;
  return (
    <span class="tank-windows">
      {items.map((r, i) => {
        const off = r.lwt_offset as number;
        const kind = off > 0 ? "warmup" : "setback";
        const verb = off > 0 ? "Pre-heat" : "Setback";
        const start = formatRelativeSlot(r.start_utc ?? undefined, nowIso);
        const end = endLabelFor(r.end_utc ?? undefined, false);
        const range = `${start.timeLabel}–${end}`;
        const dayPfx = start.dayLabel ? `${start.dayLabel} ` : "";
        const label = `${verb} ${offsetLabel(off)}`;
        return (
          <span key={i}
                class={`tank-window tank-window--${kind}${start.isToday ? "" : " tank-window--future"}`}
                title={`${label} ${dayPfx}${range} (leaving-water offset)`}>
            <span class="tank-window-dot" />
            {label} {dayPfx}{range}
          </span>
        );
      })}
    </span>
  );
}
