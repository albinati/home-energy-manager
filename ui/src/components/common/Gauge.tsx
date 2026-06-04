import type { ComponentChildren } from "preact";
import "./gauge.css";

interface GaugeProps {
  label: string;
  value: number | null | undefined;
  min: number;
  max: number;
  target?: number | null;
  unit?: string;
  tone?: "thermal" | "cool" | "neutral" | "warm" | "cold";
  sub?: string;
  icon?: ComponentChildren;
  // When true and unit is °C, show the Fahrenheit equivalent alongside.
  showFahrenheit?: boolean;
  // Override the tone gradient with an explicit fill colour (e.g. a continuous
  // temperature→colour mapping). Takes precedence over `tone`.
  fillColor?: string;
}

function clampFrac(v: number, min: number, max: number): number {
  if (max <= min) return 0;
  return Math.max(0, Math.min(1, (v - min) / (max - min)));
}

// Horizontal bar gauge: track + fill to `value`, optional tick at `target`,
// big readout above. Pure HTML/CSS — no canvas/SVG, so it renders correctly
// everywhere and is cheap on the eager Home critical path.
export function Gauge({ label, value, min, max, target, unit = "°C", tone = "thermal", sub, icon, showFahrenheit, fillColor }: GaugeProps) {
  const has = value != null && Number.isFinite(value);
  const frac = has ? clampFrac(value as number, min, max) : 0;
  const targetFrac = target != null && Number.isFinite(target) ? clampFrac(target, min, max) : null;
  const fahrenheit = has && showFahrenheit && unit === "°C"
    ? `${Math.round((value as number) * 9 / 5 + 32)}°F` : null;
  return (
    <div class={`gauge gauge--${tone}`}>
      <div class="gauge-top">
        <span class="gauge-label">{icon && <span class="gauge-icon">{icon}</span>}{label}</span>
        <span class="gauge-value">
          {has ? `${(value as number).toFixed(0)}${unit}` : "—"}
          {fahrenheit && <span class="gauge-value-alt"> · {fahrenheit}</span>}
        </span>
      </div>
      <div class="gauge-track">
        <div class="gauge-fill" style={{ width: `${(frac * 100).toFixed(1)}%`, ...(fillColor ? { background: fillColor } : {}) }} />
        {targetFrac != null && (
          <div class="gauge-target" style={{ left: `${(targetFrac * 100).toFixed(1)}%` }}
               title={`target ${target!.toFixed(0)}${unit}`} />
        )}
      </div>
      {sub && <span class="gauge-sub">{sub}</span>}
    </div>
  );
}
