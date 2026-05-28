import type { TariffDashboardResponse, TariffTotalRow, MetricsResponse } from "../../lib/types";
import { gbp, gbpSigned } from "../../lib/format";
import "./tariff-comparison.css";

interface TariffComparisonWidgetProps {
  dashboard: TariffDashboardResponse | null;
  dashboardLoading: boolean;
  metrics: MetricsResponse | null;
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
export function TariffComparisonWidget({ dashboard, dashboardLoading, metrics }: TariffComparisonWidgetProps) {
  if (dashboardLoading) {
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
  const rows = importOnly.slice().sort((a, b) => a.total_pence - b.total_pence);
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
      <div class="tcomp-header">
        <div class="tcomp-header-text">
          <span class="tcomp-header-label">Replay against your usage</span>
          <span class="tcomp-header-value">
            {days} days · {usage ? `${usage.total_import_kwh.toFixed(0)} kWh imported, ${usage.total_export_kwh.toFixed(0)} kWh exported` : "—"}
          </span>
        </div>
        {cheapest && (
          <div class="tcomp-cheapest">
            <span class="tcomp-cheapest-label">Cheapest</span>
            <span class="tcomp-cheapest-name">{cheapest.display_name}</span>
          </div>
        )}
      </div>

      <div class="tcomp-table">
        <div class="tcomp-row tcomp-row--head">
          <span class="tcomp-cell tcomp-cell-name">Tariff</span>
          <span class="tcomp-cell tcomp-cell-rate">Unit / day</span>
          <span class="tcomp-cell tcomp-cell-period">£ for {days}d</span>
          <span class="tcomp-cell tcomp-cell-delta">vs current</span>
        </div>
        {rows.slice(0, 10).map((r) => (
          <TariffRow key={r.product_code} row={r} isBg={r.product_code === "BG-FIX-V58"} />
        ))}
      </div>

      <div class="tcomp-foot">
        <span>
          Includes standing charge + export earnings.
          {ft?.label && !bgRow && " "}
          {ft?.label && !bgRow && (
            <em>{ft.label} comparison needs FIXED_TARIFF_* + usage data.</em>
          )}
          {outgoingCount > 0 && (
            <> · {outgoingCount} outgoing-only tariff{outgoingCount > 1 ? "s" : ""} hidden.</>
          )}
        </span>
        <span>Source: real-usage replay · Octopus catalogue{ft?.label ? ` + ${ft.label}` : ""}</span>
      </div>
    </div>
  );
}

function TariffRow({ row, isBg }: { row: TariffTotalRow; isBg: boolean }) {
  const cls = `tcomp-row${row.is_current ? " tcomp-row--current" : ""}${isBg ? " tcomp-row--bg" : ""}`;
  const delta = row.savings_vs_current_pounds;
  const deltaTone = row.is_current ? "neutral" : delta > 0 ? "ok" : delta < 0 ? "bad" : "neutral";
  const periodPounds = row.total_pence / 100;

  return (
    <div class={cls} title={row.is_current ? "Your current tariff" : isBg ? "Computed from your real usage × configured fixed rate" : row.product_code}>
      <span class="tcomp-cell tcomp-cell-name">
        {row.is_current && <span class="tcomp-current-pill">NOW</span>}
        {isBg && <span class="tcomp-bg-pill">FIXED</span>}
        <span class="tcomp-name-text">{row.display_name}</span>
      </span>
      <span class="tcomp-cell tcomp-cell-rate">
        {row.unit_rate_pence > 0 ? `${row.unit_rate_pence.toFixed(1)}p` : "—"}
        <span class="tcomp-cell-standing"> · {row.standing_per_day.toFixed(0)}p/d</span>
      </span>
      <span class="tcomp-cell tcomp-cell-period">
        <strong>{gbp(periodPounds)}</strong>
        <span class="tcomp-cell-annual">{gbp(row.annual_pounds)}/yr</span>
      </span>
      <span class={`tcomp-cell tcomp-cell-delta tcomp-cell-delta--${deltaTone}`}>
        {row.is_current ? "—" : gbpSigned(delta)}
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
