import type { MonthlyEnergy } from "../../lib/types";
import { gbp, kwh, gbpSigned } from "../../lib/format";
import { Spinner } from "../common/Spinner";
import "./lifetime.css";

interface LifetimeWidgetProps {
  monthly: MonthlyEnergy[];
  monthlyLoading: boolean;
}

// Cumulative "lifetime" totals across the months we've been on Agile.
// Sums the monthly fetches into total saved + total exported + days on
// Agile. Uses our existing /energy/monthly fetch (no new API call).
export function LifetimeWidget({ monthly, monthlyLoading }: LifetimeWidgetProps) {
  if (monthlyLoading && monthly.length === 0) {
    return <Spinner label="Computing lifetime totals…" />;
  }
  if (monthly.length === 0) {
    return <div class="muted">No monthly data yet.</div>;
  }

  let totalSaved = 0;
  let totalExportKwh = 0;
  let totalCost = 0;
  for (const m of monthly) {
    totalSaved += m.savings_vs_svt_gbp ?? 0;
    totalExportKwh += m.export_kwh ?? 0;
    totalCost += m.cost_gbp ?? 0;
  }
  const months = monthly.length;
  const avgPerMonth = totalSaved / months;

  return (
    <div class="lifetime">
      <div class="lifetime-row lifetime-row-headline">
        <div class="lifetime-stat">
          <div class="lifetime-stat-value lifetime-stat-value-ok">{gbpSigned(totalSaved)}</div>
          <div class="lifetime-stat-label">Saved vs SVT — {months} mo</div>
        </div>
      </div>
      <div class="lifetime-row">
        <div class="lifetime-stat">
          <div class="lifetime-stat-value">{kwh(totalExportKwh, 0)}</div>
          <div class="lifetime-stat-label">Exported total</div>
        </div>
        <div class="lifetime-stat">
          <div class="lifetime-stat-value">{gbp(totalCost)}</div>
          <div class="lifetime-stat-label">Total bills</div>
        </div>
        <div class="lifetime-stat">
          <div class="lifetime-stat-value">{gbpSigned(avgPerMonth)}</div>
          <div class="lifetime-stat-label">Avg / month</div>
        </div>
      </div>
    </div>
  );
}
