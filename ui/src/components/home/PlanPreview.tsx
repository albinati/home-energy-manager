import type { SchedulerTimeline, TimelineSlot } from "../../lib/types";
import { hhmm, slotKindLabel, slotKindColorVar } from "../../lib/format";
import "./plan-preview.css";

interface PlanPreviewProps {
  timeline: SchedulerTimeline | null;
  nowUtc: string;
  horizonHours?: number;
}

// Compact strip showing the next N hours of LP dispatch. Each cell is a
// 30-min slot coloured by dispatched_kind. Hover shows time + kind.
// Replaces the user's "I have to go to Plan tab to see what's coming up"
// gap.
export function PlanPreview({ timeline, nowUtc, horizonHours = 6 }: PlanPreviewProps) {
  if (!timeline) {
    return <div class="plan-preview-empty muted">Loading plan…</div>;
  }

  const slots: TimelineSlot[] = [
    ...(timeline.ongoing ? [timeline.ongoing] : []),
    ...(timeline.planned || []),
  ];

  const nowMs = Date.parse(nowUtc);
  const horizonMs = nowMs + horizonHours * 3600 * 1000;
  const filtered = slots.filter((s) => {
    const t = Date.parse(s.slot_time_utc);
    return Number.isFinite(t) && t < horizonMs;
  });

  if (filtered.length === 0) {
    return <div class="plan-preview-empty muted">No upcoming plan in the next {horizonHours} h.</div>;
  }

  // Condense consecutive same-kind slots into runs
  const runs: Array<{ from: string; to: string; kind: string; minutes: number }> = [];
  for (const s of filtered) {
    const kind = s.dispatched_kind || s.lp_kind || "standard";
    const last = runs[runs.length - 1];
    if (last && last.kind === kind) {
      last.to = s.slot_time_utc;
      last.minutes += 30;
    } else {
      runs.push({ from: s.slot_time_utc, to: s.slot_time_utc, kind, minutes: 30 });
    }
  }

  return (
    <div class="plan-preview">
      <div class="plan-preview-cells" role="presentation">
        {filtered.map((s, i) => {
          const kind = s.dispatched_kind || s.lp_kind || "standard";
          const isOngoing = i === 0;
          return (
            <div
              key={s.slot_time_utc}
              class={`plan-preview-cell${isOngoing ? " is-ongoing" : ""}`}
              style={{ background: slotKindColorVar(kind) }}
              title={`${hhmm(s.slot_time_utc)} · ${slotKindLabel(kind)}`}
            />
          );
        })}
      </div>
      <div class="plan-preview-axis">
        <span>now</span>
        <span>+{Math.round(horizonHours / 2)}h</span>
        <span>+{horizonHours}h</span>
      </div>
      <div class="plan-preview-runs">
        {runs.slice(0, 4).map((r) => (
          <div class="plan-preview-run" key={r.from}>
            <span class="plan-preview-run-dot" style={{ background: slotKindColorVar(r.kind) }} />
            <span class="plan-preview-run-time">{hhmm(r.from)}</span>
            <span class="plan-preview-run-kind">{slotKindLabel(r.kind)}</span>
            <span class="plan-preview-run-min">{r.minutes}m</span>
          </div>
        ))}
      </div>
    </div>
  );
}
