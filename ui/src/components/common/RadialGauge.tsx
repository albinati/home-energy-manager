import "./radial-gauge.css";

interface RadialGaugeProps {
  label: string;
  value: number | null | undefined;
  min: number;
  max: number;
  target?: number | null;
  unit?: string;
  tone?: "thermal" | "cool";
}

// Near-full-circle instrument dial (redesign "Tesla-clean radial", P4b): a 276°
// sweep (−138°→138°, notch at the bottom) with a faint track, a domain-coloured
// progress arc, a clean filled head dot at the progress end, a radial set-mark
// tick at the target, and min/max limit labels at the foot. Geometry is pure
// trig (no getTotalLength / ref) so it renders identically everywhere.
const SIZE = 132;
const CX = SIZE / 2;
const CY = SIZE / 2;
const R = SIZE * 0.37;
const A0 = -138;
const A1 = 138;
const SWEEP = A1 - A0;

function frac(v: number, min: number, max: number): number {
  if (max <= min) return 0;
  return Math.max(0, Math.min(1, (v - min) / (max - min)));
}

// 0° points up (12 o'clock); positive is clockwise.
function gPolar(cx: number, cy: number, r: number, deg: number): [number, number] {
  const a = ((deg - 90) * Math.PI) / 180;
  return [cx + r * Math.cos(a), cy + r * Math.sin(a)];
}
function gArc(cx: number, cy: number, r: number, a0: number, a1: number): string {
  const [x0, y0] = gPolar(cx, cy, r, a0);
  const [x1, y1] = gPolar(cx, cy, r, a1);
  const large = Math.abs(a1 - a0) > 180 ? 1 : 0;
  return `M ${x0.toFixed(1)} ${y0.toFixed(1)} A ${r} ${r} 0 ${large} 1 ${x1.toFixed(1)} ${y1.toFixed(1)}`;
}

export function RadialGauge({ label, value, min, max, target, unit = "°", tone = "thermal" }: RadialGaugeProps) {
  const has = value != null && Number.isFinite(value);
  const t = has ? frac(value as number, min, max) : 0;
  const ang = A0 + t * SWEEP;

  const [lx0, ly0] = gPolar(CX, CY, R, A0);
  const [lx1, ly1] = gPolar(CX, CY, R, A1);
  const yBase = Math.max(ly0, ly1) + 13; // one shared baseline for both limits
  const [hx, hy] = gPolar(CX, CY, R, ang); // progress head — a clean filled dot

  let tick: { x0: number; y0: number; x1: number; y1: number; dx: number; dy: number } | null = null;
  if (target != null && Number.isFinite(target)) {
    const sa = A0 + frac(target, min, max) * SWEEP;
    const [x0, y0] = gPolar(CX, CY, R - 7, sa);
    const [x1, y1] = gPolar(CX, CY, R + 7, sa);
    const [dx, dy] = gPolar(CX, CY, R + 13, sa);
    tick = { x0, y0, x1, y1, dx, dy };
  }

  return (
    <div class={`rgauge rgauge--${tone}`}>
      <svg viewBox={`0 0 ${SIZE} ${SIZE * 0.9}`} class="rgauge-svg" aria-hidden="true">
        <path d={gArc(CX, CY, R, A0, A1)} class="rgauge-track" />
        {has && <path d={gArc(CX, CY, R, A0, ang)} class="rgauge-fill" />}
        {has && <circle cx={hx} cy={hy} r="4.5" class="rgauge-head" />}
        {tick && <line x1={tick.x0} y1={tick.y0} x2={tick.x1} y2={tick.y1} class="rgauge-tick" />}
        {tick && <circle cx={tick.dx} cy={tick.dy} r="1.6" class="rgauge-tick-dot" />}
        <text x={CX} y={CY + 1} text-anchor="middle" dominant-baseline="middle" class="rgauge-v">
          {has ? Math.round(value as number) : "—"}
          <tspan class="rgauge-deg" dy="-7">{unit}</tspan>
        </text>
        <text x={lx0} y={yBase} text-anchor="middle" class="rgauge-lim">{min}{unit}</text>
        <text x={lx1} y={yBase} text-anchor="middle" class="rgauge-lim">{max}{unit}</text>
      </svg>
      <div class="rgauge-label">
        {label}{target != null ? ` · set ${Math.round(target)}${unit}` : ""}
      </div>
    </div>
  );
}
