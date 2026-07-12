import { useState } from "preact/hooks";
import { useFetch } from "../lib/poll";
import { getActionLog } from "../lib/endpoints";
import { Spinner } from "../components/common/Spinner";
import { Pill } from "../components/common/Pill";
import { SensorJournal } from "../components/report/SensorJournal";
import { hhmm } from "../lib/format";
import type { ActionLogEntry } from "../lib/types";
import "./report.css";

// Execution journal — what the system ACTUALLY did (tank / battery / appliances),
// read straight from action_log. This is the audit trail: every fired, skipped
// or failed action with its trigger, tariff context and result.
// A second view (Sensors) shows the lossless room-sensor ingest audit
// (device_reading_log, #540 W1c) — everything each device POSTed.

type JournalView = "actions" | "sensors";
const VIEWS: { key: JournalView; label: string; blurb: string }[] = [
  { key: "actions", label: "Actions",
    blurb: "Everything the system actually did — tank, battery and appliances — with its trigger and result." },
  { key: "sensors", label: "Sensors",
    blurb: "Everything the room sensors sent — every reading per device, exactly as received." },
];

type DeviceFilter = "all" | "daikin" | "foxess" | "appliance";
const DEVICE_TABS: { key: DeviceFilter; label: string }[] = [
  { key: "all", label: "All" },
  { key: "daikin", label: "Tank / heat pump" },
  { key: "foxess", label: "Battery" },
  { key: "appliance", label: "Appliances" },
];
const RANGES = [
  { days: 1, label: "24h" },
  { days: 7, label: "7 days" },
  { days: 30, label: "30 days" },
];

function deviceLabel(d: string): string {
  switch (d) {
    case "daikin": return "Tank";
    case "foxess": case "fox": return "Battery";
    case "appliance": return "Appliance";
    default: return d;
  }
}
function deviceTone(d: string): "thermal" | "power" | "appliance" | "neutral" {
  switch (d) {
    case "daikin": return "thermal";
    case "foxess": case "fox": return "power";
    case "appliance": return "appliance";
    default: return "neutral";
  }
}

// Humanise the raw action verbs into something readable.
function actionLabel(a: string): string {
  const map: Record<string, string> = {
    tank_warmup: "Tank warmup",
    tank_setback: "Tank setback",
    tank_negative_boost: "Tank boost (negative price)",
    max_heat: "Tank max-heat",
    pre_heat: "Tank pre-heat",
    shutdown: "Tank shutdown",
    restore: "Restore to baseline",
    charge: "Battery force-charge",
    discharge: "Battery force-discharge",
    mode_switch: "Battery mode switch",
    washer_start: "Washer start",
    dryer_start: "Dryer start",
    dishwasher_start: "Dishwasher start",
    apply_safe_defaults: "Apply safe defaults",
  };
  return map[a] || a.replace(/_/g, " ");
}
function resultTone(r: string): "ok" | "bad" | "dim" {
  if (r === "success") return "ok";
  if (r === "failed") return "bad";
  return "dim";
}

function localDay(iso: string): string {
  try {
    return new Date(iso).toLocaleDateString([], { weekday: "short", day: "2-digit", month: "short" });
  } catch { return iso.slice(0, 10); }
}

// Pick the most informative param to show inline (temp, offset, power, soc).
function paramHint(p: Record<string, unknown>): string | null {
  const bits: string[] = [];
  if (typeof p.tank_temp === "number") bits.push(`${p.tank_temp}°C`);
  if (typeof p.lwt_offset === "number" && p.lwt_offset !== 0) bits.push(`LWT ${p.lwt_offset > 0 ? "+" : ""}${p.lwt_offset}`);
  if (typeof p.fdSoc === "number") bits.push(`→ ${p.fdSoc}%`);
  if (typeof p.fdPwr === "number") bits.push(`${p.fdPwr}W`);
  if (p.tank_powerful === true) bits.push("powerful");
  return bits.length ? bits.join(" · ") : null;
}

export default function Report() {
  const [view, setView] = useState<JournalView>("actions");
  const blurb = VIEWS.find((v) => v.key === view)!.blurb;

  return (
    <div class="page-padded report">
      <header class="report-head">
        <div class="report-title-row">
          <h1>Activity journal</h1>
          <div class="report-tabs" role="tablist" aria-label="Journal view">
            {VIEWS.map((v) => (
              <button key={v.key} role="tab" aria-selected={view === v.key}
                      class={`report-tab${view === v.key ? " active" : ""}`}
                      onClick={() => setView(v.key)}>{v.label}</button>
            ))}
          </div>
        </div>
        <p class="muted">{blurb}</p>
      </header>

      {view === "actions" ? <ActionsJournal /> : <SensorJournal />}
    </div>
  );
}

function ActionsJournal() {
  const [device, setDevice] = useState<DeviceFilter>("all");
  const [days, setDays] = useState(7);
  const log = useFetch(
    () => getActionLog({ device: device === "all" ? undefined : device, days, limit: 300 }),
    [device, days],
  );

  const entries = log.data?.entries ?? [];
  // Group by local day, preserving the DESC order the API returns.
  const groups: { day: string; items: ActionLogEntry[] }[] = [];
  for (const e of entries) {
    const day = localDay(e.timestamp);
    const last = groups[groups.length - 1];
    if (last && last.day === day) last.items.push(e);
    else groups.push({ day, items: [e] });
  }

  return (
    <>
      <div class="report-filters">
        <div class="report-tabs" role="tablist" aria-label="Device">
          {DEVICE_TABS.map((t) => (
            <button key={t.key} role="tab" aria-selected={device === t.key}
                    class={`report-tab${device === t.key ? " active" : ""}`}
                    onClick={() => setDevice(t.key)}>{t.label}</button>
          ))}
        </div>
        <div class="report-range">
          {RANGES.map((r) => (
            <button key={r.days} class={`report-tab${days === r.days ? " active" : ""}`}
                    onClick={() => setDays(r.days)}>{r.label}</button>
          ))}
        </div>
      </div>

      {log.loading && !log.data && <Spinner label="Loading journal…" />}
      {log.error && <p class="report-error">Couldn't load the journal: {log.error.message}</p>}
      {!log.loading && entries.length === 0 && (
        <p class="muted report-empty">No actions recorded in this window.</p>
      )}

      {groups.map((g) => (
        <section key={g.day} class="report-group">
          <h2 class="report-group-day">{g.day}</h2>
          <ul class="report-list">
            {g.items.map((e) => {
              const hint = paramHint(e.params || {});
              return (
                <li key={e.id} class="report-row">
                  <span class="report-time">{hhmm(e.timestamp)}</span>
                  <span class={`report-dot report-dot--${deviceTone(e.device)}`} />
                  <span class="report-device">{deviceLabel(e.device)}</span>
                  <span class="report-action">
                    {actionLabel(e.action)}
                    {hint && <span class="report-hint"> · {hint}</span>}
                  </span>
                  <span class="report-meta">
                    {e.slot_kind && <span class="report-kind">{e.slot_kind}</span>}
                    {e.agile_price_at_time != null && (
                      <span class="report-price">{e.agile_price_at_time.toFixed(1)}p</span>
                    )}
                    {e.trigger && <span class="report-trigger" title="What scheduled this action">{e.trigger}</span>}
                    <Pill tone={resultTone(e.result)}>{e.result}</Pill>
                  </span>
                  {e.error_msg && e.result !== "success" && (
                    <span class="report-err" title={e.error_msg}>{e.error_msg}</span>
                  )}
                </li>
              );
            })}
          </ul>
        </section>
      ))}
    </>
  );
}
