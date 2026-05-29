import type { PeriodInsightsResponse } from "../../lib/types";
import { gbp } from "../../lib/format";
import "./cost-breakdown.css";

interface CostBreakdownChartProps {
  today: PeriodInsightsResponse | null;
  month: PeriodInsightsResponse | null;
  loading: boolean;
}

interface PeriodCols {
  label: string;
  import_p: number;
  standing_p: number;
  export_p: number;       // earnings (we render as negative below the zero line)
  total_net_p: number;    // signed total over the period
  per_day_net_p: number;  // signed daily average — what the bars represent
  per_day_import_p: number;
  per_day_standing_p: number;
  per_day_export_p: number;
  days: number;
  hasData: boolean;
}

// Three stacked bars side-by-side showing actual money composition for
// Today / Trailing 7 days / Month-to-date. Bars are sized by £/DAY (not
// raw totals) so the periods are visually comparable — otherwise today
// (1 day) vs month (28+ days) would squash today into invisibility.
//
// Each bar:
//   Above zero: standing £/day (grey) stacked on import £/day (red)
//   Below zero: export £/day (green) — visually "subtracts" from the bill
// Period total appears as a small chip above the bar; net £/day under it.
//
// Data sources:
//   today  = /energy/period?period=day  (1 day)
//   month  = /energy/period?period=month (chart_data has per-day breakdown)
//   trail7 = computed client-side from month.chart_data (sum of last 7 days)
//   — this avoids /energy/period?period=week returning the calendar week
//   (Mon-Sun) which can be 4 days early in the week.
export function CostBreakdownChart({ today, month, loading }: CostBreakdownChartProps) {
  const todayCols = periodCols("Today", today);
  const monthCols = periodCols("Month", month);
  const trailCols = trailingFromMonth(month, 7);

  const cols: PeriodCols[] = [todayCols, trailCols, monthCols];

  if (loading && cols.every((c) => !c.hasData)) {
    return <div class="cbd"><div class="cbd-skel skel" /></div>;
  }

  // Bars are sized by per-day values so the scale is comparable.
  const maxPos = Math.max(...cols.map((c) => c.per_day_import_p + c.per_day_standing_p), 0.1);
  const maxNeg = Math.max(...cols.map((c) => c.per_day_export_p), 0.1);

  const W = 320, H = 178;
  const padX = 18, padTop = 22, padBottom = 50;
  const innerW = W - 2 * padX;
  const colW = innerW / cols.length;
  const barW = Math.min(54, colW * 0.6);
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

// Trailing N-day sum derived from the month's chart_data — true rolling
// window even on Mon/Tue (when /energy/period?period=week would only have
// 1-2 days). chart_data points carry per-day kWh; we re-bill at the same
// average price the period totals imply (cost_per_kwh × kwh + standing).
function trailingFromMonth(month: PeriodInsightsResponse | null, days: number): PeriodCols {
  const fallback: PeriodCols = {
    label: `Last ${days}d`, import_p: 0, standing_p: 0, export_p: 0,
    total_net_p: 0, per_day_net_p: 0,
    per_day_import_p: 0, per_day_standing_p: 0, per_day_export_p: 0,
    days: 0, hasData: false,
  };
  if (!month?.chart_data?.length || !month.cost) return fallback;
  const cd = month.chart_data;
  const totalDays = cd.length;
  const monthImportKwh = cd.reduce((s, d) => s + (d.import_kwh ?? 0), 0);
  const monthExportKwh = cd.reduce((s, d) => s + (d.export_kwh ?? 0), 0);
  if (monthImportKwh <= 0 && monthExportKwh <= 0 && (month.cost.standing_charge_pence ?? 0) === 0) {
    return fallback;
  }

  // Imports/exports are per-day kWh; convert to £ using the month's
  // effective per-kWh rates derived from cost totals. This sidesteps the
  // need to fetch per-day cost (chart_data doesn't carry it).
  const monthImportP = month.cost.import_cost_pounds ?? 0;
  const monthExportP = month.cost.export_earnings_pounds ?? 0;
  const monthStandingP = (month.cost.standing_charge_pence ?? 0) / 100;
  const importRate = monthImportKwh > 0 ? monthImportP / monthImportKwh : 0;
  const exportRate = monthExportKwh > 0 ? monthExportP / monthExportKwh : 0;
  const standingPerDay = totalDays > 0 ? monthStandingP / totalDays : 0;

  const last = cd.slice(-days);
  const lastDays = last.length;
  const lastImportKwh = last.reduce((s, d) => s + (d.import_kwh ?? 0), 0);
  const lastExportKwh = last.reduce((s, d) => s + (d.export_kwh ?? 0), 0);
  const lastImportP = lastImportKwh * importRate;
  const lastExportP = lastExportKwh * exportRate;
  const lastStandingP = lastDays * standingPerDay;
  const lastNetP = lastImportP + lastStandingP - lastExportP;

  return {
    label: `Last ${days}d`,
    import_p: lastImportP,
    standing_p: lastStandingP,
    export_p: lastExportP,
    total_net_p: lastNetP,
    per_day_net_p: lastDays > 0 ? lastNetP / lastDays : 0,
    per_day_import_p: lastDays > 0 ? lastImportP / lastDays : 0,
    per_day_standing_p: lastDays > 0 ? lastStandingP / lastDays : 0,
    per_day_export_p: lastDays > 0 ? lastExportP / lastDays : 0,
    days: lastDays,
    hasData: true,
  };
}
