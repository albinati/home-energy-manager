import { useFetch } from "../../lib/poll";
import { getLpScorecard } from "../../lib/endpoints";
import { periodLastCompleteDay, type PeriodState } from "../../lib/period";
import type { LpScorecard } from "../../lib/types";

// "System health" — the LP scorecard for the last COMPLETE day of the selected
// period (so the plan-vs-realised comparison isn't half-empty). Follows the
// navigator: a past period shows its final day, the current period shows
// yesterday. Three slices: did the plan match reality (dispatch accuracy), did
// optimising pay (vs a naive self-use shadow), and how good were the forecasts
// the LP planned on.

const pct = (v: number | null | undefined) => (v == null ? "—" : `${Math.round(v)}%`);
const pence = (v: number | null | undefined) =>
  v == null ? "—" : `${v >= 0 ? "" : "−"}£${Math.abs(v / 100).toFixed(2)}`;

export function SystemHealthCard({ period }: { period: PeriodState }) {
  const day = periodLastCompleteDay(period);
  const res = useFetch(() => getLpScorecard(day), [day]);
  const sc: LpScorecard | undefined = res.data?.scorecard;

  // The backend grades a data-less day "N/A" (a string, never null) — treat
  // it as no-grade.
  const grade = sc?.grade && sc.grade !== "N/A" ? sc.grade : null;
  const disp = sc?.dispatch_accuracy;
  const econ = sc?.economic_value;
  const fc = sc?.forecast_accuracy;
  const hasAnything = !!(grade || disp?.n_slots_with_plan || econ?.lp_realised_cost_p != null);

  if (!sc || !hasAnything) {
    // A diagnostic, not a load-bearing surface — but a quiet placeholder reads
    // better than a blank page bottom on a data-thin day.
    return (
      <section class="syshealth">
        <header class="syshealth-head">
          <h2>System health</h2>
          <span class="muted">LP scorecard · {day}</span>
        </header>
        <p class="muted insights-empty">No scorecard for {day} yet.</p>
      </section>
    );
  }

  const avoided = econ?.lp_avoided_cost_p;

  return (
    <section class="syshealth">
      <header class="syshealth-head">
        <h2>System health</h2>
        <span class="muted">LP scorecard · {sc.day}</span>
        {grade && <span class={`syshealth-grade syshealth-grade--${grade.toLowerCase()}`}>{grade}</span>}
      </header>

      <div class="syshealth-grid">
        {disp && (disp.n_slots_with_plan ?? 0) > 0 && (
          <div class="syshealth-cell" title={`${disp.n_slots_with_plan} planned / ${disp.n_slots_with_real} realised slots`}>
            <span class="syshealth-label">Plan followed</span>
            <span class="syshealth-value">
              import {pct(disp.import_accuracy_pct)} · export {pct(disp.export_accuracy_pct)} · charge {pct(disp.charge_accuracy_pct)}
            </span>
            <span class="syshealth-sub">
              {disp.import_planned_kwh?.toFixed(1)} → {disp.import_real_kwh?.toFixed(1)} kWh imported
            </span>
          </div>
        )}

        {econ && econ.lp_realised_cost_p != null && (
          <div class="syshealth-cell" title={econ.comparison_basis ?? undefined}>
            <span class="syshealth-label">Optimising paid?</span>
            <span class="syshealth-value">
              {avoided != null
                ? (avoided >= 0 ? `saved ${pence(avoided)}` : `cost ${pence(-avoided)} extra`)
                : "no shadow baseline"}
            </span>
            <span class="syshealth-sub">
              day cost {pence(econ.lp_realised_cost_p)}
              {econ.naive_self_use_shadow_p != null ? ` · naive self-use ${pence(econ.naive_self_use_shadow_p)}` : ""}
            </span>
          </div>
        )}

        {fc?.available && (
          <div class="syshealth-cell" title={`${fc.n_hours ?? 0} hours scored (positive bias = over-forecast)`}>
            <span class="syshealth-label">Forecast skill</span>
            <span class="syshealth-value">
              PV MAE {fc.pv_kwh_mae != null ? `${fc.pv_kwh_mae.toFixed(2)} kWh` : "—"}
              {fc.outdoor_temp_c_mae != null ? ` · temp ${fc.outdoor_temp_c_mae.toFixed(1)}°C` : ""}
            </span>
            <span class="syshealth-sub">
              {fc.load_kwh_mae != null ? `load MAE ${fc.load_kwh_mae.toFixed(2)} kWh` : ""}
              {fc.pv_kwh_bias != null ? ` · PV bias ${fc.pv_kwh_bias >= 0 ? "+" : ""}${fc.pv_kwh_bias.toFixed(2)}` : ""}
            </span>
          </div>
        )}
      </div>
    </section>
  );
}
