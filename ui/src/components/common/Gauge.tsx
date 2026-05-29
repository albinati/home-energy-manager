import "./gauge.css";

interface GaugeProps {
  label: string;
  value: number | null | undefined;
  min: number;
  max: number;
  target?: number | null;
  unit?: string;
  tone?: "thermal" | "cool" | "neutral";
  sub?: string;
}

function clampFrac(v: number, min: number, max: number): number {
  if (max <= min) return 0;
  return Math.max(0, Math.min(1, (v - min) / (max - min)));
}

// Horizontal bar gauge: track + fill to `value`, optional tick at `target`,
// big readout above. Pure HTML/CSS — no canvas/SVG, so it renders correctly
// everywhere and is cheap on the eager Home critical path.
export function Gauge({ label, value, min, max, target, unit = "°C", tone = "thermal", sub }: GaugeProps) {
  const has = value != null && Number.isFinite(value);
  const frac = has ? clampFrac(value as number, min, max) : 0;
  const targetFrac = target != null && Number.isFinite(target) ? clampFrac(target, min, max) : null;
  return (
    <div class={`gauge gauge--${tone}`}>
      <div class="gauge-top">
        <span class="gauge-label">{label}</span>
        <span class="gauge-value">{has ? `${(value as number).toFixed(0)}${unit}` : "—"}</span>
      </div>
      <div class="gauge-track">
        <div class="gauge-fill" style={{ width: `${(frac * 100).toFixed(1)}%` }} />
        {targetFrac != null && (
          <div class="gauge-target" style={{ left: `${(targetFrac * 100).toFixed(1)}%` }}
               title={`target ${target!.toFixed(0)}${unit}`} />
        )}
      </div>
      {sub && <span class="gauge-sub">{sub}</span>}
    </div>
  );
}
