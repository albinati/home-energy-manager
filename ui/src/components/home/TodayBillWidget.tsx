import type { EnergyReport, MetricsResponse } from "../../lib/types";
import { gbp, gbpSigned } from "../../lib/format";
import { Spinner } from "../common/Spinner";
import "./today-bill.css";

interface TodayBillWidgetProps {
  report: EnergyReport | null;
  reportLoading: boolean;
  metrics: MetricsResponse | null;
}

// "What's today going to cost?" — realised net cost so far + projected EOD
// (simple linear extrapolation based on hours elapsed), with delta vs the
// running monthly daily-average from /metrics.
export function TodayBillWidget({ report, reportLoading, metrics }: TodayBillWidgetProps) {
  if (reportLoading && !report) {
    return <Spinner label="Crunching today's bill…" />;
  }

  const pnl = report?.pnl;
  const realised = pnl?.realised_net_cost_gbp ?? pnl?.realised_cost_gbp ?? null;
  const exportRev = pnl?.export_revenue_gbp ?? null;

  // Projection: realised / hours_elapsed × 24
  const now = new Date();
  const hoursElapsed = Math.max(0.5, now.getHours() + now.getMinutes() / 60);
  const projected = realised != null && hoursElapsed > 0
    ? (realised / hoursElapsed) * 24
    : null;

  // DMA from metrics monthly delta — rough but no extra API call
  const monthDelta = metrics?.pnl?.monthly?.delta_vs_svt_pounds ?? null;
  const dayOfMonth = now.getDate();
  const dma = monthDelta != null ? monthDelta / Math.max(1, dayOfMonth) : null;

  const todayDelta = metrics?.pnl?.daily?.delta_vs_svt_pounds ?? null;
  const dmaCompare = todayDelta != null && dma != null ? todayDelta - dma : null;

  return (
    <div class="today-bill">
      <div class="today-bill-headline">
        <div class="today-bill-realised">
          <span class="today-bill-label">Today so far</span>
          <span class="today-bill-amount today-bill-amount-realised">
            {realised != null ? gbp(realised) : "—"}
          </span>
        </div>
        <div class="today-bill-projected">
          <span class="today-bill-label">→ EOD projected</span>
          <span class="today-bill-amount today-bill-amount-projected">
            {projected != null ? gbp(projected) : "—"}
          </span>
        </div>
      </div>

      <div class="today-bill-rows">
        {exportRev != null && exportRev > 0 && (
          <div class="today-bill-row">
            <span class="today-bill-row-label">Export earned</span>
            <span class="today-bill-row-value today-bill-row-value-ok">{gbp(exportRev)}</span>
          </div>
        )}
        {dma != null && (
          <div class="today-bill-row">
            <span class="today-bill-row-label">30-day avg saving</span>
            <span class="today-bill-row-value">{gbpSigned(dma)}/day</span>
          </div>
        )}
        {dmaCompare != null && (
          <div class="today-bill-row">
            <span class="today-bill-row-label">Vs typical day</span>
            <span class={`today-bill-row-value ${dmaCompare >= 0 ? "today-bill-row-value-ok" : "today-bill-row-value-bad"}`}>
              {gbpSigned(dmaCompare)}
            </span>
          </div>
        )}
      </div>

      <div class="today-bill-note">
        Projection assumes current cost rate continues. Updated with realised values overnight.
      </div>
    </div>
  );
}
