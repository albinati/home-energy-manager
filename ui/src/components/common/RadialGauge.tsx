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

// Top-semicircle arc (sweep=1 verified). pathLength=100 lets us fill a fraction
// with stroke-dasharray and place the target tick with dashoffset — no
// getTotalLength / ref needed, so it renders correctly everywhere.
const CX = 60, CY = 62, R = 50;
const ARC = `M ${CX - R} ${CY} A ${R} ${R} 0 0 1 ${CX + R} ${CY}`;

function frac(v: number, min: number, max: number): number {
  if (max <= min) return 0;
  return Math.max(0, Math.min(1, (v - min) / (max - min)));
}

// Onecta-style dial: a 180° arc that fills to the current value, a tick at the
// target, and the reading in the centre.
export function RadialGauge({ label, value, min, max, target, unit = "°", tone = "thermal" }: RadialGaugeProps) {
  const has = value != null && Number.isFinite(value);
  const f = has ? frac(value as number, min, max) : 0;
  const tf = target != null && Number.isFinite(target) ? frac(target, min, max) : null;
  return (
    <div class={`rgauge rgauge--${tone}`}>
      <svg viewBox="0 0 120 72" class="rgauge-svg" aria-hidden="true">
        <path d={ARC} class="rgauge-track" pathLength="100" />
        {has && (
          <path d={ARC} class="rgauge-fill" pathLength="100"
                stroke-dasharray={`${(f * 100).toFixed(1)} 100`} />
        )}
        {tf != null && (
          <path d={ARC} class="rgauge-target" pathLength="100"
                stroke-dasharray="1.2 100" stroke-dashoffset={`${-(tf * 100)}`} />
        )}
      </svg>
      <div class="rgauge-center">
        <span class="rgauge-value">{has ? `${Math.round(value as number)}${unit}` : "—"}</span>
        <span class="rgauge-label">
          {label}{target != null ? ` · set ${Math.round(target)}${unit}` : ""}
        </span>
      </div>
    </div>
  );
}
