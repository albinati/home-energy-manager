import type { AttributionDay } from "../../lib/types";
import { kwh, pct } from "../../lib/format";

interface AttributionDonutProps {
  data: AttributionDay | null;
}

// Visualises how yesterday's solar energy was used: self-consumed, charged
// into the battery, or exported. Inline SVG donut keeps the bundle small.
export function AttributionDonut({ data }: AttributionDonutProps) {
  if (!data) return null;
  const shares = data.shares;

  const selfUse = shares?.self_use_pct ?? 0;
  const battery = shares?.battery_pct ?? 0;
  const exported = shares?.export_pct ?? 0;
  const total = selfUse + battery + exported || 1;

  const slices = [
    { label: "Self-use", value: selfUse, color: "var(--house)" },
    { label: "Battery", value: battery, color: "var(--batt)" },
    { label: "Export", value: exported, color: "var(--export)" },
  ];

  // Donut: radius 70, stroke 18, circumference ~ 440
  const r = 70;
  const cx = 100;
  const cy = 100;
  const C = 2 * Math.PI * r;

  let offset = 0;
  const arcs = slices.map((s) => {
    const len = (s.value / total) * C;
    const arc = (
      <circle
        key={s.label}
        cx={cx}
        cy={cy}
        r={r}
        fill="none"
        stroke={s.color}
        stroke-width="18"
        stroke-dasharray={`${len} ${C - len}`}
        stroke-dashoffset={-offset}
        transform={`rotate(-90 ${cx} ${cy})`}
      />
    );
    offset += len;
    return arc;
  });

  return (
    <div class="attribution-donut">
      <svg viewBox="0 0 200 200" aria-hidden="true">
        {arcs}
        <text x={cx} y={cy - 6} text-anchor="middle" fill="var(--text)" font-size="22" font-weight="700" font-variant-numeric="tabular-nums">
          {kwh(data.solar_kwh)}
        </text>
        <text x={cx} y={cy + 16} text-anchor="middle" fill="var(--text-dim)" font-size="11">
          solar produced
        </text>
      </svg>
      <div class="attribution-legend">
        {slices.map((s) => (
          <div class="attribution-legend-row" key={s.label}>
            <span class="attribution-swatch" style={{ background: s.color }} />
            <span>{s.label}</span>
            <span class="attribution-pct">{pct(s.value)}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
