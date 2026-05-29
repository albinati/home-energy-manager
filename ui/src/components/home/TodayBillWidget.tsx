import { useState } from "preact/hooks";
import type { EnergyReport, MetricsResponse, ExecutionTodayResponse } from "../../lib/types";
import { gbp, gbpSigned, kwh } from "../../lib/format";
import { Icon } from "../common/Icon";
import { useAnimatedNumber } from "../../lib/useAnimatedNumber";
import "./today-bill.css";

interface TodayBillWidgetProps {
  report: EnergyReport | null;
  reportLoading: boolean;
  metrics: MetricsResponse | null;
  execution: ExecutionTodayResponse | null;
}

// Today's actual bill. The NET cost (includes standing charge) is the card's
// one focal number; projection, breakdown, and the hourly spark collapse
// behind a "Details" disclosure so the default view is calm.
export function TodayBillWidget({ report, reportLoading, metrics, execution }: TodayBillWidgetProps) {
  const [open, setOpen] = useState(false);
  const cost = report?.cost;
  const energy = report?.energy;

  const realised = cost?.net_cost_pounds ?? null;
  const importCost = cost?.import_cost_pounds ?? null;
  const exportEarn = cost?.export_earnings_pounds ?? null;
  const standingPence = cost?.standing_charge_pence ?? null;
  const importKwh = energy?.import_kwh ?? null;
  const exportKwh = energy?.export_kwh ?? null;

  const now = new Date();
  const hoursElapsed = Math.max(0.5, now.getHours() + now.getMinutes() / 60);
  const projected = realised != null && hoursElapsed > 0
    ? (realised / hoursElapsed) * 24
    : null;

  const monthDelta = metrics?.pnl?.monthly?.delta_vs_svt_pounds ?? null;
  const dayOfMonth = now.getDate();
  const dma = monthDelta != null ? monthDelta / Math.max(1, dayOfMonth) : null;
  const todayDelta = metrics?.pnl?.daily?.delta_vs_svt_pounds ?? null;
  const dmaCompare = todayDelta != null && dma != null ? todayDelta - dma : null;

  const isLoadingReport = reportLoading && !report;
  const hourly = buildHourlyCost(execution);
  const realisedAnim = useAnimatedNumber(realised);

  return (
    <div class="today-bill">
      {/* Focal number — today's net cost */}
      <div class="today-bill-hero-block">
        <div class="today-bill-label">
          <span class="live-pulse today-bill-dot" /> Today net
        </div>
        <div class="today-bill-hero">
          {realisedAnim != null
            ? gbp(realisedAnim)
            : isLoadingReport
              ? <span class="skel-text" style={{ width: "5rem", height: "2.4rem" }} />
              : "—"}
        </div>
        {todayDelta != null && (
          <div class={`today-bill-vs ${todayDelta >= 0 ? "today-bill-vs-ok" : "today-bill-vs-bad"}`}>
            {gbpSigned(todayDelta)} vs SVT today
          </div>
        )}
      </div>

      <button class="today-bill-disclose" onClick={() => setOpen((v) => !v)} aria-expanded={open}>
        <span>Details</span>
        <span class={`today-bill-chevron ${open ? "is-open" : ""}`}><Icon name="chevron" size={12} /></span>
      </button>

      {open && (
        <div class="today-bill-detail">
          <div class="today-bill-row">
            <span class="today-bill-row-label">Projected end of day</span>
            <span class="today-bill-row-value">{projected != null ? gbp(projected) : "—"}</span>
          </div>

          <div class="today-bill-flow">
            <div class="today-bill-flow-row">
              <span class="today-bill-flow-icon"><Icon name="import" size={16} /></span>
              <span class="today-bill-flow-label">Imported</span>
              <span class="today-bill-flow-kwh">{importKwh != null ? kwh(importKwh) : "—"}</span>
              <span class="today-bill-flow-cost today-bill-flow-cost-paid">
                {importCost != null ? `−${gbp(importCost)}` : "—"}
              </span>
            </div>
            <div class="today-bill-flow-row">
              <span class="today-bill-flow-icon"><Icon name="export" size={16} /></span>
              <span class="today-bill-flow-label">Exported</span>
              <span class="today-bill-flow-kwh">{exportKwh != null ? kwh(exportKwh) : "—"}</span>
              <span class="today-bill-flow-cost today-bill-flow-cost-earned">
                {exportEarn != null && exportEarn > 0 ? `+${gbp(exportEarn)}` : "—"}
              </span>
            </div>
            <div class="today-bill-flow-row today-bill-flow-row-fixed">
              <span class="today-bill-flow-icon"><Icon name="schedule" size={16} /></span>
              <span class="today-bill-flow-label">Standing charge</span>
              <span class="today-bill-flow-kwh"></span>
              <span class="today-bill-flow-cost">{standingPence != null ? `−${gbp(standingPence / 100)}` : "—"}</span>
            </div>
          </div>

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
      )}
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
            fill={isNow ? "var(--accent)" : isPast ? "var(--text-dim)" : "var(--text-mute)"}
            opacity={isNow ? 1 : isPast ? 0.55 : 0.22}
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
