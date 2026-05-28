import type { CockpitNow, AttributionDay, EnergyReport, MonthlyEnergy } from "../../lib/types";
import { kw, kwh, pence, gbp } from "../../lib/format";
import "./exports-widget.css";

interface ExportsWidgetProps {
  now: CockpitNow;
  yesterday: AttributionDay | null;
  report: EnergyReport | null;
  monthly: MonthlyEnergy[];
}

// Real revenue numbers (not estimates) using the corrected EnergyReport
// shape:
//   /cockpit/now.current_slot.price_export_p — current Outgoing Agile rate
//   /attribution/day.export_kwh                — yesterday's kWh exported
//   /energy/monthly.cost.export_earnings_pounds — this month's actual £
// Yesterday-specific revenue isn't in /energy/report?period=day (it covers
// today, not yesterday), so we fall back to kWh × current rate as estimate.
export function ExportsWidget({ now, yesterday, report, monthly }: ExportsWidgetProps) {
  const grid = now.state.grid_kw;
  const exportingNow = grid < -0.05;
  const liveRate = now.current_slot.price_export_p;
  const exportingKw = exportingNow ? -grid : 0;

  // Yesterday: report is for TODAY (period=day) so it doesn't carry
  // yesterday's revenue. Estimate kWh × current rate; mark with ≈.
  const ydayKwh = yesterday?.export_kwh ?? null;
  const ydayRevenueReal: number | null = null;
  void report; // kept for future per-day report wiring
  const ydayEarn = ydayKwh != null && liveRate != null
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
        <span class={`exports-live-dot${exportingNow ? " live-pulse" : ""}`} />
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
              <span class={`exports-row-earn${ydayRevenueReal == null ? " exports-row-earn--est" : ""}`}>
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
