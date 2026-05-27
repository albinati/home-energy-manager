import type { CockpitNow, AttributionDay, EnergyReport, MonthlyEnergy } from "../../lib/types";
import { kw, kwh, pence, gbp } from "../../lib/format";
import "./exports-widget.css";

interface ExportsWidgetProps {
  now: CockpitNow;
  yesterday: AttributionDay | null;
  report: EnergyReport | null;
  monthly: MonthlyEnergy[];
}

// Now backed by real revenue figures (not estimates):
//   - Current export rate from /cockpit/now (dynamic Outgoing Agile)
//   - Live exporting kW if currently exporting
//   - Yesterday total kWh + actual revenue from /energy/report.pnl.export_revenue_gbp
//   - This month total kWh + revenue from /energy/monthly sum
export function ExportsWidget({ now, yesterday, report, monthly }: ExportsWidgetProps) {
  const grid = now.state.grid_kw;
  const exportingNow = grid < -0.05;
  const liveRate = now.current_slot.price_export_p;
  const exportingKw = exportingNow ? -grid : 0;

  // Yesterday: prefer real revenue from /energy/report (if today's report
  // returns yesterday-window data or if we extend the type). Otherwise fall
  // back to kWh × current rate estimate.
  const ydayKwh = yesterday?.export_kwh ?? null;
  const ydayRevenueReal = report?.pnl?.export_revenue_gbp ?? null;
  const ydayEarn = ydayRevenueReal != null
    ? ydayRevenueReal
    : ydayKwh != null && liveRate != null
      ? (ydayKwh * liveRate) / 100
      : null;

  // This month: real export_kwh + export_earnings_pounds from the latest
  // monthly aggregate.
  const latestMonth = monthly.length > 0 ? monthly[monthly.length - 1] : null;
  const monthExportKwh = latestMonth?.energy?.export_kwh ?? 0;
  const monthExportEarn = latestMonth?.cost?.export_earnings_pounds ?? 0;

  return (
    <div class="exports-widget">
      <div class="exports-rate">
        <div class="exports-rate-value" style={{ color: rateColor(liveRate) }}>
          {pence(liveRate)}
        </div>
        <div class="exports-rate-label">/kWh right now <span class="exports-rate-tariff">Outgoing Agile</span></div>
      </div>

      <div class={`exports-live${exportingNow ? " is-exporting" : ""}`}>
        <span class="exports-live-dot" />
        {exportingNow ? (
          <>Exporting <strong>{kw(exportingKw)}</strong> to grid</>
        ) : (
          <>Not exporting right now</>
        )}
      </div>

      <div class="exports-rows">
        <div class="exports-row">
          <span class="exports-row-label">Yesterday</span>
          <span class="exports-row-value">
            {ydayKwh != null ? kwh(ydayKwh) : "—"}
            {ydayEarn != null && (
              <span class="exports-row-earn">
                {ydayRevenueReal != null ? " = " : " ≈ "}
                {gbp(ydayEarn)}
              </span>
            )}
          </span>
        </div>
        <div class="exports-row">
          <span class="exports-row-label">This month</span>
          <span class="exports-row-value">
            {monthExportKwh > 0 ? kwh(monthExportKwh) : "—"}
            {monthExportEarn > 0 && (
              <span class="exports-row-earn"> = {gbp(monthExportEarn)}</span>
            )}
          </span>
        </div>
      </div>

      {ydayRevenueReal == null && ydayEarn != null && (
        <div class="exports-note">
          ≈ estimate · yesterday kWh × current rate (per-slot export rates not yet logged — see #420).
        </div>
      )}
    </div>
  );
}

function rateColor(p: number | null | undefined): string {
  if (p == null) return "var(--text-mute)";
  if (p >= 15) return "var(--ok)";
  if (p >= 5) return "var(--text)";
  return "var(--text-dim)";
}
