import type { SchedulerTimeline, DhwScheduleRow, HeatingPlanResponse, HeatingPlanSlot } from "../../lib/types";
import { formatRelativeSlot, endLabelFor, tankLabelOf, tankKindOf } from "../../lib/slotLabels";
import { upcomingForcedWindows, labelForKind, kindColorVar } from "../../lib/planHelpers";
import "./plan-widget.css";

interface Props {
  timeline: SchedulerTimeline | null;
  dhwSchedule?: DhwScheduleRow[] | null;
  heatingPlan?: HeatingPlanResponse | null;
  nowUtc?: string;
  foxMode?: string;
  foxActive?: boolean;
}

interface LwtWindow { kind: "boost" | "setback"; start_utc: string; end_utc: string; slot_count: number; peak: number; }

// Upcoming space-heating LWT offset windows: contiguous future slots where HEM
// nudges the radiator water temp up (boost, on cheap/negative) or down (setback,
// on peak). Neutral (offset 0) slots break a run.
function upcomingLwtWindows(slots: HeatingPlanSlot[], nowMs: number, limit = 3): LwtWindow[] {
  const out: LwtWindow[] = [];
  let cur: LwtWindow | null = null;
  for (const s of slots) {
    const st = s.slot_utc ? Date.parse(s.slot_utc) : NaN;
    if (!Number.isFinite(st) || st <= nowMs) continue;   // future only
    const off = s.lwt_offset ?? 0;
    const kind = off > 0 ? "boost" : off < 0 ? "setback" : null;
    if (kind) {
      if (cur && cur.kind === kind) {
        cur.end_utc = s.slot_utc!;
        cur.slot_count += 1;
        if (Math.abs(off) > Math.abs(cur.peak)) cur.peak = off;
      } else {
        if (cur) out.push(cur);
        cur = { kind, start_utc: s.slot_utc!, end_utc: s.slot_utc!, slot_count: 1, peak: off };
      }
    } else if (cur) {
      out.push(cur);
      cur = null;
      if (out.length >= limit) break;
    }
  }
  if (cur) out.push(cur);
  return out.slice(0, limit);
}

// The committed dispatch PLAN, lifted out of the Live-power tile into its own
// card next to Weather: the upcoming Fox battery windows + the tank (DHW)
// schedule, side by side. Forward-looking only — "what the system is about to
// do". The live power flow stays in the Live-power tile.
export function PlanWidget({ timeline, dhwSchedule, heatingPlan, nowUtc, foxMode, foxActive }: Props) {
  const forced = timeline ? upcomingForcedWindows(timeline, 4) : [];
  const nowMs = nowUtc ? Date.parse(nowUtc) : Date.now();
  const tank = (dhwSchedule || [])
    .filter((r) => r.start_utc && Date.parse(r.start_utc) > nowMs)
    .sort((a, b) => Date.parse(a.start_utc!) - Date.parse(b.start_utc!))
    .slice(0, 4);

  const lwt = upcomingLwtWindows(
    (heatingPlan?.slots || []).filter((s) => !!s.slot_utc), nowMs, 3,
  );

  const runId = timeline?.run_id ?? null;
  const planDate = timeline?.plan_date ?? null;
  const nothing = forced.length === 0 && tank.length === 0 && lwt.length === 0;

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

      {/* HEATING (space-heating LWT offset) */}
      <div class="planw-group">
        <div class="planw-head"><span class="planw-title">Heating</span></div>
        {lwt.length > 0 ? (
          <div class="planw-chips">
            {lwt.map((w) => {
              const start = formatRelativeSlot(w.start_utc, nowUtc);
              const endTime = endLabelFor(w.end_utc);
              const range = w.slot_count > 1 ? `${start.timeLabel}–${endTime}` : start.timeLabel;
              const label = w.kind === "boost" ? "Boost" : "Setback";
              return (
                <span key={w.start_utc} class={`planw-chip planw-chip--lwt-${w.kind}`}
                      title={`Radiator ${label} ${w.peak > 0 ? "+" : ""}${w.peak}°C · ${start.dayLabel ? start.dayLabel + " " : ""}${range}`}>
                  <span class="planw-dot" style={`background:${w.kind === "boost" ? "var(--ok)" : "var(--warn)"}`} />
                  {label} <span class="planw-temp-inline">{w.peak > 0 ? "+" : ""}{w.peak}°</span>
                  <span class="planw-when">{start.dayLabel ? `${start.dayLabel} ` : ""}{range}</span>
                </span>
              );
            })}
          </div>
        ) : (
          <span class="planw-empty">Weather curve — no LWT offset ahead</span>
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
