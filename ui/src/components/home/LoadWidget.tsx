import { chartTheme, withAlpha } from "../../lib/charts";
import { useFetch } from "../../lib/poll";
import { getExecutionToday, getPvToday } from "../../lib/endpoints";
import { MetricTimeline, localHM, nowIndexOf, periodPointLabel, type TimelineLine } from "./MetricTimeline";
import { Spinner } from "../common/Spinner";
import type { PeriodInsightsResponse, ExecutionTodayResponse, PvTodayResponse } from "../../lib/types";
import { isCurrentPeriod, type PeriodState } from "../../lib/period";
import "./timeline-widget.css";

interface Props {
  period: PeriodState;
  periodData: PeriodInsightsResponse | null;
  periodLoading: boolean;
  cheapP?: number | null;
  peakP?: number | null;
}

// Household load timeline, synced to the period navigator. Day → measured total
// consumption (bold) vs the LP's base-load forecast (dashed), on the /pv/today
// time axis with /execution/today overlaying measured kWh. Week/month/year →
// daily load actuals as bars.
export function LoadWidget({ period, periodData, periodLoading, cheapP, peakP }: Props) {
  const isDay = period.gran === "day";
  // Current day → endpoint UTC-today default; historical → anchor (see SolarWidget).
  const dayArg = isCurrentPeriod(period) ? undefined : period.anchor;
  const day = useFetch(
    () => (isDay
      ? Promise.all([getExecutionToday(dayArg), getPvToday(dayArg)])
          .then(([e, p]) => ({ e, p }) as { e: ExecutionTodayResponse; p: PvTodayResponse })
      : Promise.resolve(null)),
    [isDay, dayArg],
  );
  const t = chartTheme();

  if (isDay) {
    const pv = day.data?.p;
    const exec = day.data?.e;
    const slots = pv?.slots ?? [];
    if (day.loading && !slots.length) return <Spinner label="Loading load…" />;
    if (!slots.length) return <p class="muted">No load data for this day yet.</p>;
    const labels = slots.map((s) => localHM(s.slot_utc));
    const nowIdx = nowIndexOf(slots, pv?.now_utc);
    const prices = slots.map((s) => s.import_price_p ?? null);
    // Measured total consumption, aligned to the pv time axis by slot_utc.
    const consBy = new Map<string, number>();
    for (const e of exec?.slots ?? []) {
      if (e.slot_utc && e.consumption_kwh != null) consBy.set(e.slot_utc, e.consumption_kwh);
    }
    const actual = slots.map((s) => {
      const v = consBy.get(s.slot_utc);
      return v == null ? null : round2(v);
    });
    const fcast = slots.map((s) => (s.base_load_kwh == null ? null : round2(s.base_load_kwh)));
    const lines: TimelineLine[] = [
      { name: "Base-load forecast", color: withAlpha(t.house, 0.55), data: fcast, dashed: true },
      { name: "Total load", color: t.house, data: actual, area: true, width: 2.5 },
    ];
    const consumed = (exec?.slots ?? []).reduce((s, e) => s + (e.consumption_kwh ?? 0), 0);
    return (
      <div class="tlw">
        <div class="tlw-summary">
          <span class="tlw-summary-value">{consumed.toFixed(1)}<span class="tlw-summary-unit"> kWh</span></span>
          <span class="tlw-summary-label">consumed so far today</span>
        </div>
        <MetricTimeline labels={labels} lines={lines} prices={prices} nowIdx={nowIdx}
                        cheapAt={cheapP} peakAt={peakP} height={260} unit="kWh" />
        <div class="tlw-legend">
          <span><i style={`border-color:${t.house}`} /> total load</span>
          <span><i class="dashed" style={`border-color:${withAlpha(t.house, 0.6)}`} /> base-load forecast</span>
          <span>◉ now · paid/cheap/peak shaded</span>
        </div>
      </div>
    );
  }

  // Period mode: daily total-load actuals (bars).
  const pts = periodData?.chart_data ?? [];
  if (periodLoading && !pts.length) return <Spinner label="Loading load…" />;
  if (!pts.length) return <p class="muted">No load data for this period.</p>;
  const labels = pts.map((p) => periodPointLabel(p.date, period.gran));
  const lines: TimelineLine[] = [{ name: "Load", color: t.house, data: pts.map((p) => round2(p.load_kwh)), area: true }];
  const total = pts.reduce((s, p) => s + (p.load_kwh ?? 0), 0);
  return (
    <div class="tlw">
      <div class="tlw-summary">
        <span class="tlw-summary-value">{total.toFixed(0)}<span class="tlw-summary-unit"> kWh</span></span>
        <span class="tlw-summary-label">consumed · {periodData?.period_label}</span>
      </div>
      <MetricTimeline labels={labels} lines={lines} barMode height={240} />
    </div>
  );
}

function round2(n: number): number {
  return Math.round(n * 100) / 100;
}
