import { tempC } from "../../lib/format";
import type { CockpitState } from "../../lib/types";
import "./cockpit.css";

interface ThermalWidgetProps {
  state: CockpitState;
}

// Compact horizontal rows — works at any widget width. Each row is a small
// icon + label + value. Mode pill at the top with an animated indicator
// when the heat pump is doing something.
export function ThermalWidget({ state }: ThermalWidgetProps) {
  const mode = (state.daikin_mode || "").toLowerCase();
  const isHeating = mode.includes("heat");
  const isCooling = mode.includes("cool");
  const isOff = mode === "" || mode === "off" || mode === "idle";

  return (
    <div class="thermal-widget">
      <div class="thermal-mode" data-mode={isHeating ? "heating" : isCooling ? "cooling" : "idle"}>
        {isHeating && <span class="thermal-flame" aria-hidden="true">🔥</span>}
        {isCooling && <span class="thermal-snow" aria-hidden="true">❄</span>}
        {isOff && <span class="thermal-zzz" aria-hidden="true">⏸</span>}
        <span class="thermal-mode-label">{state.daikin_mode || "—"}</span>
      </div>

      <div class="thermal-rows">
        <ThermalRow icon="♨" label="Tank" temp={state.tank_c} />
        <ThermalRow icon="🏠" label="Indoor" temp={state.indoor_c} />
        <ThermalRow icon="💧" label="LWT" temp={state.lwt_c} sub="leaving water" />
      </div>
    </div>
  );
}

function ThermalRow({ icon, label, temp, sub }: { icon: string; label: string; temp: number | null; sub?: string }) {
  let tone = "var(--text-dim)";
  if (temp != null) {
    if (temp < 35) tone = "var(--accent)";
    else if (temp < 45) tone = "var(--ok)";
    else if (temp < 55) tone = "var(--warn)";
    else tone = "var(--bad)";
  }
  return (
    <div class="thermal-row">
      <span class="thermal-row-icon">{icon}</span>
      <span class="thermal-row-label">{label}</span>
      {sub && <span class="thermal-row-sub">{sub}</span>}
      <span class="thermal-row-temp" style={{ color: tone }}>{tempC(temp, 0)}</span>
    </div>
  );
}
