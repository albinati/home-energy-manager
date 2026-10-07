import type { PlanFrontsTank, TankPlanWindow } from "../../../lib/types";
import { Icon } from "../../common/Icon";
import { windowStatus, num, range, hh } from "./util";
import "./plan.css";

const LABEL: Record<TankPlanWindow["kind"], string> = {
  warmup: "Warm up", setback: "Setback", boost: "Boost", legionella: "Legionella cycle",
};

function armLine(d: NonNullable<PlanFrontsTank["decision"]>): string | null {
  if (d.arm === "hold") return `hold to ${hh(d.peak_entry_hour ?? d.setback_hour)}`;
  if (d.arm === "boost") return `boost to ${num(d.warmup_target_c, 0)} °C then coast`;
  if (d.arm === "static") return "static schedule";
  return null;
}

export function TankPlanCard({ data, nowUtc, loading }: { data: PlanFrontsTank | null; nowUtc: string; loading: boolean }) {
  if (!data) {
    return loading ? <div class="pf"><span class="skel-text" style={{ width: "70%", height: "1.2em" }} /><span class="skel-text" style={{ width: "100%", height: "5em" }} /></div>
      : <p class="muted">Hot water plan unavailable.</p>;
  }
  if (data.error) return <p class="muted">Hot water plan unavailable ({data.error}).</p>;

  const windows = data.windows ?? [];
  const showers = data.showers ?? [];
  const na = data.next_action;
  const d = data.decision;
  const m = data.model;
  const arm = d ? armLine(d) : null;
  return (
    <div class="pf">
      <div class="pf-next">
        <span class="pf-eyebrow">Next</span>
        <span class="pf-headline">
          {na ? `${LABEL[na.kind as TankPlanWindow["kind"]] ?? na.kind}${na.tank_target_c != null ? ` to ${num(na.tank_target_c, 0)} °C` : ""} at ${na.start_local}` : "No tank action planned"}
        </span>
        <div class="pf-kv">
          <span>tank now <b>{num(data.tank_now_c)} °C</b></span>
          <span>target <b>{num(data.target_now_c, 0)} °C</b></span>
          {data.power_on === false && <span class="pf-warn">power off</span>}
        </div>
        {arm && d && (
          <span class="pf-sub">
            {arm}
            {d.cost_hold_p != null && d.cost_boost_p != null && <> · hold {num(d.cost_hold_p)}p vs boost {num(d.cost_boost_p)}p</>}
          </span>
        )}
      </div>

      {windows.length > 0 && (
        <ul class="pf-list" aria-label="Tank windows">
          {windows.map((w) => (
            <li key={`${w.start_utc}-${w.kind}`} class={`pf-row pf-row--${windowStatus(w.start_utc, w.end_utc, nowUtc)}`}>
              <span class="pf-ico pf-tone-heat"><Icon name="droplet" size={13} /></span>
              <span class="pf-when">{range(w.start_local, w.end_local)}</span>
              <span class="pf-what">{LABEL[w.kind] ?? w.kind}</span>
              <span class="pf-val">{w.tank_target_c != null ? `${num(w.tank_target_c, 0)} °C` : ""}</span>
              <span class="pf-val2" />
            </li>
          ))}
        </ul>
      )}

      {showers.length > 0 && (
        <div class="pf-next">
          <span class="pf-eyebrow">Showers</span>
          {showers.map((s) => {
            const low = s.predicted_tank_c != null && s.predicted_tank_c < s.floor_c;
            return (
              <span key={`${s.start_local}-${s.end_local}`} class={`pf-sub ${low ? "pf-warn" : ""}`}>
                {range(s.start_local, s.end_local)} needs ≥{num(s.floor_c, 0)} °C
                {s.predicted_tank_c != null && <> · predicted {num(s.predicted_tank_c)} °C</>}
              </span>
            );
          })}
        </div>
      )}

      {m && m.coast_measured_c_per_h != null && (
        <span class="pf-note">
          coast {num(m.coast_measured_c_per_h, 2)} °C/h measured
          {m.coast_model_c_per_h != null && <> vs {num(m.coast_model_c_per_h, 2)} model</>}
          {m.ua_w_per_k != null && <> (UA {num(m.ua_w_per_k)} W/K{m.source === "measured_indoor" ? ", ambient = house" : ""})</>}
        </span>
      )}
    </div>
  );
}
