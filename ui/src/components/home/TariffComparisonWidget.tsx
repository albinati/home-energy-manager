import type { TariffDashboardResponse, TariffTotalRow, MetricsResponse, PeriodInsightsResponse } from "../../lib/types";
import { gbp, gbpSigned } from "../../lib/format";
import "./tariff-comparison.css";

interface TariffComparisonWidgetProps {
  dashboard: TariffDashboardResponse | null;
  dashboardLoading: boolean;
  metrics: MetricsResponse | null;
  // Real realised cost for the same calendar window — used to replace the
  // current tariff's PROJECTED total. The dashboard engine bills total
  // consumption at the tariff's average rate, which ignores the LP's
  // battery-arbitrage on half-hourly tariffs (Agile) and over-states the
  // current cost by ~30 % on solar+battery setups. The realised number
  // (from /energy/period) bills measured grid import at the half-hourly
  // Agile rate per slot — the actual money out the door.
  monthPeriod: PeriodInsightsResponse | null;
  // Whether the /energy/period fetch that feeds `monthPeriod` is still
  // in-flight. The dashboard and monthPeriod are independent fetches: if the
  // dashboard lands first we must NOT render the current tariff's projected
  // total — it would flash a wrong (over-stated) number until the realised
  // cost arrives and overwrites it. Hold the skeleton until BOTH have settled.
  monthPeriodLoading: boolean;
}

// Default SEG floor used when a fixed-tariff doesn't expose its own outgoing
// rate. Octopus Flux/Outgoing varies; 4p/kWh is the long-standing SEG export
// minimum HEM falls back to elsewhere — matches user's mental model.
const SEG_EXPORT_FALLBACK_P = 4.0;

// Tariff comparison anchored ENTIRELY on the household's real usage. The
// engine in /tariffs/dashboard replays the same import/export half-hour
// profile against every Octopus tariff's rate schedule — so `total_pence`
// IS the £ that tariff would have cost over the comparison window. We lead
// with that real number; the annualised projection is a small chip.
//
// BG Fixed v58 row is computed client-side using the same real-usage block
// + the configured FIXED_TARIFF_* rates from /metrics. No annual-from-daily
// extrapolation; pure replay over the same window as the Octopus rows.
export function TariffComparisonWidget({ dashboard, dashboardLoading, metrics, monthPeriod, monthPeriodLoading }: TariffComparisonWidgetProps) {
  // Gate on BOTH fetches: the current tariff's realised total comes from
  // monthPeriod, so rendering before it settles flashes the projected number.
  if (dashboardLoading || monthPeriodLoading) {
    return <div class="tcomp"><div class="tcomp-skel skel" /></div>;
  }
  if (!dashboard?.ok || !dashboard.totals?.length) {
    return (
      <div class="tcomp">
        <p class="muted">No tariff data available yet — Octopus catalogue or usage history missing.</p>
      </div>
    );
  }

  // Strip outgoing/export-only catalogue entries — they have standing=0 and
  // a positive "unit rate" that's actually an export price. Treating them
  // as cheapest import would mislabel revenue as cost (see commit ec81a4b).
  const importOnly = dashboard.totals.filter((r) => isImportTariff(r));
  const realisedMonthlyP = monthPeriod?.cost?.net_cost_pence ?? null;
  const realisedDays = monthPeriod?.chart_data?.length ?? null;
  // Override the current tariff's projected totals with the real realised
  // ones. The engine's projection is consumption × avg rate (no battery-
  // arbitrage); the realised number is half-hourly grid_import × Agile_p.
  const replaced = importOnly.map((r) => {
    if (!r.is_current || realisedMonthlyP == null) return r;
    const adjusted: TariffTotalRow = {
      ...r,
      total_pence: realisedMonthlyP,
      daily_avg_pence: realisedDays && realisedDays > 0 ? realisedMonthlyP / realisedDays : r.daily_avg_pence,
      annual_pounds: realisedDays && realisedDays > 0 ? (realisedMonthlyP / realisedDays * 365) / 100 : r.annual_pounds,
    };
    return adjusted;
  });
  const rows = replaced.slice().sort((a, b) => a.total_pence - b.total_pence);
  const cheapest = rows[0];
  const currentRow = rows.find((r) => r.is_current) ?? null;
  const usage = dashboard.usage;
  const days = usage?.total_days ?? 0;
  const outgoingCount = dashboard.totals.length - importOnly.length;

  // Compute BG Fixed v58 (or whatever FIXED_TARIFF_LABEL is set to) from
  // the same real-usage block. No annual-from-daily extrapolation —
  // straight: cost = (import_kwh × rate) + (days × standing) − (export_kwh × 4p)
  const ft = metrics?.fixed_tariff;
  let bgRow: TariffTotalRow | null = null;
  if (ft?.label && ft.rate_pence && usage && days > 0) {
    const importCostP = (usage.total_import_kwh ?? 0) * ft.rate_pence;
    const standingP   = days * (ft.standing_pence_per_day ?? 0);
    const exportEarnP = (usage.total_export_kwh ?? 0) * SEG_EXPORT_FALLBACK_P;
    const netP = importCostP + standingP - exportEarnP;
    const dailyAvgP = netP / days;
    const savings = currentRow ? (currentRow.total_pence - netP) / 100 : 0;
    bgRow = {
      product_code: "BG-FIX-V58",
      display_name: ft.label,
      pricing: "flat",
      total_pence: netP,
      daily_avg_pence: dailyAvgP,
      annual_pounds: (dailyAvgP * 365) / 100,
      standing_per_day: ft.standing_pence_per_day ?? 0,
      unit_rate_pence: ft.rate_pence,
      savings_vs_current_pounds: savings,
      is_current: false,
    } as TariffTotalRow;
    // Insert into sort order by total_pence.
    const idx = rows.findIndex((r) => r.total_pence > netP);
    if (idx === -1) rows.push(bgRow);
    else rows.splice(idx, 0, bgRow);
  }

  return (
    <div class="tcomp">
      <div class="tcomp-table-head-row">
        <span class="tcomp-table-title">
          What other tariffs would have cost over the last {days}d of your usage
          {usage ? <span class="tcomp-table-usage"> · {usage.total_import_kwh.toFixed(0)} kWh in / {usage.total_export_kwh.toFixed(0)} kWh out</span> : null}
        </span>
        {cheapest && (
          <span class="tcomp-cheapest-inline">
            cheapest: <strong>{cheapest.display_name}</strong>
          </span>
        )}
      </div>

      <div class="tcomp-table">
        <div class="tcomp-row tcomp-row--head">
          <span class="tcomp-cell tcomp-cell-name">Tariff</span>
          <span class="tcomp-cell tcomp-cell-period">£ / {days}d</span>
          <span class="tcomp-cell tcomp-cell-delta">vs now</span>
        </div>
        {rows.slice(0, 8).map((r) => (
          <TariffRow key={r.product_code} row={r}
                     isBg={r.product_code === "BG-FIX-V58"}
                     currentTotalP={currentRow?.total_pence ?? null}
                     currentAnnualPounds={currentRow?.annual_pounds ?? null} />
        ))}
      </div>

      <div class="tcomp-foot">
        <span>
          Your current tariff = <strong>real realised cost</strong> (half-hourly Agile × measured grid import).
          Others = engine projection (flat avg rate × usage, no battery-arbitrage credit), so they over-state TOU tariffs.
          {outgoingCount > 0 && <> {outgoingCount} export-only hidden.</>}
        </span>
      </div>
    </div>
  );
}

function TariffRow({ row, isBg, currentTotalP, currentAnnualPounds }: {
  row: TariffTotalRow; isBg: boolean;
  currentTotalP: number | null; currentAnnualPounds: number | null;
}) {
  const cls = `tcomp-row${row.is_current ? " tcomp-row--current" : ""}${isBg ? " tcomp-row--bg" : ""}`;
  const periodPounds = row.total_pence / 100;
  // The backend's `savings_vs_current_pounds` is ANNUALISED for Octopus
  // rows (derived from annual_pounds), while the BG row I compute client-
  // side from real-usage replay returns a period-aligned figure — mixing
  // them made the table delta column read up to 12× too big for some
  // tariffs and correct for others. Both deltas are now computed here
  // from period vs period and annual vs annual, consistently.
  const periodDelta = currentTotalP != null
    ? (currentTotalP - row.total_pence) / 100
    : 0;
  const annualDelta = currentAnnualPounds != null
    ? currentAnnualPounds - row.annual_pounds
    : 0;
  const deltaTone = row.is_current ? "neutral" : periodDelta > 0 ? "ok" : periodDelta < 0 ? "bad" : "neutral";
  const tooltip = `${row.unit_rate_pence > 0 ? `${row.unit_rate_pence.toFixed(1)}p/kWh · ` : ""}${row.standing_per_day.toFixed(0)}p/day standing${row.is_current ? " · your current tariff" : isBg ? " · computed from your usage × configured fixed rate" : ""}${currentAnnualPounds != null && !row.is_current ? ` · annualised ≈ ${annualDelta >= 0 ? "+" : ""}£${annualDelta.toFixed(0)}/yr` : ""}`;

  return (
    <div class={cls} title={tooltip}>
      <span class="tcomp-cell tcomp-cell-name">
        {row.is_current && <span class="tcomp-current-pill">NOW</span>}
        {isBg && <span class="tcomp-bg-pill">FIXED</span>}
        <span class="tcomp-name-text">{row.display_name}</span>
      </span>
      <span class="tcomp-cell tcomp-cell-period">{gbp(periodPounds)}</span>
      <span class={`tcomp-cell tcomp-cell-delta tcomp-cell-delta--${deltaTone}`}>
        {row.is_current ? "—" : gbpSigned(periodDelta)}
      </span>
    </div>
  );
}

function isImportTariff(r: TariffTotalRow): boolean {
  const code = (r.product_code || "").toUpperCase();
  if (code.includes("OUTGOING") || code.includes("POWER-PACK")) return false;
  if (code.startsWith("AGILE-OUTGOING") || code.includes("-EXPORT")) return false;
  if ((r.standing_per_day ?? 0) <= 0 && (r.unit_rate_pence ?? 0) <= 0) return false;
  return true;
}
