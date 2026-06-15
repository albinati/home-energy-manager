import { useEffect, useRef } from "preact/hooks";
import { useFetch } from "../../lib/poll";
import { getLoadErrorLog, type LoadErrorLog } from "../../lib/endpoints";
import { periodDateRange, periodLabel, isCurrentPeriod, type PeriodState } from "../../lib/period";
import { makeChart, chartTheme, type EChartsType } from "../../lib/charts";
import { Spinner } from "../common/Spinner";

/** How well the household LOAD forecast the LP plans against matched reality,
 *  by local hour. Complements LoadPatternCard (when we spend) with how well we
 *  predict it. Surfaces the load_error_log measurement (Phase 1). Read-only. */
export function LoadForecastAccuracyCard({ period }: { period: PeriodState }) {
  const { start, end } = periodDateRange(period);
  const res = useFetch<LoadErrorLog>(
    () => getLoadErrorLog({ startDate: start, endDate: end }),
    [start, end],
    { cacheKey: `load-err:${start}:${end}`, immutable: !isCurrentPeriod(period) },
  );
  const data = res.data;
  const elRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<EChartsType | null>(null);

  useEffect(() => {
    if (!elRef.current || !data) return;
    if (!chartRef.current) chartRef.current = makeChart(elRef.current);
    const t = chartTheme();

    // 24 bars: bias (actual − forecast) per local hour. + = under-forecast
    // (planned too little), − = over-forecast (planned too much).
    const bars: { value: number; itemStyle: { color: string } }[] = [];
    for (let h = 0; h < 24; h++) {
      const s = data.per_hour_local[String(h)];
      const b = s ? s.bias_kwh : 0;
      bars.push({
        value: Number(b.toFixed(3)),
        itemStyle: { color: b >= 0 ? (t.importColor ?? "#ef4444") : (t.pv ?? "#38bdf8") },
      });
    }

    chartRef.current.setOption({
      backgroundColor: "transparent",
      tooltip: {
        trigger: "axis",
        formatter: (ps: { dataIndex: number; value: number }[]) => {
          const h = ps[0].dataIndex;
          const s = data.per_hour_local[String(h)];
          if (!s) return `${String(h).padStart(2, "0")}:00 — no data`;
          const dir = s.bias_kwh >= 0 ? "under-forecast" : "over-forecast";
          return `${String(h).padStart(2, "0")}:00 — <b>${s.bias_kwh >= 0 ? "+" : ""}${s.bias_kwh.toFixed(2)} kWh</b> (${dir})<br/>MAE ${s.mae_kwh.toFixed(2)} · n=${s.n}`;
        },
      },
      grid: { left: 40, right: 12, top: 10, bottom: 24 },
      xAxis: {
        type: "category",
        data: Array.from({ length: 24 }, (_, h) => (h % 3 === 0 ? String(h) : "")),
        axisLine: { lineStyle: { color: t.border } },
        axisTick: { show: false },
        axisLabel: { color: t.textMute, fontSize: 10 },
      },
      yAxis: {
        type: "value",
        axisLabel: { color: t.textMute, fontSize: 10, formatter: "{value}" },
        splitLine: { lineStyle: { color: t.border, opacity: 0.3 } },
      },
      series: [{
        type: "bar",
        data: bars,
        barWidth: "65%",
        markLine: {
          silent: true, symbol: "none",
          lineStyle: { color: t.textMute, type: "solid", opacity: 0.5 },
          data: [{ yAxis: 0 }],
          label: { show: false },
        },
      }],
    }, { notMerge: true });
    chartRef.current.resize();
  }, [data]);

  useEffect(() => () => { chartRef.current?.dispose(); chartRef.current = null; }, []);

  const o = data?.overall;
  return (
    <section class="insights-card load-accuracy">
      <header class="load-pattern-head">
        <h2>Load forecast accuracy</h2>
        <p class="muted">
          How well the household-load forecast the optimizer plans against matched
          reality, by local hour. <span class="la-key la-over">Blue</span> = over-forecast
          (planned too much); <span class="la-key la-under">red</span> = under-forecast
          (planned too little).
        </p>
      </header>
      {res.loading && !data && <Spinner label="Loading load accuracy…" />}
      {res.error && <p class="insights-error">Couldn't load accuracy: {res.error.message}</p>}
      {data && o && o.n > 0 && (
        <>
          <div class="la-stats">
            <div class="la-stat">
              <span class="la-stat-val">{o.mae_kwh.toFixed(2)}</span>
              <span class="la-stat-lbl">kWh/slot MAE</span>
            </div>
            <div class="la-stat">
              <span class="la-stat-val">{o.bias_kwh >= 0 ? "+" : ""}{o.bias_kwh.toFixed(3)}</span>
              <span class="la-stat-lbl">net bias (kWh/slot)</span>
            </div>
            <div class="la-stat">
              <span class="la-stat-val">{o.mean_actual_kwh.toFixed(2)}</span>
              <span class="la-stat-lbl">mean actual</span>
            </div>
            <div class="la-stat">
              <span class="la-stat-val">{o.n}</span>
              <span class="la-stat-lbl">slots ({data.window_days}d)</span>
            </div>
          </div>
          <div ref={elRef} class="load-pattern-chart" />
          <p class="muted load-pattern-meta">
            Net bias near zero overall can still hide a diurnal pattern (the bars) —
            measured against the committed plan, total household load (heat pump included).
          </p>
        </>
      )}
      {data && (!o || o.n === 0) && (
        <p class="muted insights-empty">No load forecast-vs-actual data for {periodLabel(period)} yet.</p>
      )}
    </section>
  );
}
