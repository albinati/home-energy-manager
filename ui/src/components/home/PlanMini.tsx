import type { ComponentChildren } from "preact";
import type {
  SchedulerTimeline, DhwScheduleRow, HeatingPlanResponse, HeatingPlanSlot,
  Appliance, ApplianceJob, ApplianceSuggestion,
} from "../../lib/types";
import { Icon, type IconName } from "../common/Icon";
import { formatRelativeSlot, endLabelFor, tankLabelOf } from "../../lib/slotLabels";
import { upcomingForcedWindows, labelForKind, kindColorVar } from "../../lib/planHelpers";
import "./plan-mini.css";

// The committed dispatch plan, redesign form: NOT a standalone widget but a
// dashed-top "PLANNED DISPATCH" section pinned to the foot of each live card
// (battery + appliances inside Live power; heating + tank inside Live heating).
// Chips: tone dot · label · bold amount · time · muted note.

interface Chip {
  tone: string;            // css var name: ok | warn | cheap | neg-price | text-mute
  label: string;
  amt?: string;
  when: string;
  note?: string;
  faded?: boolean;
  title?: string;
}

interface GroupSpec {
  icon: IconName;
  title: string;
  sub?: string;
  chips: Chip[];
  empty: string;
}

interface PlanMiniProps {
  groups: ("battery" | "appliances" | "heating" | "tank")[];
  timeline?: SchedulerTimeline | null;
  dhwSchedule?: DhwScheduleRow[] | null;
  heatingPlan?: HeatingPlanResponse | null;
  appliances?: Appliance[] | null;
  applianceJobs?: ApplianceJob[] | null;
  applianceSuggestions?: ApplianceSuggestion[] | null;
  nowUtc?: string;
  foxMode?: string;
  foxActive?: boolean;
}

function hm(iso: string | null | undefined): string {
  if (!iso) return "—";
  try { return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false }); }
  catch { return "—"; }
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

function rangeOf(startUtc: string, endUtc: string, slotCount: number, nowUtc?: string): { when: string; faded: boolean } {
  const start = formatRelativeSlot(startUtc, nowUtc);
  const endTime = endLabelFor(endUtc);
  const range = slotCount > 1 ? `${start.timeLabel}–${endTime}` : start.timeLabel;
  return { when: `${start.dayLabel ? start.dayLabel + " " : ""}${range}`, faded: !start.isToday };
}

export function PlanMini(props: PlanMiniProps) {
  const { timeline, dhwSchedule, heatingPlan, appliances, applianceJobs, applianceSuggestions,
          nowUtc, foxMode, foxActive } = props;
  const nowMs = nowUtc ? Date.parse(nowUtc) : Date.now();

  const specs: GroupSpec[] = props.groups.map((g): GroupSpec => {
    if (g === "battery") {
      const forced = timeline ? upcomingForcedWindows(timeline, 4) : [];
      return {
        icon: "battery", title: "Battery",
        sub: foxActive && foxMode ? foxMode : "SelfUse",
        empty: "no forced windows ahead",
        chips: forced.map((w) => {
          const r = rangeOf(w.start_utc, w.end_utc, w.slot_count, nowUtc);
          return { tone: kindColorVar(w.kind), label: labelForKind(w.kind), when: r.when, faded: r.faded, title: w.kind };
        }),
      };
    }
    if (g === "heating") {
      const lwt = upcomingLwtWindows((heatingPlan?.slots || []).filter((s) => !!s.slot_utc), nowMs, 3);
      return {
        icon: "heating", title: "Heating",
        empty: "weather curve — no LWT offset ahead",
        chips: lwt.map((w) => {
          const r = rangeOf(w.start_utc, w.end_utc, w.slot_count, nowUtc);
          return {
            tone: w.kind === "boost" ? "var(--ok)" : "var(--warn)",
            label: w.kind === "boost" ? "Boost" : "Setback",
            amt: `${w.peak > 0 ? "+" : "−"}${Math.abs(w.peak)}°`,
            when: r.when, faded: r.faded,
            title: `Radiator LWT ${w.kind}`,
          };
        }),
      };
    }
    if (g === "tank") {
      const tank = (dhwSchedule || [])
        .filter((r) => r.start_utc && Date.parse(r.start_utc) > nowMs)
        .sort((a, b) => Date.parse(a.start_utc!) - Date.parse(b.start_utc!))
        .slice(0, 4);
      return {
        icon: "cost", title: "Tank",
        empty: "no tank changes ahead",
        chips: tank.map((row) => {
          const start = formatRelativeSlot(row.start_utc!, nowUtc);
          return {
            tone: "var(--warn)",
            label: tankLabelOf(row.action_type),
            when: `${start.dayLabel ? start.dayLabel + " " : ""}${start.timeLabel}`,
            note: row.tank_temp_c != null ? `${row.tank_temp_c}°` : undefined,
            faded: !start.isToday,
            title: row.action_type ?? undefined,
          };
        }),
      };
    }
    // appliances
    const apps = (appliances || []).filter((a) => a.enabled);
    const jobByApp = new Map(
      (applianceJobs || []).filter((j) => j.status === "scheduled" || j.status === "running")
        .map((j) => [j.appliance_id, j]),
    );
    const sugByApp = new Map((applianceSuggestions || []).map((s) => [s.appliance_id, s]));
    return {
      icon: "appliance", title: "Appliances",
      empty: "nothing queued",
      chips: apps.map((a): Chip => {
        const job = jobByApp.get(a.id);
        const sug = sugByApp.get(a.id);
        if (job?.status === "running") return { tone: "var(--ok)", label: a.name, when: "running" };
        if (job) {
          return {
            tone: "var(--cheap)", label: a.name,
            when: `${hm(job.planned_start_utc)}–${hm(job.planned_end_utc)}`,
            note: job.avg_price_pence != null ? `· ${job.avg_price_pence.toFixed(1)}p` : undefined,
            title: "Scheduled run",
          };
        }
        if (sug) {
          const paid = sug.is_negative;
          const cheap = sug.meets_threshold !== false;
          return {
            tone: paid ? "var(--neg-price)" : cheap ? "var(--cheap)" : "var(--text-mute)",
            label: a.name,
            when: `${cheap ? "" : "next "}${hm(sug.recommended_start_utc)}`,
            note: `· ${sug.avg_price_pence.toFixed(1)}p`,
            title: `${cheap ? (paid ? "Paid" : "Cheap") + " window" : "Next window (no cheap window ahead)"} ${hm(sug.recommended_start_utc)}–${hm(sug.recommended_end_utc)} · load + Smart Control by ${sug.deadline_local}`,
          };
        }
        return { tone: "var(--text-mute)", label: a.name, when: "idle" };
      }),
    };
  });

  // Appliances group disappears when none are registered (matches the old
  // widget); the other groups show their quiet empty line.
  const visible = specs.filter((s) => s.title !== "Appliances" || s.chips.length > 0);
  if (visible.length === 0) return null;

  const runId = timeline?.run_id ?? null;
  const planDate = timeline?.plan_date ?? null;
  const lpTitle = runId != null || planDate
    ? `LP${runId != null ? ` #${runId}` : ""}${planDate ? ` · plan ${planDate}` : ""}`
    : undefined;

  return (
    <div class="plan-mini">
      <div class="plan-mini-h" title={lpTitle}>Planned dispatch</div>
      {visible.map((s) => (
        <PlanGroup key={s.title} icon={s.icon} title={s.title} sub={s.sub}>
          {s.chips.length > 0 ? (
            <div class="chips">
              {s.chips.map((c, i) => (
                <span key={i} class={`chip${c.faded ? " chip--faded" : ""}`} title={c.title}>
                  <span class="cdot" style={{ background: c.tone }} />
                  {c.label}{c.amt && <b> {c.amt}</b>} <span class="when">{c.when}</span>
                  {c.note && <span class="note"> {c.note}</span>}
                </span>
              ))}
            </div>
          ) : (
            <span class="plan-grp-empty">{s.empty}</span>
          )}
        </PlanGroup>
      ))}
    </div>
  );
}

function PlanGroup({ icon, title, sub, children }: { icon: IconName; title: string; sub?: string; children: ComponentChildren }) {
  return (
    <div class="plan-grp">
      <div class="plan-grp-t">
        <Icon name={icon} size={12} />{title}
        {sub && <span class="plan-grp-sub">· {sub}</span>}
      </div>
      {children}
    </div>
  );
}
