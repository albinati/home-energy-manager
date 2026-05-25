import { hhmm, slotKindLabel, slotKindColorVar } from "../../lib/format";
import type { SchedulerTimeline, TimelineSlot } from "../../lib/types";

interface NextTransitionStripProps {
  timeline: SchedulerTimeline | null;
  limit?: number;
}

// Shows the next N planned slot edges so the operator sees what's coming.
export function NextTransitionStrip({ timeline, limit = 6 }: NextTransitionStripProps) {
  const upcoming = (timeline?.planned || []).slice(0, limit);
  if (upcoming.length === 0) {
    return <div class="next-transition next-transition--empty muted">No upcoming dispatch transitions.</div>;
  }

  // Collapse consecutive same-kind slots into a single strip cell.
  const condensed = collapse(upcoming);

  return (
    <div class="next-transition" aria-label="Upcoming dispatch slots">
      {condensed.map((cell, i) => {
        const kind = cell.kind || "standard";
        return (
          <div class="next-transition-cell" key={i} title={`${cell.from} → ${cell.to} · ${slotKindLabel(kind)}`}>
            <span class="next-transition-bar" style={{ background: slotKindColorVar(kind) }} />
            <div class="next-transition-when">{hhmm(cell.from)}</div>
            <div class="next-transition-label">{slotKindLabel(kind)}</div>
            <div class="next-transition-mode muted">{cell.fox_mode || ""}</div>
          </div>
        );
      })}
    </div>
  );
}

interface Cell {
  from: string;
  to: string;
  kind: string;
  fox_mode?: string;
}

function collapse(slots: TimelineSlot[]): Cell[] {
  const out: Cell[] = [];
  for (const s of slots) {
    const kind = s.dispatched_kind || s.lp_kind || "standard";
    const last = out[out.length - 1];
    if (last && last.kind === kind && last.fox_mode === s.fox_mode) {
      last.to = s.slot_time_utc;
    } else {
      out.push({ from: s.slot_time_utc, to: s.slot_time_utc, kind, fox_mode: s.fox_mode });
    }
  }
  return out;
}
