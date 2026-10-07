import { useState } from "preact/hooks";
import { role } from "../../../lib/auth";
import { postComfortFeedback, type ComfortVerdict } from "../../../lib/endpoints";
import type { PlanFrontsHeating, HeatingPlanWindow } from "../../../lib/types";
import { Icon } from "../../common/Icon";
import { Pill } from "../../common/Pill";
import { windowStatus, num, signed, range } from "./util";
import "./plan.css";

function describe(w: HeatingPlanWindow): string {
  if (w.kind === "restore") return "Back to weather curve";
  return `${signed(w.offset_c)} °C ${w.kind === "boost" ? "boost" : "setback"}`;
}

function gateLine(g: NonNullable<PlanFrontsHeating["gate"]>): string {
  const parts: string[] = [];
  if (g.preheat_enabled === false) parts.push("preheat disabled");
  else if (g.preheat_suppressed) parts.push("preheat suppressed");
  parts.push(g.demand_present === false ? "no heating demand" : "demand present");
  if (g.current_outdoor_c != null && g.outdoor_cutoff_c != null) {
    parts.push(`outdoor ${num(g.current_outdoor_c)} °C vs cutoff ${num(g.outdoor_cutoff_c, 0)} °C${g.positive_offset_suppressed_by_outdoor ? " (boost off)" : ""}`);
  }
  return parts.join(" · ");
}

function FeelRow({ rooms }: { rooms: string[] }) {
  const [room, setRoom] = useState("");
  const [busy, setBusy] = useState(false);
  const [ack, setAck] = useState("");
  const send = async (v: ComfortVerdict) => {
    setBusy(true);
    try {
      const r = await postComfortFeedback(v, room);
      setAck(`Logged ${v}${r.room ? ` · ${r.room}` : ""}${r.indoor_c != null ? ` · ${r.indoor_c.toFixed(1)} °C` : ""}${r.band ? ` · ${r.band}` : ""}`);
    } catch {
      setAck("Could not save feedback");
    } finally {
      setBusy(false);
    }
  };
  return (
    <div class="pf-feel">
      <span class="pf-eyebrow">How does it feel?</span>
      <div class="pf-feel-btns">
        {(["cold", "ok", "hot"] as ComfortVerdict[]).map((v) => (
          <button key={v} type="button" class="pf-feel-btn" disabled={busy} onClick={() => send(v)}>
            {v === "cold" ? "Cold" : v === "ok" ? "OK" : "Hot"}
          </button>
        ))}
        {rooms.length > 0 && (
          <select class="pf-feel-room" aria-label="Room" value={room} onChange={(e) => setRoom((e.target as HTMLSelectElement).value)}>
            <option value="">whole house</option>
            {rooms.map((r) => <option key={r} value={r}>{r.replace(/_/g, " ")}</option>)}
          </select>
        )}
      </div>
      {ack && <span class="pf-note" role="status">{ack}</span>}
    </div>
  );
}

export function HeatingPlanCard({ data, nowUtc, loading }: { data: PlanFrontsHeating | null; nowUtc: string; loading: boolean }) {
  if (!data) {
    return loading ? <div class="pf"><span class="skel-text" style={{ width: "70%", height: "1.2em" }} /><span class="skel-text" style={{ width: "100%", height: "5em" }} /></div>
      : <p class="muted">Heating plan unavailable.</p>;
  }
  if (data.error) return <p class="muted">Heating plan unavailable ({data.error}).</p>;

  const byBand = data.by_band ?? [];
  const rows = (data.windows ?? []).map((w) => ({ w, st: windowStatus(w.start_utc, w.end_utc, nowUtc) }));
  const next = rows.find((r) => r.st !== "done" && r.w.kind !== "restore");
  const rooms = Object.entries(data.indoor_rooms_c ?? {}).sort(([a], [b]) => a.localeCompare(b));
  const agg = data.indoor_aggregate ?? "";
  const minRoom = rooms.length ? rooms.reduce((a, b) => (b[1] < a[1] ? b : a))[0] : null;
  const emphasised = (r: string): boolean =>
    agg.startsWith("room:") ? agg.slice(5) === r : agg === "min" ? r === minRoom : false;
  const p = data.predicted_indoor;
  const marks: [string, number | null][] = p ? [["07", p.at_07_c], ["16", p.at_16_c], ["19", p.at_19_c], ["22", p.at_22_c]] : [];
  return (
    <div class="pf">
      <div class="pf-next">
        <span class="pf-eyebrow">{next?.st === "ongoing" ? "Now" : "Next"}</span>
        <span class="pf-headline">
          {next ? `${describe(next.w)} ${range(next.w.start_local, next.w.end_local)}` : "No change planned"}
        </span>
        <div class="pf-kv">
          <span>indoor <b>{num(data.indoor_now_c)} °C</b>{data.indoor_aggregate && rooms.length > 1 ? ` (${data.indoor_aggregate})` : ""}</span>
          <span>outdoor <b>{num(data.outdoor_now_c)} °C</b></span>
        </div>
        {rooms.length > 1 && (
          <div class="pf-rooms">
            {rooms.map(([r, t]) => (
              <span key={r} class={`pf-room ${emphasised(r) ? "pf-room--em" : ""}`}>{r.replace(/_/g, " ")} {num(t)}</span>
            ))}
          </div>
        )}
        <div class="pf-kv">
          <span>setpoint <b>{num(data.setpoint_c)}</b></span>
          <span>night floor <b>{num(data.night_floor_c)}</b></span>
          {data.peak_coast_delta_c != null && data.setpoint_c != null && (
            <span>peak coast <b>{num(data.setpoint_c - data.peak_coast_delta_c)}</b></span>
          )}
        </div>
        <div class="pf-chips">
          <Pill tone={data.lwt_source === "lp" ? "accent" : "neutral"} title="DAIKIN_LWT_SOURCE">
            {data.lwt_source === "lp" ? "LP" : data.lwt_source === "tier" ? "rule" : "—"}
          </Pill>
          {data.gate && <span class="pf-note">{gateLine(data.gate)}</span>}
        </div>
      </div>

      {rows.length > 0 && (
        <ul class="pf-list" aria-label="Heating windows">
          {rows.map(({ w, st }) => (
            <li key={`${w.start_utc}-${w.kind}`} class={`pf-row pf-row--${st}`}>
              <span class="pf-ico pf-tone-heat"><Icon name="heating" size={13} /></span>
              <span class="pf-when">{range(w.start_local, w.end_local)}</span>
              <span class="pf-what">{w.kind === "restore" ? "Weather curve" : w.kind === "boost" ? "Boost" : "Setback"}</span>
              <span class="pf-val">{w.kind === "restore" ? "" : `${signed(w.offset_c)} °C`}</span>
              <span class="pf-val2">{w.source === "lp" ? "LP" : w.source === "tier" ? "rule" : ""}</span>
            </li>
          ))}
        </ul>
      )}

      {p && (p.min_c != null || p.max_c != null) && (
        <span class="pf-note">
          predicted indoor {num(p.min_c)}–{num(p.max_c)} °C
          {marks.filter(([, v]) => v != null).map(([h, v]) => ` · ${h}h ${num(v)}`).join("")}
        </span>
      )}

      {byBand.length > 0 && (
        <div class="pf-next">
          <span class="pf-eyebrow">By band</span>
          {byBand.map((b) => (
            <div key={`${b.key}-${b.start_local}`} class="pf-band-row">
              <span><span class="pf-band-name">{b.label}</span> {range(b.start_local, b.end_local)}</span>
              <span>
                {b.offset_mode ?? "—"}
                {b.indoor_min_c != null && b.indoor_max_c != null && <> · <b>{num(b.indoor_min_c)}–{num(b.indoor_max_c)}</b> °C</>}
              </span>
            </div>
          ))}
        </div>
      )}
      {role.value === "admin" && <FeelRow rooms={rooms.map(([r]) => r)} />}
    </div>
  );
}
