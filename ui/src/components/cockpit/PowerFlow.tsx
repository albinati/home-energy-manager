import { watts } from "../../lib/format";
import type { CockpitState } from "../../lib/types";

interface PowerFlowProps {
  state: CockpitState;
}

// Diamond layout:
//        ☀ PV (top)
//       ↙    ↘
//   🔋 Batt   🏠 House
//       ↘    ↙
//        ⚡ Grid (bottom)
//
// Sign convention (from /cockpit/now):
//   solar_kw   ≥ 0 always
//   load_kw    ≥ 0 always
//   battery_kw > 0 = charging, < 0 = discharging
//   grid_kw    > 0 = importing, < 0 = exporting
//
// Edges render only when active. Active edges animate (stroke-dashoffset)
// and carry an arrowhead. Inactive edges render as faint static lines so the
// topology is always visible.

const THRESHOLD_W = 50;

export function PowerFlow({ state }: PowerFlowProps) {
  // Compute per-edge watts (positive in the named direction).
  const pvHouseW = state.solar_kw > 0 ? Math.max(0, state.solar_kw * 1000 - Math.max(0, state.battery_kw * 1000)) : 0;
  const pvBattW = state.solar_kw > 0 && state.battery_kw > 0 ? state.battery_kw * 1000 : 0;
  const gridHouseW = state.grid_kw > 0 ? state.grid_kw * 1000 : 0;
  const houseGridW = state.grid_kw < 0 ? -state.grid_kw * 1000 : 0;
  const battHouseW = state.battery_kw < 0 ? -state.battery_kw * 1000 : 0;
  const gridBattW = state.grid_kw > 0 && state.battery_kw > 0 ? Math.min(state.grid_kw, state.battery_kw) * 1000 : 0;

  const gridImporting = state.grid_kw > 0.05;
  const gridExporting = state.grid_kw < -0.05;
  const battCharging = state.battery_kw > 0.05;
  const battDischarging = state.battery_kw < -0.05;

  return (
    <div class="powerflow" aria-label="Live power flow">
      <svg viewBox="0 0 420 260" class="powerflow-svg" aria-hidden="true">
        <defs>
          {/* Arrowheads — one per colour. */}
          <marker id="arrow-pv" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M0,0 L10,5 L0,10 z" fill="var(--pv)" />
          </marker>
          <marker id="arrow-batt" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M0,0 L10,5 L0,10 z" fill="var(--batt)" />
          </marker>
          <marker id="arrow-import" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M0,0 L10,5 L0,10 z" fill="var(--import)" />
          </marker>
          <marker id="arrow-export" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
            <path d="M0,0 L10,5 L0,10 z" fill="var(--export)" />
          </marker>
        </defs>

        {/* PV → House (top to right) */}
        <FlowEdge d="M 130 60 Q 250 70 320 130" colorVar="var(--pv)" w={pvHouseW} marker="arrow-pv" />
        {/* PV → Battery (top to left) */}
        <FlowEdge d="M 110 65 Q 60 130 100 195" colorVar="var(--pv)" w={pvBattW} marker="arrow-pv" />
        {/* Grid → House (bottom to right) */}
        <FlowEdge d="M 320 200 Q 350 165 330 130" colorVar="var(--import)" w={gridHouseW} marker="arrow-import" />
        {/* House → Grid (right to bottom) — export */}
        <FlowEdge d="M 320 140 Q 350 175 320 210" colorVar="var(--export)" w={houseGridW} marker="arrow-export" />
        {/* Battery → House (left to right) — discharge */}
        <FlowEdge d="M 130 200 Q 240 220 320 145" colorVar="var(--batt)" w={battHouseW} marker="arrow-batt" />
        {/* Grid → Battery (bottom to left) — force-charging from grid */}
        <FlowEdge d="M 220 220 Q 150 230 110 210" colorVar="var(--import)" w={gridBattW} marker="arrow-import" />

        {/* Nodes — render last so edges underneath */}
        <Node x={120} y={60}
              icon="☀"
              label="Solar"
              valueW={state.solar_kw * 1000}
              colorVar="var(--pv)"
              status={state.solar_kw > 0.05 ? "producing" : "off"} />
        <Node x={120} y={210}
              icon="🔋"
              label="Battery"
              valueW={Math.abs(state.battery_kw) * 1000}
              colorVar="var(--batt)"
              status={battCharging ? `charging` : battDischarging ? `discharging` : "idle"}
              statusColor={battCharging ? "var(--ok)" : battDischarging ? "var(--warn)" : "var(--text-mute)"} />
        <Node x={325} y={130}
              icon="🏠"
              label="House"
              valueW={state.load_kw * 1000}
              colorVar="var(--house)"
              status="using" />
        <Node x={325} y={215}
              icon="⚡"
              label="Grid"
              valueW={Math.abs(state.grid_kw) * 1000}
              colorVar={gridExporting ? "var(--export)" : gridImporting ? "var(--import)" : "var(--grid)"}
              status={gridExporting ? "exporting" : gridImporting ? "importing" : "idle"}
              statusColor={gridExporting ? "var(--export)" : gridImporting ? "var(--import)" : "var(--text-mute)"} />
      </svg>
    </div>
  );
}

interface FlowEdgeProps {
  d: string;
  colorVar: string;
  w: number;
  marker: string;
}

function FlowEdge({ d, colorVar, w, marker }: FlowEdgeProps) {
  const active = w > THRESHOLD_W;
  return (
    <path
      d={d}
      stroke={colorVar}
      stroke-width={active ? 2.5 : 1}
      fill="none"
      stroke-dasharray={active ? "8 6" : "3 4"}
      opacity={active ? 1 : 0.18}
      marker-end={active ? `url(#${marker})` : undefined}
      style={active ? { animation: "pf-dash 1.4s linear infinite" } : undefined}
    />
  );
}

interface NodeProps {
  x: number;
  y: number;
  icon: string;
  label: string;
  valueW: number;
  colorVar: string;
  status: string;
  statusColor?: string;
}

function Node({ x, y, icon, label, valueW, colorVar, status, statusColor }: NodeProps) {
  const r = 32;
  return (
    <g>
      <circle cx={x} cy={y} r={r} fill="var(--bg-card-2)" stroke={colorVar} stroke-width="2" />
      <text x={x} y={y - 9} text-anchor="middle" font-size="16">{icon}</text>
      <text x={x} y={y + 7} text-anchor="middle" fill="var(--text)" font-size="10.5" font-weight="600">{label}</text>
      <text
        x={x}
        y={y + 20}
        text-anchor="middle"
        fill="var(--text-dim)"
        font-size="9.5"
        font-variant-numeric="tabular-nums"
      >
        {watts(valueW)}
      </text>
      <text
        x={x}
        y={y + r + 14}
        text-anchor="middle"
        fill={statusColor || "var(--text-mute)"}
        font-size="9.5"
        font-weight="500"
      >
        {status}
      </text>
    </g>
  );
}
