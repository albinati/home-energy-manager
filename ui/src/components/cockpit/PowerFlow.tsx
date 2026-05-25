import { watts } from "../../lib/format";
import type { CockpitState } from "../../lib/types";

interface PowerFlowProps {
  state: CockpitState;
}

// Power-flow diagram. Four nodes in a diamond:
//   PV (top), House (right), Grid (bottom), Battery (left).
// Edges animate (stroke-dasharray) when flow > ~50W. Arrow direction follows
// the sign convention from /cockpit/now:
//   battery_kw > 0 = charging (PV/grid → battery)
//   grid_kw    > 0 = importing (grid → house)
export function PowerFlow({ state }: PowerFlowProps) {
  const flows = [
    edge("pv-house", state.solar_kw > 0.05 ? state.solar_kw * 1000 : 0, "var(--pv)"),
    edge("pv-batt", state.solar_kw > 0.05 && state.battery_kw > 0.05 ? state.battery_kw * 1000 : 0, "var(--pv)"),
    edge("grid-house", state.grid_kw > 0.05 ? state.grid_kw * 1000 : 0, "var(--import)"),
    edge("house-grid", state.grid_kw < -0.05 ? -state.grid_kw * 1000 : 0, "var(--export)"),
    edge("batt-house", state.battery_kw < -0.05 ? -state.battery_kw * 1000 : 0, "var(--batt)"),
  ];

  return (
    <div class="powerflow" aria-label="Live power flow">
      <svg viewBox="0 0 380 240" class="powerflow-svg" aria-hidden="true">
        <defs>
          <filter id="pf-glow" x="-50%" y="-50%" width="200%" height="200%">
            <feGaussianBlur stdDeviation="2" result="blur" />
            <feMerge>
              <feMergeNode in="blur" />
              <feMergeNode in="SourceGraphic" />
            </feMerge>
          </filter>
        </defs>

        {/* Edges drawn beneath nodes */}
        {/* PV → House */}
        <path
          d="M 100 50 Q 200 60 280 120"
          stroke={flows[0].color}
          stroke-width={flows[0].active ? 3 : 1.5}
          fill="none"
          stroke-dasharray="8 6"
          opacity={flows[0].active ? 1 : 0.18}
          style={flows[0].active ? { animation: "pf-dash 1.4s linear infinite" } : {}}
          filter={flows[0].active ? "url(#pf-glow)" : undefined}
        />
        {/* PV → Battery */}
        <path
          d="M 100 50 Q 60 120 100 190"
          stroke={flows[1].color}
          stroke-width={flows[1].active ? 3 : 1.5}
          fill="none"
          stroke-dasharray="8 6"
          opacity={flows[1].active ? 1 : 0.15}
          style={flows[1].active ? { animation: "pf-dash 1.6s linear infinite" } : {}}
          filter={flows[1].active ? "url(#pf-glow)" : undefined}
        />
        {/* Grid → House */}
        <path
          d="M 280 220 Q 290 175 285 130"
          stroke={flows[2].color}
          stroke-width={flows[2].active ? 3 : 1.5}
          fill="none"
          stroke-dasharray="8 6"
          opacity={flows[2].active ? 1 : 0.15}
          style={flows[2].active ? { animation: "pf-dash 1.4s linear infinite" } : {}}
          filter={flows[2].active ? "url(#pf-glow)" : undefined}
        />
        {/* House → Grid (export) */}
        <path
          d="M 285 130 Q 290 175 280 220"
          stroke={flows[3].color}
          stroke-width={flows[3].active ? 3 : 1.5}
          fill="none"
          stroke-dasharray="8 6"
          opacity={flows[3].active ? 1 : 0.15}
          style={flows[3].active ? { animation: "pf-dash-rev 1.4s linear infinite" } : {}}
          filter={flows[3].active ? "url(#pf-glow)" : undefined}
        />
        {/* Battery → House */}
        <path
          d="M 100 190 Q 200 200 280 120"
          stroke={flows[4].color}
          stroke-width={flows[4].active ? 3 : 1.5}
          fill="none"
          stroke-dasharray="8 6"
          opacity={flows[4].active ? 1 : 0.15}
          style={flows[4].active ? { animation: "pf-dash 1.4s linear infinite" } : {}}
          filter={flows[4].active ? "url(#pf-glow)" : undefined}
        />

        {/* Nodes */}
        <g>{node(100, 50, "PV", state.solar_kw * 1000, "var(--pv)")}</g>
        <g>{node(285, 130, "House", -state.load_kw * 1000, "var(--house)", true)}</g>
        <g>{node(280, 220, "Grid", state.grid_kw * 1000, "var(--grid)")}</g>
        <g>{node(100, 190, "Battery", state.battery_kw * 1000, "var(--batt)")}</g>
      </svg>
    </div>
  );
}

function edge(_id: string, watts_: number, color: string) {
  return { active: watts_ > 50, color, watts: watts_ };
}

function node(
  x: number,
  y: number,
  label: string,
  watts_: number,
  color: string,
  loadStyle = false,
) {
  // For load node we display |w|, since load_kw is positive in /cockpit/now
  // but we pass it negated above for visual logic.
  const display = loadStyle ? watts(Math.abs(watts_)) : watts(Math.abs(watts_));
  return (
    <>
      <circle cx={x} cy={y} r="26" fill="var(--bg-card-2)" stroke={color} stroke-width="2" />
      <text x={x} y={y - 2} text-anchor="middle" fill="var(--text)" font-size="12" font-weight="600">
        {label}
      </text>
      <text x={x} y={y + 13} text-anchor="middle" fill="var(--text-dim)" font-size="10" font-variant-numeric="tabular-nums">
        {display}
      </text>
    </>
  );
}
