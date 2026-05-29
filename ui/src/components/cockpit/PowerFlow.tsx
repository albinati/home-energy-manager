import { watts } from "../../lib/format";
import { reducedMotion } from "../../lib/motion";
import type { CockpitState } from "../../lib/types";
import type { JSX } from "preact";
import "./cockpit.css";

interface PowerFlowProps {
  state: CockpitState;
}

// 2x2 node field connected by curves; energy streams as particles whose SPEED
// and DENSITY scale with live kW (the Tesla centerpiece). The power-flow is the
// ONE place domain colour is allowed on icons/nodes — the node literally IS its
// domain. Node values (watts()), positions, and direction logic are unchanged
// from the data; only the rendering is uplifted.
//
// Sign convention (from /cockpit/now):
//   battery_kw > 0 = charging,    < 0 = discharging
//   grid_kw    > 0 = importing,   < 0 = exporting

const ACTIVATION_W = 50;

// Reduced motion — freeze particles into static proportional connector lines.
// Honours the in-app motion override (default on), not just the OS setting.
const RM = reducedMotion();

const NODES = {
  pv:    { x: 110, y: 80 },
  house: { x: 370, y: 80 },
  batt:  { x: 110, y: 230 },
  grid:  { x: 370, y: 230 },
};

// Thin-line node icons (24×24), from the foundation family. Inlined here so
// each can take its node's domain colour as stroke.
const NODE_ICON: Record<"solar" | "house" | "battery" | "grid", JSX.Element> = {
  solar: (
    <>
      <circle cx="12" cy="12" r="4" />
      <path d="M12 3 V5 M12 19 V21 M3 12 H5 M19 12 H21 M5.6 5.6 L7 7 M17 17 L18.4 18.4 M18.4 5.6 L17 7 M7 17 L5.6 18.4" />
    </>
  ),
  house: (
    <>
      <path d="M5 11 L12 5 L19 11 V20 H5 Z" />
      <path d="M10 20 V15 H14 V20" />
    </>
  ),
  battery: (
    <>
      <path d="M3 8 H17 a2 2 0 0 1 2 2 V14 a2 2 0 0 1 -2 2 H3 a2 2 0 0 1 -2 -2 V10 a2 2 0 0 1 2 -2 Z" />
      <path d="M21 11 V13" />
    </>
  ),
  grid: (
    <>
      <path d="M6 21 L9 3 M18 21 L15 3 M9 3 H15" />
      <path d="M7.5 9 L16.5 9 M8 13 L16 13 M9 9 L15 13 M15 9 L9 13" />
    </>
  ),
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

        {/* Background edges — faint, always visible */}
        <BackgroundEdge href="#pf-pv-house" />
        <BackgroundEdge href="#pf-pv-batt" />
        <BackgroundEdge href="#pf-grid-house" />
        <BackgroundEdge href="#pf-batt-house" />
        <BackgroundEdge href="#pf-grid-batt" />

        {/* Active edges with traveling particles (or static lines under RM) */}
        <ActiveEdge pathId="pf-pv-house"   w={pvHouseW}   colorVar="var(--pv)" />
        <ActiveEdge pathId="pf-pv-batt"   w={pvBattW}   colorVar="var(--pv)" />
        <ActiveEdge pathId="pf-grid-house" w={gridHouseW} colorVar="var(--import)" />
        <ActiveEdge pathId="pf-house-grid" w={houseGridW} colorVar="var(--export)" />
        <ActiveEdge pathId="pf-batt-house" w={battHouseW} colorVar="var(--batt)" />
        <ActiveEdge pathId="pf-grid-batt"  w={gridBattW}  colorVar="var(--import)" />

        <Node {...NODES.pv} icon="solar" label="Solar"
              valueW={s.solar_kw * 1000} colorVar="var(--pv)"
              status={solarProducing ? "producing" : "off"} active={solarProducing} />
        <Node {...NODES.house} icon="house" label="House"
              valueW={s.load_kw * 1000} colorVar="var(--house)"
              status="consuming" active={houseConsuming} />
        <Node {...NODES.batt} icon="battery" label="Battery"
              valueW={Math.abs(s.battery_kw) * 1000} colorVar="var(--batt)"
              status={battCharging ? "charging" : battDischarging ? "discharging" : "idle"}
              statusColor={battCharging ? "var(--ok)" : battDischarging ? "var(--warn)" : "var(--text-mute)"}
              active={battCharging || battDischarging} />
        <Node {...NODES.grid} icon="grid" label="Grid"
              valueW={Math.abs(s.grid_kw) * 1000}
              colorVar={gridExporting ? "var(--export)" : gridImporting ? "var(--import)" : "var(--grid)"}
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
         stroke-dasharray="3 6" opacity="0.3" />
  );
}

function ActiveEdge({ pathId, w, colorVar }: { pathId: string; w: number; colorVar: string }) {
  if (w < ACTIVATION_W) return null;
  // kW → speed + density + size. Particle travel time ∝ 1/power, so higher
  // flow = visibly faster particles. Wide range (0.5s–3.0s) and a low fast-end
  // clamp so a >4 kW import/export streams noticeably quicker than 2 kW, which
  // is quicker than 1 kW (operator-requested proportional motion).
  //   ~800 W → 3.0s · 1 kW → 2.4s · 2 kW → 1.2s · 4 kW → 0.6s · ≥4.8 kW → 0.5s
  const dur = Math.max(0.5, Math.min(3.0, 2400 / w));
  const particleCount = w > 3000 ? 5 : w > 2000 ? 4 : w > 1000 ? 3 : w > 400 ? 2 : 1;
  const r = 3 + Math.min(1.5, w / 3000);
  const glowOpacity = 0.35 + Math.min(0.25, w / 4000);

  return (
    <g>
      <use href={`#${pathId}`} stroke={colorVar} stroke-width="3" fill="none"
           opacity={glowOpacity} filter="url(#pf-glow)" />
      <use href={`#${pathId}`} stroke={colorVar} stroke-width="1.5" fill="none"
           opacity="0.9" />
      {/* Reduced motion: no particles — the active edge above is already a
          proportional-weight static line; render a kW label at its midpoint. */}
      {RM ? (
        <StaticFlowLabel pathId={pathId} w={w} colorVar={colorVar} />
      ) : (
        Array.from({ length: particleCount }).map((_, i) => {
          const offset = (i / particleCount) * dur;
          return (
            <circle key={i} r={r} fill={colorVar} filter="url(#pf-glow)">
              <animateMotion dur={`${dur}s`} repeatCount="indefinite" begin={`-${offset}s`} rotate="auto">
                <mpath href={`#${pathId}`} />
              </animateMotion>
            </circle>
          );
        })
      )}
    </g>
  );
}

// Midpoints of each curved path (approx) for the reduced-motion kW labels.
const PATH_MID: Record<string, { x: number; y: number }> = {
  "pf-pv-house": { x: 240, y: 64 },
  "pf-pv-batt": { x: 90, y: 155 },
  "pf-grid-house": { x: 378, y: 155 },
  "pf-house-grid": { x: 362, y: 155 },
  "pf-batt-house": { x: 240, y: 140 },
  "pf-grid-batt": { x: 240, y: 238 },
};
function StaticFlowLabel({ pathId, w, colorVar }: { pathId: string; w: number; colorVar: string }) {
  const m = PATH_MID[pathId];
  if (!m) return null;
  return (
    <text x={m.x} y={m.y} text-anchor="middle" fill={colorVar}
          font-size="9.5" font-weight="600" font-variant-numeric="tabular-nums">
      {watts(w)}
    </text>
  );
}

interface NodeProps {
  x: number; y: number;
  icon: keyof typeof NODE_ICON;
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
      {/* Static depth — active nodes get a domain-tinted ring + soft shadow.
          No breathing pulse: liveness lives in the particles, not the nodes. */}
      <circle cx={x} cy={y} r={r}
              fill="var(--bg-card-2)"
              stroke={active ? colorVar : "var(--border)"}
              stroke-width={active ? 2 : 1.5}
              style={active ? { filter: `drop-shadow(0 0 8px ${colorVar}33)` } : undefined} />
      {/* Thin-line domain icon (the sanctioned colour exception) */}
      <svg x={x - 10} y={y - 19} width="20" height="20" viewBox="0 0 24 24"
           fill="none" stroke={active ? colorVar : "var(--text-dim)"}
           stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
        {NODE_ICON[icon]}
      </svg>
      <text x={x} y={y + 8} text-anchor="middle" fill="var(--text)" font-size="9.5" font-weight="600">{label}</text>
      <text x={x} y={y + 20} text-anchor="middle" fill="var(--text-dim)" font-size="9"
            font-variant-numeric="tabular-nums">
        {watts(Math.abs(valueW))}
      </text>
      <text x={x} y={y + r + 17} text-anchor="middle"
            fill={statusColor || "var(--text-mute)"} font-size="9" font-weight="500">
        {status}
      </text>
    </g>
  );
}
