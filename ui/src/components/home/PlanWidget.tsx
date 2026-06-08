import type { SchedulerTimeline, DhwScheduleRow } from "../../lib/types";
import { formatRelativeSlot, endLabelFor, tankLabelOf, tankKindOf } from "../../lib/slotLabels";
import { upcomingForcedWindows, labelForKind, kindColorVar } from "../../lib/planHelpers";
import "./plan-widget.css";

interface Props {
  timeline: SchedulerTimeline | null;
  dhwSchedule?: DhwScheduleRow[] | null;
  nowUtc?: string;
  foxMode?: string;
  foxActive?: boolean;
}

// The committed dispatch PLAN, lifted out of the Live-power tile into its own
// card next to Weather: the upcoming Fox battery windows + the tank (DHW)
// schedule, side by side. Forward-looking only — "what the system is about to
// do". The live power flow stays in the Live-power tile.
export function PlanWidget({ timeline, dhwSchedule, nowUtc, foxMode, foxActive }: Props) {
  const forced = timeline ? upcomingForcedWindows(timeline, 4) : [];
  const nowMs = nowUtc ? Date.parse(nowUtc) : Date.now();
  const tank = (dhwSchedule || [])
    .filter((r) => r.start_utc && Date.parse(r.start_utc) > nowMs)
    .sort((a, b) => Date.parse(a.start_utc!) - Date.parse(b.start_utc!))
    .slice(0, 4);

  const runId = timeline?.run_id ?? null;
  const planDate = timeline?.plan_date ?? null;
  const nothing = forced.length === 0 && tank.length === 0;

  return (
    <div class="planw">
      {/* BATTERY (Fox) */}
      <div class="planw-group">
        <div class="planw-head">
          <span class="planw-title">Battery</span>
          {foxActive && foxMode && (
            <span class={`planw-mode planw-mode--${foxMode.toLowerCase()}`}>
              <span class="planw-mode-dot" />{foxMode}
            </span>
          )}
        </div>
        {forced.length > 0 ? (
          <div class="planw-chips">
            {forced.map((w) => {
              const start = formatRelativeSlot(w.start_utc, nowUtc);
              const endTime = endLabelFor(w.end_utc);
              const range = w.slot_count > 1 ? `${start.timeLabel}–${endTime}` : start.timeLabel;
              return (
                <span key={w.start_utc} class={`planw-chip${start.isToday ? "" : " planw-chip--future"}`}
                      title={`${w.kind} · ${start.dayLabel ? start.dayLabel + " " : ""}${range}`}>
                  <span class="planw-dot" style={`background:${kindColorVar(w.kind)}`} />
                  {labelForKind(w.kind)} <span class="planw-when">{start.dayLabel ? `${start.dayLabel} ` : ""}{range}</span>
                </span>
              );
            })}
          </div>
        ) : (
          <span class="planw-empty">Self-use — no forced windows ahead</span>
        )}
      </div>

      {/* TANK (DHW) */}
      <div class="planw-group">
        <div class="planw-head"><span class="planw-title">Tank</span></div>
        {tank.length > 0 ? (
          <div class="planw-chips">
            {tank.map((r) => {
              const start = formatRelativeSlot(r.start_utc!, nowUtc);
              const kind = tankKindOf(r.action_type);
              return (
                <span key={r.start_utc} class={`planw-chip planw-chip--tank-${kind}`}
                      title={`${tankLabelOf(r.action_type)} · ${start.dayLabel ? start.dayLabel + " " : ""}${start.timeLabel}${r.tank_temp_c != null ? ` → ${r.tank_temp_c}°C` : ""}`}>
                  <span class="planw-dot planw-dot--tank" />
                  {tankLabelOf(r.action_type)} <span class="planw-when">{start.dayLabel ? `${start.dayLabel} ` : ""}{start.timeLabel}</span>
                  {r.tank_temp_c != null && <span class="planw-temp">{r.tank_temp_c}°</span>}
                </span>
              );
            })}
          </div>
        ) : (
          <span class="planw-empty">No tank changes ahead</span>
        )}
      </div>

      {nothing && <span class="planw-allquiet">Plan steady — nothing scheduled soon</span>}

      {(runId != null || planDate) && (
        <div class="planw-foot" title="The LP run this plan came from">
          LP{runId != null ? ` #${runId}` : ""}{planDate ? ` · plan ${planDate}` : ""}
        </div>
      )}
    </div>
  );
}
