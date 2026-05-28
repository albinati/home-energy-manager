import type { TariffDashboardResponse, TariffTotalRow, MetricsResponse } from "../../lib/types";
import { gbp, gbpSigned } from "../../lib/format";
import "./tariff-comparison.css";

interface TariffComparisonWidgetProps {
  dashboard: TariffDashboardResponse | null;
  dashboardLoading: boolean;
  metrics: MetricsResponse | null;
}

// Compares the *catalogue* of tariffs against the user's actual usage.
// Source: POST /tariffs/dashboard — Octopus tariff engine that replays the
// same import/export kWh half-hour profile against every tariff's rates,
// always including standing charge and export earnings.
//
// Bonus row: British Gas Fixed v58 (configured via .env FIXED_TARIFF_*).
// Its annual cost is back-derived from metrics.pnl.daily.delta_vs_fixed_pounds
// (Agile beat BG Fixed by £X today) projected onto the dashboard's window —
// not exact, but a fair "for comparable usage" reference.
//
// Export rate fallback handled server-side: when Octopus export = 0/missing
// the LP uses 4p/kWh as a floor (mirrors the user's request).
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

  // Backend bug: dashboard.totals mixes export tariffs (Agile Outgoing,
  // Outgoing-SEG-*, Power-Pack) with import tariffs. Export tariffs have
  // standing=0 and rate=export-price; treating them as "cheapest import"
  // misreads £521 revenue as £521 cost. Filter to import tariffs only —
  // standing_per_day > 0 + product code that isn't an outgoing/power-pack.
  const importOnly = dashboard.totals.filter((r) => isImportTariff(r));
  const rows = importOnly.slice().sort((a, b) => a.annual_pounds - b.annual_pounds);
  const cheapest = rows[0];
  const currentRow = rows.find((r) => r.is_current) ?? null;
  const outgoingCount = dashboard.totals.length - importOnly.length;
  const usage = dashboard.usage;
  const days = usage?.total_days ?? 0;

  // Synthesize a BG Fixed v58 row if metrics has delta_vs_fixed for today.
  // BG_daily = realised_today - delta_vs_fixed_today (per-day average over
  // the dashboard window via current's daily_avg, then × days).
  let bgRow: TariffTotalRow | null = null;
  const deltaFixedDaily = metrics?.pnl?.daily?.delta_vs_fixed_pounds;
  if (currentRow && deltaFixedDaily != null && days > 0) {
    // BG annual ≈ current annual + (delta_vs_fixed × 365) — positive delta
    // means Agile saved money; BG cost was higher by that × 365.
    const bgAnnualPounds = (currentRow.annual_pounds ?? 0) + deltaFixedDaily * 365;
    const bgTotalP = bgAnnualPounds * 100 * (days / 365);
    const savingsVsCurrent = (currentRow.total_pence - bgTotalP) / 100;
    bgRow = {
      product_code: "BG-FIX-V58",
      display_name: "British Gas Fixed v58",
      pricing: "flat",
      total_pence: bgTotalP,
      daily_avg_pence: bgTotalP / days,
      annual_pounds: bgAnnualPounds,
      standing_per_day: 0,
      unit_rate_pence: 0,
      savings_vs_current_pounds: savingsVsCurrent,
      is_current: false,
    } as TariffTotalRow;
    // Insert in sort order
    const idx = rows.findIndex((r) => r.annual_pounds > bgAnnualPounds);
    if (idx === -1) rows.push(bgRow);
    else rows.splice(idx, 0, bgRow);
  }

  return (
    <div class="tcomp">
      <div class="tcomp-header">
        <div class="tcomp-header-text">
          <span class="tcomp-header-label">Comparison window</span>
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
          <span class="tcomp-cell tcomp-cell-annual">Annual</span>
          <span class="tcomp-cell tcomp-cell-delta">vs current</span>
        </div>
        {rows.slice(0, 8).map((r) => (
          <TariffRow key={r.product_code} row={r} isBg={r.product_code === "BG-FIX-V58"} />
        ))}
      </div>

      <div class="tcomp-foot">
        <span>Includes standing charge + export earnings{outgoingCount > 0 ? ` · ${outgoingCount} outgoing-only tariff${outgoingCount > 1 ? "s" : ""} hidden` : ""}.</span>
        <span>Source: Octopus catalogue · usage replay</span>
      </div>
    </div>
  );
}

// True for "real" import tariffs. Export-only catalogue entries have
// standing_per_day=0 and product codes containing OUTGOING / POWER-PACK /
// FLUX-EXPORT — they belong in their own surface, not the cheapest-import list.
function isImportTariff(r: TariffTotalRow): boolean {
  const code = (r.product_code || "").toUpperCase();
  if (code.includes("OUTGOING") || code.includes("POWER-PACK")) return false;
  if (code.startsWith("AGILE-OUTGOING") || code.includes("-EXPORT")) return false;
  // A real import tariff has a standing charge. Zero standing + zero unit
  // rate is also a sign of an export-only entry.
  if ((r.standing_per_day ?? 0) <= 0 && (r.unit_rate_pence ?? 0) <= 0) return false;
  return true;
}

function TariffRow({ row, isBg }: { row: TariffTotalRow; isBg: boolean }) {
  const cls = `tcomp-row${row.is_current ? " tcomp-row--current" : ""}${isBg ? " tcomp-row--bg" : ""}`;
  const delta = row.savings_vs_current_pounds;
  const deltaTone = row.is_current ? "neutral" : delta > 0 ? "ok" : delta < 0 ? "bad" : "neutral";

  return (
    <div class={cls} title={row.is_current ? "Your current tariff" : isBg ? "Estimated from metrics.delta_vs_fixed" : row.product_code}>
      <span class="tcomp-cell tcomp-cell-name">
        {row.is_current && <span class="tcomp-current-pill">NOW</span>}
        {isBg && <span class="tcomp-bg-pill">EST</span>}
        <span class="tcomp-name-text">{row.display_name}</span>
      </span>
      <span class="tcomp-cell tcomp-cell-rate">
        {row.unit_rate_pence > 0 ? `${row.unit_rate_pence.toFixed(1)}p` : "—"}
        <span class="tcomp-cell-standing"> · {row.standing_per_day.toFixed(0)}p/d</span>
      </span>
      <span class="tcomp-cell tcomp-cell-annual">{gbp(row.annual_pounds)}/yr</span>
      <span class={`tcomp-cell tcomp-cell-delta tcomp-cell-delta--${deltaTone}`}>
        {row.is_current ? "—" : gbpSigned(delta)}
      </span>
    </div>
  );
}
