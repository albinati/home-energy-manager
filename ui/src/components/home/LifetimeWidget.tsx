import type { MonthlyEnergy } from "../../lib/types";
import { gbp, kwh } from "../../lib/format";
import { Spinner } from "../common/Spinner";
import "./lifetime.css";

interface LifetimeWidgetProps {
  monthly: MonthlyEnergy[];
  monthlyLoading: boolean;
}

// Cumulative totals across the months we have data for. Pulls from
// /energy/monthly (no savings_vs_svt in that endpoint — savings comparison
// lives in /metrics for the current period and in /energy/report per day).
// We show concrete realised numbers: total net cost paid, total exported
// kWh, total export earnings, avg monthly spend.
export function LifetimeWidget({ monthly, monthlyLoading }: LifetimeWidgetProps) {
  if (monthlyLoading && monthly.length === 0) {
    return <Spinner label="Computing lifetime totals…" />;
  }

  // Only count months that have any real activity (export or cost > 0).
  const used = monthly.filter((m) => (m.cost?.net_cost_pounds ?? 0) !== 0 || (m.energy?.export_kwh ?? 0) > 0);

  if (used.length === 0) {
    return <div class="muted">No monthly data yet.</div>;
  }

  let totalCost = 0;
  let totalExportKwh = 0;
  let totalExportEarn = 0;
  let totalSolarKwh = 0;
  for (const m of used) {
    totalCost += m.cost?.net_cost_pounds ?? 0;
    totalExportKwh += m.energy?.export_kwh ?? 0;
    totalExportEarn += m.cost?.export_earnings_pounds ?? 0;
    totalSolarKwh += m.energy?.solar_kwh ?? 0;
  }
  const months = used.length;

  return (
    <div class="lifetime">
      <div class="lifetime-row lifetime-row-headline">
        <div class="lifetime-stat">
          <div class="lifetime-stat-value lifetime-stat-value-ok">{gbp(totalExportEarn)}</div>
          <div class="lifetime-stat-label">Export earnings — {months} mo tracked</div>
        </div>
      </div>
      <div class="lifetime-row">
        <div class="lifetime-stat">
          <div class="lifetime-stat-value">{kwh(totalSolarKwh, 0)}</div>
          <div class="lifetime-stat-label">Solar produced</div>
        </div>
        <div class="lifetime-stat">
          <div class="lifetime-stat-value">{kwh(totalExportKwh, 0)}</div>
          <div class="lifetime-stat-label">Exported</div>
        </div>
        <div class="lifetime-stat">
          <div class="lifetime-stat-value">{gbp(totalCost)}</div>
          <div class="lifetime-stat-label">Total bills</div>
        </div>
      </div>
    </div>
  );
}
