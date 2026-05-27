import { watts } from "../../lib/format";
import type { CockpitState } from "../../lib/types";
import "./cockpit.css";

interface PowerFlowProps {
  state: CockpitState;
}

// 2x2 grid layout — eliminates the diamond overlap by giving each node its
// own quadrant. Labels live BELOW each node with plenty of whitespace.
// Particles travel along the connecting curves.
//
//   ☀ Solar (top-left)     🏠 House (top-right)
//        ╲                 ╱
//         ╲               ╱
//   🔋 Battery (bot-left)  ⚡ Grid (bot-right)
//
// Sign convention (from /cockpit/now):
//   battery_kw > 0 = charging,    < 0 = discharging
//   grid_kw    > 0 = importing,   < 0 = exporting

const ACTIVATION_W = 50;

// Quadrant centers — pick spacing that keeps node circles + labels clear.
const NODES = {
  pv:    { x: 110, y: 80 },
  house: { x: 370, y: 80 },
  batt:  { x: 110, y: 230 },
  grid:  { x: 370, y: 230 },
};

export function PowerFlow({ state }: PowerFlowProps) {
  const s = state;
  const pvHouseW = s.solar_kw > 0 ? Math.max(0, s.solar_kw * 1000 - Math.max(0, s.battery_kw * 1000)) : 0;
  const pvBattW = s.solar_kw > 0 && s.battery_kw > 0 ? s.battery_kw * 1000 : 0;
  const gridHouseW = s.grid_kw > 0 ? s.grid_kw * 1000 : 0;
  const houseGridW = s.grid_kw < 0 ? -s.grid_kw * 1000 : 0;
  const battHouseW = s.battery_kw < 0 ? -s.battery_kw * 1000 : 0;
  const gridBattW = s.grid_kw > 0 && s.battery_kw > 0 ? Math.min(s.grid_kw, s.battery_kw) * 1000 : 0;

  const gridImporting = s.grid_kw > 0.05;
  const gridExporting = s.grid_kw < -0.05;
  const battCharging = s.battery_kw > 0.05;
  const battDischarging = s.battery_kw < -0.05;
  const solarProducing = s.solar_kw > 0.05;
  const houseConsuming = s.load_kw > 0.05;

  // Curved paths between quadrants (SVG q commands)
  const pvToHouse = "M 142 80 C 220 60 280 60 338 80";
  const battToHouse = "M 130 200 C 200 150 280 130 350 95";
  const gridToHouse = "M 370 200 C 380 160 380 130 370 110";
  const houseToGrid = "M 370 110 C 360 140 360 170 370 200";
  const pvToBatt = "M 110 112 C 80 150 80 180 110 198";
  const gridToBatt = "M 338 230 C 280 240 200 240 142 230";

  return (
    <div class="powerflow" aria-label="Live power flow">
      <svg viewBox="0 0 480 310" class="powerflow-svg" aria-hidden="true">
        <defs>
          <Marker id="arrow-pv" colorVar="var(--pv)" />
          <Marker id="arrow-batt" colorVar="var(--batt)" />
          <Marker id="arrow-import" colorVar="var(--import)" />
          <Marker id="arrow-export" colorVar="var(--export)" />
          <filter id="pf-glow" x="-50%" y="-50%" width="200%" height="200%">
            <feGaussianBlur stdDeviation="3" result="blur" />
            <feMerge><feMergeNode in="blur" /><feMergeNode in="SourceGraphic" /></feMerge>
          </filter>
          <path id="pf-pv-house"   d={pvToHouse} />
          <path id="pf-pv-batt"   d={pvToBatt} />
          <path id="pf-grid-house" d={gridToHouse} />
          <path id="pf-house-grid" d={houseToGrid} />
          <path id="pf-batt-house" d={battToHouse} />
          <path id="pf-grid-batt"  d={gridToBatt} />
        </defs>

        {/* Background edges (always visible faintly) */}
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
        <Node {...NODES.pv} icon="☀" label="Solar"
              valueW={s.solar_kw * 1000}
              colorVar="var(--pv)"
              status={solarProducing ? "producing" : "off"}
              active={solarProducing} />
        <Node {...NODES.house} icon="🏠" label="House"
              valueW={s.load_kw * 1000}
              colorVar="var(--house)"
              status="consuming"
              active={houseConsuming} />
        <Node {...NODES.batt} icon="🔋" label="Battery"
              valueW={Math.abs(s.battery_kw) * 1000}
              colorVar="var(--batt)"
              status={battCharging ? "charging" : battDischarging ? "discharging" : "idle"}
              statusColor={battCharging ? "var(--ok)" : battDischarging ? "var(--warn)" : "var(--text-mute)"}
              active={battCharging || battDischarging} />
        <Node {...NODES.grid} icon="⚡" label="Grid"
              valueW={Math.abs(s.grid_kw) * 1000}
              colorVar={gridExporting ? "var(--export)" : gridImporting ? "var(--import)" : "var(--grid)"}
              status={gridExporting ? "exporting" : gridImporting ? "importing" : "idle"}
              statusColor={gridExporting ? "var(--export)" : gridImporting ? "var(--import)" : "var(--text-mute)"}
              active={gridImporting || gridExporting} />
      </svg>
    </div>
  );
}

function Marker({ id, colorVar }: { id: string; colorVar: string }) {
  return (
    <marker id={id} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
      <path d="M0,0 L10,5 L0,10 z" fill={colorVar} />
    </marker>
  );
}

function BackgroundEdge({ href }: { href: string }) {
  return (
    <use href={href} stroke="var(--border)" stroke-width="1.5" fill="none"
         stroke-dasharray="3 6" opacity="0.35" />
  );
}

function ActiveEdge({ pathId, w, colorVar }: { pathId: string; w: number; colorVar: string }) {
  if (w < ACTIVATION_W) return null;
  const dur = Math.max(0.8, Math.min(3, 4000 / w));
  const particleCount = w > 1500 ? 3 : w > 500 ? 2 : 1;
  return (
    <g>
      <use href={`#${pathId}`} stroke={colorVar} stroke-width="3" fill="none"
           opacity="0.45" filter="url(#pf-glow)" />
      <use href={`#${pathId}`} stroke={colorVar} stroke-width="1.5" fill="none"
           opacity="0.9" />
      {Array.from({ length: particleCount }).map((_, i) => {
        const offset = (i / particleCount) * dur;
        return (
          <circle key={i} r="3.5" fill={colorVar} filter="url(#pf-glow)">
            <animateMotion dur={`${dur}s`} repeatCount="indefinite" begin={`-${offset}s`} rotate="auto">
              <mpath href={`#${pathId}`} />
            </animateMotion>
          </circle>
        );
      })}
    </g>
  );
}

interface NodeProps {
  x: number; y: number;
  icon: string;
  label: string;
  valueW: number;
  colorVar: string;
  status: string;
  statusColor?: string;
  active?: boolean;
}

function Node({ x, y, icon, label, valueW, colorVar, status, statusColor, active }: NodeProps) {
  const r = 30;
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
      <text x={x} y={y - 6} text-anchor="middle" font-size="16">{icon}</text>
      <text x={x} y={y + 9} text-anchor="middle" fill="var(--text)" font-size="10" font-weight="600">{label}</text>
      <text x={x} y={y + 22} text-anchor="middle" fill="var(--text-dim)" font-size="9.5"
            font-variant-numeric="tabular-nums">
        {watts(Math.abs(valueW))}
      </text>
      {/* Status label below the circle, far enough not to collide with siblings */}
      <text x={x} y={y + r + 18} text-anchor="middle"
            fill={statusColor || "var(--text-mute)"} font-size="9.5" font-weight="500">
        {status}
      </text>
    </g>
  );
}
