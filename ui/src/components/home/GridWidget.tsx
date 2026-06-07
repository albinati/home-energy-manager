import { chartTheme, withAlpha } from "../../lib/charts";
import { useFetch } from "../../lib/poll";
import { getGridToday } from "../../lib/endpoints";
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

// Grid timeline, synced to the period navigator. Day → committed-plan vs
// realised import/export (the new /grid/today rollup — closes "Today's plan had
// no grid"). Week/month/year → daily import/export actuals as bars.
export function GridWidget({ period, periodData, periodLoading, cheapP, peakP }: Props) {
  const isDay = period.gran === "day";
  // Current day → endpoint UTC-today default; historical → anchor (see SolarWidget).
  const dayArg = isCurrentPeriod(period) ? undefined : period.anchor;
  const day = useFetch(() => (isDay ? getGridToday(dayArg) : Promise.resolve(null)), [isDay, dayArg]);
  const t = chartTheme();

  if (isDay) {
    const slots = day.data?.slots ?? [];
    if (day.loading && !slots.length) return <Spinner label="Loading grid…" />;
    if (!slots.length) return <p class="muted">No grid data for this day yet.</p>;
    const labels = slots.map((s) => localHM(s.slot_utc));
    const nowIdx = nowIndexOf(slots, day.data?.now_utc);
    const prices = slots.map((s) => s.import_price_p ?? null);
    // Export drawn negative so import-above / export-below reads instantly.
    const impPlan = slots.map((s) => (s.import_planned_kwh == null ? null : round2(s.import_planned_kwh)));
    const impAct = slots.map((s) => (s.import_actual_kwh == null ? null : round2(s.import_actual_kwh)));
    const expPlan = slots.map((s) => (s.export_planned_kwh == null ? null : -round2(s.export_planned_kwh)));
    const expAct = slots.map((s) => (s.export_actual_kwh == null ? null : -round2(s.export_actual_kwh)));
    const lines: TimelineLine[] = [
      { name: "Import plan", color: withAlpha(t.importColor, 0.5), data: impPlan, dashed: true },
      { name: "Import", color: t.importColor, data: impAct, area: true, width: 2.5 },
      { name: "Export plan", color: withAlpha(t.exportColor, 0.5), data: expPlan, dashed: true },
      { name: "Export", color: t.exportColor, data: expAct, area: true, width: 2.5 },
    ];
    const tot = day.data?.totals;
    return (
      <div class="tlw">
        <div class="tlw-summary">
          <span class="tlw-summary-value tlw-neg">{(tot?.import_actual_kwh ?? 0).toFixed(1)}<span class="tlw-summary-unit"> kWh in</span></span>
          <span class="tlw-summary-value tlw-pos">{(tot?.export_actual_kwh ?? 0).toFixed(1)}<span class="tlw-summary-unit"> kWh out</span></span>
          <span class="tlw-summary-label">grid so far today</span>
        </div>
        <MetricTimeline labels={labels} lines={lines} prices={prices} nowIdx={nowIdx}
                        cheapAt={cheapP} peakAt={peakP} height={260} />
        <div class="tlw-legend">
          <span><i style={`border-color:${t.importColor}`} /> import</span>
          <span><i style={`border-color:${t.exportColor}`} /> export (below 0)</span>
          <span><i class="dashed" style={`border-color:${t.textMute}`} /> plan</span>
          <span>◉ now</span>
        </div>
      </div>
    );
  }

  // Period mode: daily import + export actuals (grouped bars; export negative).
  const pts = periodData?.chart_data ?? [];
  if (periodLoading && !pts.length) return <Spinner label="Loading grid…" />;
  if (!pts.length) return <p class="muted">No grid data for this period.</p>;
  const labels = pts.map((p) => periodPointLabel(p.date, period.gran));
  const lines: TimelineLine[] = [
    { name: "Import", color: t.importColor, data: pts.map((p) => round2(p.import_kwh)) },
    { name: "Export", color: t.exportColor, data: pts.map((p) => -round2(p.export_kwh)), area: true },
  ];
  const impTot = pts.reduce((s, p) => s + (p.import_kwh ?? 0), 0);
  const expTot = pts.reduce((s, p) => s + (p.export_kwh ?? 0), 0);
  return (
    <div class="tlw">
      <div class="tlw-summary">
        <span class="tlw-summary-value tlw-neg">{impTot.toFixed(0)}<span class="tlw-summary-unit"> kWh in</span></span>
        <span class="tlw-summary-value tlw-pos">{expTot.toFixed(0)}<span class="tlw-summary-unit"> kWh out</span></span>
        <span class="tlw-summary-label">grid · {periodData?.period_label}</span>
      </div>
      <MetricTimeline labels={labels} lines={lines} barMode height={240} />
    </div>
  );
}

function round2(n: number): number {
  return Math.round(n * 100) / 100;
}
