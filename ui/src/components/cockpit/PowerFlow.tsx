import { watts } from "../../lib/format";
import type { CockpitState } from "../../lib/types";

interface PowerFlowProps {
  state: CockpitState;
}

// Diamond layout with truly-animated flow:
//   - Active edges carry SVG <circle> particles that travel along the path
//     via <animateMotion mpath>. Particle speed scales with watts (faster
//     when more power flows). Multiple particles per edge for a "stream"
//     feel.
//   - Source/sink nodes pulse when actively producing/consuming.
//
//        ☀ PV (top)
//       ↙    ↘
//   🔋 Batt   🏠 House
//       ↘    ↙
//        ⚡ Grid (bottom)
//
// Signs from /cockpit/now:
//   battery_kw > 0 = charging,    < 0 = discharging
//   grid_kw    > 0 = importing,   < 0 = exporting

const ACTIVATION_W = 50;

export function PowerFlow({ state }: PowerFlowProps) {
  // Per-edge watts (positive in the named direction).
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
  const solarProducing = state.solar_kw > 0.05;
  const houseConsuming = state.load_kw > 0.05;

  return (
    <div class="powerflow" aria-label="Live power flow">
      <svg viewBox="0 0 440 280" class="powerflow-svg" aria-hidden="true">
        <defs>
          <path id="pf-pv-house"   d="M 130 70 Q 250 80 320 140" />
          <path id="pf-pv-batt"   d="M 110 75 Q 60 140 100 200" />
          <path id="pf-grid-house" d="M 320 215 Q 350 175 325 140" />
          <path id="pf-house-grid" d="M 325 140 Q 350 175 320 215" />
          <path id="pf-batt-house" d="M 130 200 Q 240 230 320 145" />
          <path id="pf-grid-batt"  d="M 290 230 Q 200 245 110 215" />

          <filter id="pf-glow" x="-50%" y="-50%" width="200%" height="200%">
            <feGaussianBlur stdDeviation="3" result="blur" />
            <feMerge>
              <feMergeNode in="blur" />
              <feMergeNode in="SourceGraphic" />
            </feMerge>
          </filter>

          <radialGradient id="pf-pulse-grad">
            <stop offset="0%" stop-color="var(--accent)" stop-opacity="0.6" />
            <stop offset="100%" stop-color="var(--accent)" stop-opacity="0" />
          </radialGradient>
        </defs>

        {/* Background dashed paths (always shown) */}
        <BackgroundEdge href="#pf-pv-house" />
        <BackgroundEdge href="#pf-pv-batt" />
        <BackgroundEdge href="#pf-grid-house" />
        <BackgroundEdge href="#pf-batt-house" />
        <BackgroundEdge href="#pf-grid-batt" />

        {/* Active edges with traveling particles */}
        <ActiveEdge pathId="pf-pv-house"   w={pvHouseW}   colorVar="var(--pv)" />
        <ActiveEdge pathId="pf-pv-batt"   w={pvBattW}   colorVar="var(--pv)" />
        <ActiveEdge pathId="pf-grid-house" w={gridHouseW} colorVar="var(--import)" />
        <ActiveEdge pathId="pf-house-grid" w={houseGridW} colorVar="var(--export)" />
        <ActiveEdge pathId="pf-batt-house" w={battHouseW} colorVar="var(--batt)" />
        <ActiveEdge pathId="pf-grid-batt"  w={gridBattW}  colorVar="var(--import)" />

        {/* Nodes */}
        <Node x={120} y={70}  icon="☀"  label="Solar"   valueW={state.solar_kw * 1000}      colorVar="var(--pv)"
              status={solarProducing ? "producing" : "off"}
              active={solarProducing} />
        <Node x={120} y={215} icon="🔋" label="Battery" valueW={Math.abs(state.battery_kw) * 1000} colorVar="var(--batt)"
              status={battCharging ? "charging" : battDischarging ? "discharging" : "idle"}
              statusColor={battCharging ? "var(--ok)" : battDischarging ? "var(--warn)" : "var(--text-mute)"}
              active={battCharging || battDischarging} />
        <Node x={325} y={140} icon="🏠" label="House"   valueW={state.load_kw * 1000}        colorVar="var(--house)"
              status="consuming"
              active={houseConsuming} />
        <Node x={325} y={215} icon="⚡" label="Grid"    valueW={Math.abs(state.grid_kw) * 1000} colorVar="var(--grid)"
              status={gridExporting ? "exporting" : gridImporting ? "importing" : "idle"}
              statusColor={gridExporting ? "var(--export)" : gridImporting ? "var(--import)" : "var(--text-mute)"}
              active={gridImporting || gridExporting} />
      </svg>
    </div>
  );
}

function BackgroundEdge({ href }: { href: string }) {
  return (
    <use href={href} stroke="var(--border)" stroke-width="1.5" fill="none"
         stroke-dasharray="3 6" opacity="0.35" />
  );
}

interface ActiveEdgeProps {
  pathId: string;
  w: number;
  colorVar: string;
}

function ActiveEdge({ pathId, w, colorVar }: ActiveEdgeProps) {
  if (w < ACTIVATION_W) return null;

  // Particle travel duration scales inversely with watts (capped 0.8s..3s).
  const dur = Math.max(0.8, Math.min(3, 4000 / w));
  // More watts = more particles.
  const particleCount = w > 1500 ? 3 : w > 500 ? 2 : 1;

  return (
    <g>
      {/* Glowing live path under particles */}
      <use href={`#${pathId}`} stroke={colorVar} stroke-width="3" fill="none"
           opacity="0.45" filter="url(#pf-glow)" />
      <use href={`#${pathId}`} stroke={colorVar} stroke-width="1.5" fill="none"
           opacity="0.9" />

      {/* Particles travelling along path */}
      {Array.from({ length: particleCount }).map((_, i) => {
        const offset = (i / particleCount) * dur;
        return (
          <circle key={i} r="3.5" fill={colorVar} filter="url(#pf-glow)">
            <animateMotion
              dur={`${dur}s`}
              repeatCount="indefinite"
              begin={`-${offset}s`}
              rotate="auto"
            >
              <mpath href={`#${pathId}`} />
            </animateMotion>
          </circle>
        );
      })}
    </g>
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
  active?: boolean;
}

function Node({ x, y, icon, label, valueW, colorVar, status, statusColor, active }: NodeProps) {
  const r = 34;
  return (
    <g>
      {active && (
        <circle cx={x} cy={y} r={r + 6}
                fill="none" stroke={colorVar}
                stroke-width="1.5" opacity="0.5"
                style={{ animation: "pf-node-pulse 2.2s ease-in-out infinite" }} />
      )}
      <circle cx={x} cy={y} r={r}
              fill="var(--bg-card-2)" stroke={colorVar} stroke-width="2"
              style={active ? { filter: "drop-shadow(0 0 8px " + colorVar + "55)" } : undefined} />
      <text x={x} y={y - 10} text-anchor="middle" font-size="17">{icon}</text>
      <text x={x} y={y + 7} text-anchor="middle" fill="var(--text)" font-size="11" font-weight="600">{label}</text>
      <text x={x} y={y + 21} text-anchor="middle" fill="var(--text-dim)" font-size="10"
            font-variant-numeric="tabular-nums">
        {watts(Math.abs(valueW))}
      </text>
      <text x={x} y={y + r + 16} text-anchor="middle"
            fill={statusColor || "var(--text-mute)"} font-size="10" font-weight="500">
        {status}
      </text>
    </g>
  );
}
