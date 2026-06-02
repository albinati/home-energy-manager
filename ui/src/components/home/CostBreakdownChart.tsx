import type { PeriodInsightsResponse } from "../../lib/types";
import { gbp } from "../../lib/format";
import "./cost-breakdown.css";

interface CostBreakdownChartProps {
  // The period selected in the navigator. Cost figures are now correct for any
  // window (the backend prorates standing to the elapsed-day count), so we
  // render this one period's money composition directly.
  period: PeriodInsightsResponse | null;
  label: string;
  loading: boolean;
}

interface PeriodCols {
  label: string;
  import_p: number;
  standing_p: number;
  export_p: number;       // earnings (rendered as negative below the zero line)
  total_net_p: number;    // signed total over the period
  per_day_net_p: number;  // signed daily average — what the bar represents
  per_day_import_p: number;
  per_day_standing_p: number;
  per_day_export_p: number;
  days: number;
  hasData: boolean;
}

// A single bar showing the actual money composition of the SELECTED period.
// Bar height is sized by £/DAY so day/week/month/year stay visually comparable.
//   Above zero: standing £/day (grey) stacked on import £/day (red)
//   Below zero: export £/day (green) — visually "subtracts" from the bill
// Period total appears as a small chip above the bar; net £/day under it.
export function CostBreakdownChart({ period, label, loading }: CostBreakdownChartProps) {
  const col = periodCols(label, period);
  const cols: PeriodCols[] = [col];

  if (loading && !col.hasData) {
    return <div class="cbd"><div class="cbd-skel skel" /></div>;
  }

  const maxPos = Math.max(col.per_day_import_p + col.per_day_standing_p, 0.1);
  const maxNeg = Math.max(col.per_day_export_p, 0.1);

  const W = 320, H = 178;
  const padX = 18, padTop = 22, padBottom = 50;
  const innerW = W - 2 * padX;
  const colW = innerW / cols.length;
  const barW = Math.min(72, colW * 0.5);
  const innerH = H - padTop - padBottom;
  const posShare = maxNeg > maxPos * 0.6 ? 0.6 : 0.78;
  const zeroY = padTop + innerH * posShare;
  const posH = innerH * posShare;
  const negH = innerH * (1 - posShare);

  return (
    <div class="cbd">
      <svg viewBox={`0 0 ${W} ${H}`} class="cbd-svg" aria-label="Cost breakdown chart">
        {/* faint zero baseline */}
        <line x1={padX} x2={W - padX} y1={zeroY} y2={zeroY}
              stroke="color-mix(in srgb, var(--border) 60%, transparent)"
              stroke-width="1" />

        {cols.map((c, i) => {
          const cx = padX + colW * i + colW / 2;
          const x = cx - barW / 2;
          const importH = (c.per_day_import_p / maxPos) * posH;
          const standH = (c.per_day_standing_p / maxPos) * posH;
          const exportH = (c.per_day_export_p / maxNeg) * negH;

          const segImport = c.hasData && importH > 0 ? (
            <rect x={x} y={zeroY - importH - standH}
                  width={barW} height={Math.max(1, importH)}
                  rx="4" ry="4"
                  fill="var(--import)" opacity="0.92">
              <title>{`Import: ${gbp(c.per_day_import_p)}/day · ${gbp(c.import_p)} total`}</title>
            </rect>
          ) : null;
          const segStanding = c.hasData && standH > 0 ? (
            <rect x={x} y={zeroY - standH}
                  width={barW} height={Math.max(1, standH)}
                  rx="3" ry="3"
                  fill="var(--text-mute)" opacity="0.55">
              <title>{`Standing: ${gbp(c.per_day_standing_p)}/day · ${gbp(c.standing_p)} total`}</title>
            </rect>
          ) : null;
          const segExport = c.hasData && exportH > 0 ? (
            <rect x={x} y={zeroY}
                  width={barW} height={Math.max(1, exportH)}
                  rx="4" ry="4"
                  fill="var(--export)" opacity="0.92">
              <title>{`Export earnings: ${gbp(c.per_day_export_p)}/day · ${gbp(c.export_p)} total`}</title>
            </rect>
          ) : null;

          return (
            <g key={c.label}>
              {/* total £ chip above bar — small, contextual */}
              {c.hasData && (
                <text x={cx} y={padTop - 6}
                      text-anchor="middle"
                      fill="var(--text-mute)"
                      font-size="9.5"
                      font-weight="600"
                      letter-spacing="0.04em">
                  {`${gbp(c.total_net_p)}${c.days > 1 ? ` · ${c.days}d` : ""}`}
                </text>
              )}
              {segImport}
              {segStanding}
              {segExport}
              {!c.hasData && (
                <rect x={x} y={zeroY - 1} width={barW} height={2}
                      fill="var(--border)" />
              )}
              {/* period label below bar */}
              <text x={cx} y={H - 28}
                    text-anchor="middle"
                    fill="var(--text-dim)"
                    font-size="10.5"
                    font-weight="700"
                    letter-spacing="0.04em">
                {c.label}
              </text>
              {/* big per-day net under label — what the bar HEIGHT means */}
              <text x={cx} y={H - 12}
                    text-anchor="middle"
                    fill={c.per_day_net_p >= 0 ? "var(--text)" : "var(--ok)"}
                    font-size="15"
                    font-weight="700"
                    font-variant-numeric="tabular-nums"
                    letter-spacing="-0.01em">
                {c.hasData ? `${gbp(c.per_day_net_p)}/d` : "—"}
              </text>
            </g>
          );
        })}
      </svg>

      <div class="cbd-legend">
        <span class="cbd-legend-item"><span class="cbd-swatch cbd-swatch-import" /> Imports</span>
        <span class="cbd-legend-item"><span class="cbd-swatch cbd-swatch-standing" /> Standing</span>
        <span class="cbd-legend-item"><span class="cbd-swatch cbd-swatch-export" /> Exports</span>
      </div>
    </div>
  );
}

function periodCols(label: string, p: PeriodInsightsResponse | null): PeriodCols {
  const blank: PeriodCols = {
    label, import_p: 0, standing_p: 0, export_p: 0,
    total_net_p: 0, per_day_net_p: 0,
    per_day_import_p: 0, per_day_standing_p: 0, per_day_export_p: 0,
    days: 0, hasData: false,
  };
  if (!p?.cost) return blank;
  const days = p.chart_data?.length ?? 0;
  if (days === 0) return blank;
  const imp = p.cost.import_cost_pounds ?? 0;
  const standing = (p.cost.standing_charge_pence ?? 0) / 100;
  const exp = p.cost.export_earnings_pounds ?? 0;
  const net = p.cost.net_cost_pounds ?? 0;
  return {
    label,
    import_p: imp,
    standing_p: standing,
    export_p: exp,
    total_net_p: net,
    per_day_net_p: net / days,
    per_day_import_p: imp / days,
    per_day_standing_p: standing / days,
    per_day_export_p: exp / days,
    days,
    hasData: true,
  };
}
