import { usePoll } from "../../lib/poll";
import { getThermalCalibration } from "../../lib/endpoints";
import "./thermal-model.css";

// W2 (#540) — the building thermal model (τ / UA / C) the heating plan rides on,
// plus how far the learner has got. It stays on env defaults through summer (no
// thermal signal — τ needs cold-decay nights, UA needs heating-degree days), so
// this is the honest "learning in progress" surface the user watches converge.
export function ThermalModelStatus() {
  const cal = usePoll(getThermalCalibration, 10 * 60_000);
  const d = cal.data;
  if (!d) return null;
  const eff = d.effective;
  const learned = eff.source === "learned";
  const p = d.progress;
  const tauN = p?.tau?.episodes;
  const tauNeed = p?.tau?.needed ?? 5;
  const uaN = p?.ua?.hdd_days;
  const uaNeed = p?.ua?.needed ?? 20;

  return (
    <div class="thermal-model">
      <div class="eyebrow tm-eyebrow">
        Thermal model
        <span class={`tm-src tm-src--${eff.source}`}>{learned ? "learned" : "defaults"}</span>
      </div>
      <div class="tm-vals">
        <span>τ <b>{eff.tau_hours}h</b></span>
        <span>UA <b>{eff.ua_w_per_k}</b> W/K</span>
        <span>C <b>{eff.c_kwh_per_k}</b> kWh/K</span>
      </div>
      {!learned && (
        <div class="tm-prog dim small"
             title="τ needs clean cold-decay nights (indoor ≥5°C above outdoor); UA needs heating-degree days. Both accrue as the weather cools — the model self-activates in autumn/winter.">
          learning · {tauN ?? "—"}/{tauNeed} cold-decay nights · {uaN ?? "—"}/{uaNeed} heating days
        </div>
      )}
      <div class="tm-strat"
           title={d.w3_enabled
             ? "W3 on: the optimiser tracks indoor temperature, holds a comfort floor, and times heating to cheap slots."
             : "W3 off: the heat pump follows its weather curve, blind to price and indoor comfort. Enable LP_W3_TIN_ENABLED once the regression replay passes."}>
        <span class="tm-strat-k">Heating</span>
        <span class={`tm-strat-v tm-strat-v--${d.w3_enabled ? "on" : "off"}`}>
          {d.w3_enabled ? "comfort-optimised" : "weather curve"}
        </span>
      </div>
    </div>
  );
}
