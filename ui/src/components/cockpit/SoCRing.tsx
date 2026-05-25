import { kw, kwh } from "../../lib/format";

interface SoCRingProps {
  socPct: number | null | undefined;
  socKwh: number | null | undefined;
  batteryKw: number | null | undefined;
}

// Circular gauge: outer arc 0→100% SoC, inner glyph signals charging direction.
export function SoCRing({ socPct, socKwh, batteryKw }: SoCRingProps) {
  const pct = socPct != null && Number.isFinite(socPct) ? Math.max(0, Math.min(100, socPct)) : 0;
  const radius = 56;
  const stroke = 10;
  const cx = 80;
  const cy = 80;
  const circumference = 2 * Math.PI * radius;
  const dashLen = (pct / 100) * circumference;

  let arrow = "";
  let arrowColor = "var(--text-mute)";
  let arrowLabel = "Idle";
  if (batteryKw != null && Math.abs(batteryKw) > 0.1) {
    if (batteryKw > 0) {
      arrow = "▲";
      arrowColor = "var(--ok)";
      arrowLabel = "Charging";
    } else {
      arrow = "▼";
      arrowColor = "var(--warn)";
      arrowLabel = "Discharging";
    }
  }

  // Color the arc by SoC band.
  let arcColor = "var(--ok)";
  if (pct < 20) arcColor = "var(--bad)";
  else if (pct < 50) arcColor = "var(--warn)";

  return (
    <div class="soc-ring">
      <svg viewBox="0 0 160 160" width="160" height="160" aria-hidden="true">
        <circle cx={cx} cy={cy} r={radius} stroke="var(--border)" stroke-width={stroke} fill="none" />
        <circle
          cx={cx}
          cy={cy}
          r={radius}
          stroke={arcColor}
          stroke-width={stroke}
          fill="none"
          stroke-linecap="round"
          stroke-dasharray={`${dashLen} ${circumference}`}
          transform={`rotate(-90 ${cx} ${cy})`}
          style={{ transition: "stroke-dasharray 400ms ease, stroke 200ms ease" }}
        />
      </svg>
      <div class="soc-ring-inner">
        <div class="soc-ring-pct">{Math.round(pct)}%</div>
        <div class="soc-ring-kwh">{kwh(socKwh)}</div>
        <div class="soc-ring-arrow" style={{ color: arrowColor }} aria-label={arrowLabel}>
          <span class="soc-ring-arrow-glyph">{arrow || "•"}</span>
          {batteryKw != null && Math.abs(batteryKw) > 0.05 && (
            <span class="soc-ring-arrow-kw">{kw(Math.abs(batteryKw))}</span>
          )}
        </div>
      </div>
    </div>
  );
}
