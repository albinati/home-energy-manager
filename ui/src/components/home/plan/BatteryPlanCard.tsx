import type { PlanFrontsBattery, BatteryPlanWindow, BatteryWindowKind } from "../../../lib/types";
import { Icon, type IconName } from "../../common/Icon";
import { windowStatus, num, range } from "./util";
import "./plan.css";

const KIND: Record<BatteryWindowKind, { icon: IconName; label: string }> = {
  grid_charge: { icon: "import", label: "Grid charge" },
  pv_charge: { icon: "solar", label: "Solar charge" },
  hold: { icon: "battery", label: "Hold" },
  self_use: { icon: "house", label: "Self-use" },
  export: { icon: "export", label: "Export" },
  idle: { icon: "moon", label: "Idle" },
};

function headline(w: BatteryPlanWindow): string {
  const t = range(w.start_local, w.end_local);
  const target = w.soc_end_pct != null ? ` → ${Math.round(w.soc_end_pct)} %` : "";
  switch (w.kind) {
    case "grid_charge": return `Charge ${num(w.grid_kwh)} kWh from grid ${t}${target}`;
    case "pv_charge": return `Charge ${num(w.charge_kwh)} kWh from solar ${t}${target}`;
    case "hold": return `Hold charge ${t}${target}`;
    case "export": return `Export ${num(w.discharge_kwh)} kWh ${t}`;
    case "self_use": return `Battery serves the house ${t}${target}`;
    default: return `Idle ${t}`;
  }
}

export function BatteryPlanCard({ data, nowUtc, loading }: { data: PlanFrontsBattery | null; nowUtc: string; loading: boolean }) {
  if (!data) {
    return loading ? <div class="pf"><span class="skel-text" style={{ width: "70%", height: "1.2em" }} /><span class="skel-text" style={{ width: "100%", height: "5em" }} /></div>
      : <p class="muted">Battery plan unavailable.</p>;
  }
  if (data.error) return <p class="muted">Battery plan unavailable ({data.error}).</p>;

  const rows = data.windows.map((w) => ({ w, st: windowStatus(w.start_utc, w.end_utc, nowUtc) }));
  // Next action: the ongoing or next upcoming non-idle window.
  const next = rows.find((r) => r.st !== "done" && r.w.kind !== "idle" && r.w.kind !== "self_use")
    ?? rows.find((r) => r.st !== "done" && r.w.kind !== "idle");
  const peakPlanned = data.peak_import_planned_kwh;
  return (
    <div class="pf">
      <div class="pf-next">
        <span class="pf-eyebrow">{next?.st === "ongoing" ? "Now" : "Next"}</span>
        <span class="pf-headline">{next ? headline(next.w) : "No battery action planned"}</span>
        <div class="pf-kv">
          <span>SoC now <b>{num(data.soc_now_pct, 0)} %</b>{data.soc_now_kwh != null && <> · {num(data.soc_now_kwh)} kWh</>}</span>
          {data.reserve_pct != null && <span>reserve <b>{num(data.reserve_pct, 0)} %</b></span>}
        </div>
      </div>

      {rows.length > 0 && (
        <ul class="pf-list" aria-label="Battery windows">
          {rows.map(({ w, st }) => {
            const k = KIND[w.kind];
            const kwh = w.kind === "grid_charge" ? w.grid_kwh : w.kind === "self_use" || w.kind === "export" ? w.discharge_kwh : w.charge_kwh;
            return (
              <li key={`${w.start_utc}-${w.kind}`} class={`pf-row pf-row--${st}`}>
                <span class={`pf-ico ${w.kind === "grid_charge" || w.kind === "pv_charge" ? "pf-tone-charge" : ""}`}><Icon name={k.icon} size={13} /></span>
                <span class="pf-when">{range(w.start_local, w.end_local)}</span>
                <span class="pf-what">{k.label}</span>
                <span class="pf-val">{kwh != null && kwh > 0.005 ? `${num(kwh)} kWh` : ""}</span>
                <span class="pf-val2">{w.soc_start_pct != null && w.soc_end_pct != null ? `${Math.round(w.soc_start_pct)} → ${Math.round(w.soc_end_pct)} %` : ""}</span>
              </li>
            );
          })}
        </ul>
      )}

      {data.by_band.length > 0 && (
        <div class="pf-next">
          <span class="pf-eyebrow">Grid import by band</span>
          {data.by_band.map((b) => {
            const peakBreach = b.key === "band_peak" && (b.planned_import_kwh ?? 0) > 0.05;
            const isPeak = b.key === "band_peak" || b.label === "peak";
            return (
              <div key={`${b.key}-${b.start_local}`} class="pf-band-row">
                <span><span class="pf-band-name">{b.label}</span> {range(b.start_local, b.end_local)}
                  {b.floored && <span class="pf-dot" title="Charge floor active: SoC at entry is protected by the pessimistic plan" aria-label="floored" />}
                </span>
                <span>
                  plan <b>{num(b.planned_import_kwh)}</b> kWh
                  {b.realised_import_kwh != null && <> · so far <b>{num(b.realised_import_kwh)}</b></>}
                </span>
                {isPeak && (
                  <span class={`pf-note ${peakBreach ? "pf-note--warn" : "pf-ok"}`} style={{ gridColumn: "1 / -1" }}>
                    {peakBreach ? `peak import planned ${num(b.planned_import_kwh)} kWh — target 0` : "peak import target 0 — on plan"}
                  </span>
                )}
              </div>
            );
          })}
          {peakPlanned != null && data.by_band.every((b) => b.key !== "band_peak") && (
            <span class={`pf-note ${peakPlanned > 0.05 ? "pf-note--warn" : ""}`}>peak import planned {num(peakPlanned)} kWh (target 0)</span>
          )}
        </div>
      )}

      {data.fox_groups.length > 0 && (
        <span class="pf-note">
          Fox: {data.fox_groups.map((g) => `${g.mode ?? "?"} ${g.start_local ?? ""}–${g.end_local ?? ""}`).join(" · ")}
        </span>
      )}
    </div>
  );
}
