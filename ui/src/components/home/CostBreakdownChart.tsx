import type { PeriodInsightsResponse } from "../../lib/types";
import { gbp } from "../../lib/format";
import "./cost-breakdown.css";

interface CostBreakdownChartProps {
  today: PeriodInsightsResponse | null;
  week: PeriodInsightsResponse | null;
  month: PeriodInsightsResponse | null;
  loading: boolean;
}

interface PeriodCols {
  label: string;
  import_p: number;
  standing_p: number;
  export_p: number;   // earnings (we render as negative)
  net_p: number;
  hasData: boolean;
}

// Three stacked bars side-by-side showing what your actual current tariff
// cost across Day / Week / Month. Each bar:
//   Above zero: standing charge (grey) stacked on import cost (red)
//   Below zero: export earnings as a negative segment (green)
//   Net = imports + standing − exports → printed below the bar
//
// Drives off the existing /energy/period cost block (import_cost_pounds,
// export_earnings_pounds, standing_charge_pence). No new endpoint, no
// derived math the server doesn't already do.
export function CostBreakdownChart({ today, week, month, loading }: CostBreakdownChartProps) {
  const cols: PeriodCols[] = [
    {
      label: "Today",
      ...periodToCols(today),
    },
    {
      label: "Trail. 7d",
      ...periodToCols(week),
    },
    {
      label: "Month",
      ...periodToCols(month),
    },
  ].map((c, i) => ({ ...c, label: ["Today", "Trail. 7d", "Month"][i] }));

  if (loading && cols.every((c) => !c.hasData)) {
    return <div class="cbd"><div class="cbd-skel skel" /></div>;
  }

  // Scale across positive and negative sides separately for symmetric axes
  const maxPos = Math.max(...cols.map((c) => c.import_p + c.standing_p), 0.1);
  const maxNeg = Math.max(...cols.map((c) => c.export_p), 0.1);

  const W = 320, H = 150;
  const padX = 24, padTop = 8, padBottom = 28;
  const innerW = W - 2 * padX;
  const colW = innerW / cols.length;
  const barW = Math.min(54, colW * 0.55);
  const innerH = H - padTop - padBottom;
  // Allocate ~75% of inner height to positive, ~25% to negative — net cost
  // is always positive in real life unless you produced more than you used.
  const posShare = maxNeg > maxPos * 0.5 ? 0.6 : 0.78;
  const zeroY = padTop + innerH * posShare;
  const posH = innerH * posShare;
  const negH = innerH * (1 - posShare);

  return (
    <div class="cbd">
      <svg viewBox={`0 0 ${W} ${H}`} class="cbd-svg" aria-label="Cost breakdown">
        {/* Zero line */}
        <line x1={padX} x2={W - padX} y1={zeroY} y2={zeroY}
              stroke="var(--border)" stroke-width="1" stroke-dasharray="2 3" />

        {cols.map((c, i) => {
          const cx = padX + colW * i + colW / 2;
          const x = cx - barW / 2;
          const importH = (c.import_p / maxPos) * posH;
          const standH = (c.standing_p / maxPos) * posH;
          const exportH = (c.export_p / maxNeg) * negH;

          return (
            <g key={c.label}>
              {/* Import segment */}
              {c.hasData && importH > 0 && (
                <rect x={x} y={zeroY - importH - standH}
                      width={barW} height={Math.max(1, importH)}
                      rx="3" fill="var(--import)" opacity="0.85">
                  <title>Import: {gbp(c.import_p)}</title>
                </rect>
              )}
              {/* Standing on top of import */}
              {c.hasData && standH > 0 && (
                <rect x={x} y={zeroY - standH}
                      width={barW} height={Math.max(1, standH)}
                      rx="3" fill="var(--text-mute)" opacity="0.6">
                  <title>Standing: {gbp(c.standing_p)}</title>
                </rect>
              )}
              {/* Export below zero (earnings — visually subtract) */}
              {c.hasData && exportH > 0 && (
                <rect x={x} y={zeroY}
                      width={barW} height={Math.max(1, exportH)}
                      rx="3" fill="var(--export)" opacity="0.85">
                  <title>Export earnings: {gbp(c.export_p)}</title>
                </rect>
              )}
              {/* No-data placeholder */}
              {!c.hasData && (
                <rect x={x} y={zeroY - 1} width={barW} height={2}
                      fill="var(--border)" />
              )}
              {/* Period label below */}
              <text x={cx} y={H - 14}
                    text-anchor="middle"
                    fill="var(--text-mute)"
                    font-size="11"
                    font-weight="600">
                {c.label}
              </text>
              {/* Net cost under the label */}
              <text x={cx} y={H - 2}
                    text-anchor="middle"
                    fill={c.net_p >= 0 ? "var(--text)" : "var(--ok)"}
                    font-size="11"
                    font-weight="700"
                    font-family="ui-monospace, monospace">
                {c.hasData ? gbp(c.net_p) : "—"}
              </text>
            </g>
          );
        })}
      </svg>

      <div class="cbd-legend">
        <span class="cbd-legend-item"><span class="cbd-swatch" style={{ background: "var(--import)" }} /> Imports</span>
        <span class="cbd-legend-item"><span class="cbd-swatch" style={{ background: "var(--text-mute)", opacity: 0.6 }} /> Standing</span>
        <span class="cbd-legend-item"><span class="cbd-swatch" style={{ background: "var(--export)" }} /> Exports</span>
      </div>
    </div>
  );
}

function periodToCols(p: PeriodInsightsResponse | null): Omit<PeriodCols, "label"> {
  if (!p?.cost) {
    return { import_p: 0, standing_p: 0, export_p: 0, net_p: 0, hasData: false };
  }
  return {
    import_p: p.cost.import_cost_pounds ?? 0,
    standing_p: (p.cost.standing_charge_pence ?? 0) / 100,
    export_p: p.cost.export_earnings_pounds ?? 0,
    net_p: p.cost.net_cost_pounds ?? 0,
    hasData: true,
  };
}
