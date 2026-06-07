import { chartTheme, withAlpha } from "../../lib/charts";
import { useFetch } from "../../lib/poll";
import { getPvToday } from "../../lib/endpoints";
import { MetricTimeline, localHM, nowIndexOf, periodPointLabel, type TimelineLine } from "./MetricTimeline";
import { Spinner } from "../common/Spinner";
import type { PeriodInsightsResponse } from "../../lib/types";
import { isCurrentPeriod, type PeriodState } from "../../lib/period";
import "./timeline-widget.css";

interface Props {
  period: PeriodState;
  periodData: PeriodInsightsResponse | null;
  periodLoading: boolean;
  cheapP?: number | null;
  peakP?: number | null;
}

// Solar timeline, synced to the period navigator. Day → committed-plan vs
// realised PV (intraday, the /pv/today line). Week/month/year → daily solar
// actuals as bars (historical intraday forecast isn't kept).
export function SolarWidget({ period, periodData, periodLoading, cheapP, peakP }: Props) {
  const isDay = period.gran === "day";
  // For the CURRENT day pass no date → the endpoint's UTC-today default (the
  // proven live behaviour). Only historical days pass the anchor. This avoids
  // the "local date read as a UTC day" blank window right after local midnight.
  const dayArg = isCurrentPeriod(period) ? undefined : period.anchor;
  const day = useFetch(() => (isDay ? getPvToday(dayArg) : Promise.resolve(null)), [isDay, dayArg]);
  const t = chartTheme();

  if (isDay) {
    const slots = day.data?.slots ?? [];
    if (day.loading && !slots.length) return <Spinner label="Loading solar…" />;
    if (!slots.length) return <p class="muted">No solar data for this day yet.</p>;
    const labels = slots.map((s) => localHM(s.slot_utc));
    const nowIdx = nowIndexOf(slots, day.data?.now_utc);
    const planLine = slots.map((s, i) =>
      nowIdx >= 0 && i > nowIdx ? round2(s.pv_forecast_kwh) : round2(s.pv_planned_kwh ?? s.pv_forecast_kwh));
    const actual = slots.map((s) => (s.pv_actual_kwh == null ? null : round2(s.pv_actual_kwh)));
    const prices = slots.map((s) => s.import_price_p ?? null);
    const lines: TimelineLine[] = [
      { name: "Plan", color: withAlpha(t.pv, 0.5), data: planLine, dashed: true },
      { name: "Actual", color: t.pv, data: actual, area: true, width: 3 },
    ];
    // Day total = locked actuals + forecast for the rest (steady headline).
    const nowMs = day.data?.now_utc ? new Date(day.data.now_utc).getTime() : Date.now();
    const total = slots.reduce((sum, s) => {
      const elapsed = new Date(s.slot_utc).getTime() + 30 * 60_000 <= nowMs;
      return sum + ((elapsed ? (s.pv_actual_kwh ?? s.pv_forecast_kwh) : s.pv_forecast_kwh) ?? 0);
    }, 0);
    const generated = slots.reduce((sum, s) => sum + (s.pv_actual_kwh ?? 0), 0);
    return (
      <div class="tlw">
        <div class="tlw-summary">
          <span class="tlw-summary-value">{total.toFixed(1)}<span class="tlw-summary-unit"> kWh</span></span>
          <span class="tlw-summary-label">expected today · {generated.toFixed(1)} generated so far</span>
        </div>
        <MetricTimeline labels={labels} lines={lines} prices={prices} nowIdx={nowIdx}
                        cheapAt={cheapP} peakAt={peakP} height={260} />
        <div class="tlw-legend">
          <span><i style={`border-color:${t.pv}`} /> actual</span>
          <span><i class="dashed" style={`border-color:${withAlpha(t.pv, 0.6)}`} /> plan</span>
          <span>◉ now · paid/cheap/peak shaded</span>
        </div>
      </div>
    );
  }

  // Period mode: daily solar actuals (bars).
  const pts = periodData?.chart_data ?? [];
  if (periodLoading && !pts.length) return <Spinner label="Loading solar…" />;
  if (!pts.length) return <p class="muted">No solar data for this period.</p>;
  const labels = pts.map((p) => periodPointLabel(p.date, period.gran));
  const lines: TimelineLine[] = [{ name: "Solar", color: t.pv, data: pts.map((p) => round2(p.solar_kwh)), area: true }];
  const total = pts.reduce((s, p) => s + (p.solar_kwh ?? 0), 0);
  return (
    <div class="tlw">
      <div class="tlw-summary">
        <span class="tlw-summary-value">{total.toFixed(0)}<span class="tlw-summary-unit"> kWh</span></span>
        <span class="tlw-summary-label">solar generated · {periodData?.period_label}</span>
        {!isCurrentPeriod(period) ? null : <span class="tlw-mode-note">actuals to date</span>}
      </div>
      <MetricTimeline labels={labels} lines={lines} barMode height={240} />
    </div>
  );
}

function round2(n: number): number {
  return Math.round(n * 100) / 100;
}
