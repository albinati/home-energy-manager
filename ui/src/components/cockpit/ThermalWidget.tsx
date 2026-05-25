import { tempC } from "../../lib/format";
import type { CockpitState } from "../../lib/types";

interface ThermalWidgetProps {
  state: CockpitState;
}

// Tank / indoor / leaving-water + Daikin mode. Heating flame animates when
// the heat pump is in a heating mode. Tank shown as a stylised cylinder
// with a fill that scales to its temperature within a comfort range.
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

      <div class="thermal-grid">
        <TankGauge tempC={state.tank_c} />
        <ThermalMetric icon="🏠" label="Indoor" value={tempC(state.indoor_c, 0)} />
        <ThermalMetric icon="💧" label="LWT" value={tempC(state.lwt_c, 0)} subtitle="leaving water" />
      </div>
    </div>
  );
}

interface ThermalMetricProps {
  icon: string;
  label: string;
  value: string;
  subtitle?: string;
}

function ThermalMetric({ icon, label, value, subtitle }: ThermalMetricProps) {
  return (
    <div class="thermal-metric">
      <div class="thermal-metric-icon">{icon}</div>
      <div class="thermal-metric-label">{label}</div>
      <div class="thermal-metric-value">{value}</div>
      {subtitle && <div class="thermal-metric-sub">{subtitle}</div>}
    </div>
  );
}

// Tank rendered as a cylinder. Fill height maps temperature into a
// comfort band — 20 °C (cold) → 65 °C (hot).
function TankGauge({ tempC: t }: { tempC: number | null }) {
  if (t == null) return <ThermalMetric icon="♨" label="Tank" value="—" />;
  const lo = 20;
  const hi = 65;
  const pct = Math.max(0, Math.min(1, (t - lo) / (hi - lo)));
  let color = "var(--standard)";
  if (t < 35) color = "var(--accent)";       // cold blue
  else if (t < 45) color = "var(--ok)";      // warm green
  else if (t < 55) color = "var(--warn)";    // hot orange
  else color = "var(--bad)";                  // very hot red

  const W = 44;
  const H = 80;
  const margin = 4;
  const bodyH = H - margin * 2;
  const fillH = pct * (bodyH - 4);

  return (
    <div class="thermal-metric">
      <svg viewBox={`0 0 ${W} ${H}`} width={W} height={H} class="thermal-tank-svg" aria-hidden="true">
        <defs>
          <linearGradient id="tank-fill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color={color} stop-opacity="0.7" />
            <stop offset="100%" stop-color={color} stop-opacity="1" />
          </linearGradient>
        </defs>
        {/* Tank cylinder outline */}
        <ellipse cx={W / 2} cy={margin + 4} rx={(W - margin * 2) / 2} ry={4}
                 fill="var(--bg)" stroke="var(--border-strong)" stroke-width="1.5" />
        <rect x={margin} y={margin + 4} width={W - margin * 2} height={bodyH - 4}
              fill="var(--bg)" stroke="var(--border-strong)" stroke-width="1.5" />
        <ellipse cx={W / 2} cy={H - margin} rx={(W - margin * 2) / 2} ry={4}
                 fill="var(--bg)" stroke="var(--border-strong)" stroke-width="1.5" />
        {/* Fill */}
        <rect
          x={margin + 2}
          y={H - margin - fillH - 2}
          width={W - margin * 2 - 4}
          height={fillH}
          fill="url(#tank-fill)"
          style={{ transition: "y 600ms ease, height 600ms ease, fill 200ms ease" }}
        />
        <ellipse cx={W / 2} cy={H - margin - fillH - 2} rx={(W - margin * 2 - 4) / 2} ry={3}
                 fill={color} opacity="0.85"
                 style={{ transition: "cy 600ms ease, fill 200ms ease" }} />
      </svg>
      <div class="thermal-metric-label">Tank</div>
      <div class="thermal-metric-value">{tempC(t, 0)}</div>
    </div>
  );
}
