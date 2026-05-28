import type { EnergyReport, MetricsResponse, ExecutionTodayResponse } from "../../lib/types";
import { gbp, gbpSigned, kwh } from "../../lib/format";
import "./today-bill.css";

interface TodayBillWidgetProps {
  report: EnergyReport | null;
  reportLoading: boolean;
  metrics: MetricsResponse | null;
  execution: ExecutionTodayResponse | null;
}

// Today's actual bill from /energy/report?period=day:
//   cost.net_cost_pounds       = realised net (includes standing charge)
//   cost.import_cost_pounds    = energy we paid for
//   cost.export_earnings_pounds = energy we got paid for
//   energy.import_kwh / export_kwh = volumes
// Plus an hourly cost spark from /execution/today (per-slot realised data).
export function TodayBillWidget({ report, reportLoading, metrics, execution }: TodayBillWidgetProps) {
  const cost = report?.cost;
  const energy = report?.energy;

  const realised = cost?.net_cost_pounds ?? null;
  const importCost = cost?.import_cost_pounds ?? null;
  const exportEarn = cost?.export_earnings_pounds ?? null;
  const standingPence = cost?.standing_charge_pence ?? null;
  const importKwh = energy?.import_kwh ?? null;
  const exportKwh = energy?.export_kwh ?? null;

  // Linear projection from hours elapsed (rough — assumes the rest of the
  // day costs at the average rate-so-far).
  const now = new Date();
  const hoursElapsed = Math.max(0.5, now.getHours() + now.getMinutes() / 60);
  const projected = realised != null && hoursElapsed > 0
    ? (realised / hoursElapsed) * 24
    : null;

  // 30-day DMA from /metrics
  const monthDelta = metrics?.pnl?.monthly?.delta_vs_svt_pounds ?? null;
  const dayOfMonth = now.getDate();
  const dma = monthDelta != null ? monthDelta / Math.max(1, dayOfMonth) : null;
  const todayDelta = metrics?.pnl?.daily?.delta_vs_svt_pounds ?? null;
  const dmaCompare = todayDelta != null && dma != null ? todayDelta - dma : null;

  const isLoadingReport = reportLoading && !report;
  const hourly = buildHourlyCost(execution);

  return (
    <div class="today-bill">
      <div class="today-bill-headline">
        <div class="today-bill-realised">
          <span class="today-bill-label">Today net</span>
          <span class="today-bill-amount today-bill-amount-realised">
            {realised != null
              ? gbp(realised)
              : isLoadingReport
                ? <span class="skel-text" style={{ width: "4rem", height: "1.4rem" }} />
                : "—"}
          </span>
        </div>
        <div class="today-bill-projected">
          <span class="today-bill-label">→ projected EOD</span>
          <span class="today-bill-amount today-bill-amount-projected">
            {projected != null
              ? gbp(projected)
              : isLoadingReport
                ? <span class="skel-text" style={{ width: "4rem", height: "1.4rem" }} />
                : "—"}
          </span>
        </div>
      </div>

      {/* Real flow: imported / exported / standing — answers "where did the £ go" */}
      <div class="today-bill-flow">
        <div class="today-bill-flow-row">
          <span class="today-bill-flow-icon">↓</span>
          <span class="today-bill-flow-label">Imported</span>
          <span class="today-bill-flow-kwh">{importKwh != null ? kwh(importKwh) : "—"}</span>
          <span class="today-bill-flow-cost today-bill-flow-cost-paid">
            {importCost != null ? `−${gbp(importCost)}` : "—"}
          </span>
        </div>
        <div class="today-bill-flow-row">
          <span class="today-bill-flow-icon">↑</span>
          <span class="today-bill-flow-label">Exported</span>
          <span class="today-bill-flow-kwh">{exportKwh != null ? kwh(exportKwh) : "—"}</span>
          <span class="today-bill-flow-cost today-bill-flow-cost-earned">
            {exportEarn != null && exportEarn > 0 ? `+${gbp(exportEarn)}` : "—"}
          </span>
        </div>
        <div class="today-bill-flow-row today-bill-flow-row-fixed">
          <span class="today-bill-flow-icon">📅</span>
          <span class="today-bill-flow-label">Standing charge</span>
          <span class="today-bill-flow-kwh"></span>
          <span class="today-bill-flow-cost">{standingPence != null ? `−${gbp(standingPence / 100)}` : "—"}</span>
        </div>
      </div>

      {/* Hourly cost spark from execution_today */}
      {hourly.some((h) => h.costP > 0) && (
        <div class="today-bill-hourly">
          <div class="today-bill-hourly-label">Hourly cost (p)</div>
          <HourlySpark hourly={hourly} />
        </div>
      )}

      <div class="today-bill-rows">
        {dma != null && (
          <div class="today-bill-row">
            <span class="today-bill-row-label">30-day avg saving</span>
            <span class="today-bill-row-value">{gbpSigned(dma)}/day</span>
          </div>
        )}
        {dmaCompare != null && (
          <div class="today-bill-row">
            <span class="today-bill-row-label">vs typical day</span>
            <span class={`today-bill-row-value ${dmaCompare >= 0 ? "today-bill-row-value-ok" : "today-bill-row-value-bad"}`}>
              {gbpSigned(dmaCompare)}
            </span>
          </div>
        )}
      </div>
    </div>
  );
}

interface HourBin { hour: number; costP: number; kwh: number; }

function buildHourlyCost(exec: ExecutionTodayResponse | null): HourBin[] {
  const out: HourBin[] = [];
  for (let h = 0; h < 24; h++) out.push({ hour: h, costP: 0, kwh: 0 });
  if (!exec?.slots) return out;
  for (const s of exec.slots) {
    if (!s.slot_utc) continue;
    const h = new Date(s.slot_utc).getHours();
    out[h].costP += s.cost_realised_p ?? 0;
    out[h].kwh += s.consumption_kwh ?? 0;
  }
  return out;
}

function HourlySpark({ hourly }: { hourly: HourBin[] }) {
  const max = Math.max(0.01, ...hourly.map((h) => Math.abs(h.costP)));
  const W = 280, H = 40;
  const stepX = W / 24;
  const nowHour = new Date().getHours();

  return (
    <svg viewBox={`0 0 ${W} ${H + 12}`} class="today-bill-hourly-svg" aria-label="Hourly cost in pence">
      {hourly.map((h) => {
        const barH = (Math.abs(h.costP) / max) * H;
        const x = h.hour * stepX;
        const y = H - barH;
        const isPast = h.hour < nowHour;
        const isNow = h.hour === nowHour;
        return (
          <rect
            key={h.hour}
            x={x + 1}
            y={y}
            width={stepX - 2}
            height={barH}
            fill={isNow ? "var(--accent)" : isPast ? "var(--ok)" : "var(--text-mute)"}
            opacity={isNow ? 1 : isPast ? 0.7 : 0.25}
            rx="1"
          >
            <title>{`${String(h.hour).padStart(2, "0")}:00 — ${h.costP.toFixed(1)}p · ${h.kwh.toFixed(2)} kWh`}</title>
          </rect>
        );
      })}
      <text x={0}        y={H + 10} font-size="8" fill="var(--text-mute)">00</text>
      <text x={W / 4}    y={H + 10} font-size="8" fill="var(--text-mute)" text-anchor="middle">06</text>
      <text x={W / 2}    y={H + 10} font-size="8" fill="var(--text-mute)" text-anchor="middle">12</text>
      <text x={W * 3/4}  y={H + 10} font-size="8" fill="var(--text-mute)" text-anchor="middle">18</text>
      <text x={W}        y={H + 10} font-size="8" fill="var(--text-mute)" text-anchor="end">24</text>
    </svg>
  );
}
