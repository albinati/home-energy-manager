import { useEffect, useRef } from "preact/hooks";
import { useFetch } from "../../lib/poll";
import { getResidualProfile, type ResidualProfile } from "../../lib/endpoints";
import { makeChart, chartTheme, type EChartsType } from "../../lib/charts";
import { Spinner } from "../common/Spinner";

const DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

/** Per-(day-of-week × hour) median residual household load — "when we spend the
 *  most while at home". The same profile the LP plans against (#477). */
export function LoadPatternCard() {
  const res = useFetch<ResidualProfile>(getResidualProfile, []);
  const data = res.data;
  const elRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<EChartsType | null>(null);

  useEffect(() => {
    if (!elRef.current || !data) return;
    if (!chartRef.current) chartRef.current = makeChart(elRef.current);
    const t = chartTheme();

    // 7 dow × 24 hours; value = mean of the two half-hour medians in the hour.
    const cells: [number, number, number][] = [];
    let max = 0;
    for (let d = 0; d < 7; d++) {
      const slots = data.by_dow[String(d)] || [];
      for (let h = 0; h < 24; h++) {
        const a = slots[h * 2]?.median ?? 0;
        const b = slots[h * 2 + 1]?.median ?? 0;
        const v = (a + b) / 2;
        if (v > max) max = v;
        cells.push([h, d, Number(v.toFixed(3))]);
      }
    }

    chartRef.current.setOption({
      backgroundColor: "transparent",
      tooltip: {
        position: "top",
        formatter: (p: { value: [number, number, number] }) =>
          `${DOW[p.value[1]]} ${String(p.value[0]).padStart(2, "0")}:00 — <b>${p.value[2].toFixed(2)} kWh</b>/slot`,
      },
      grid: { left: 36, right: 12, top: 8, bottom: 30 },
      xAxis: {
        type: "category",
        data: Array.from({ length: 24 }, (_, h) => (h % 3 === 0 ? String(h) : "")),
        axisLine: { lineStyle: { color: t.border } },
        axisTick: { show: false },
        axisLabel: { color: t.textMute, fontSize: 10 },
      },
      yAxis: {
        type: "category",
        data: DOW,
        axisLine: { lineStyle: { color: t.border } },
        axisTick: { show: false },
        axisLabel: { color: t.textMute, fontSize: 10 },
      },
      visualMap: {
        min: 0, max: Math.max(0.4, max), calculable: false, show: false,
        inRange: { color: [t.bg ?? "#1b1f27", t.pv ?? "#fbbf24", t.importColor ?? "#ef4444"] },
      },
      series: [{
        type: "heatmap", data: cells,
        itemStyle: { borderColor: "transparent", borderWidth: 0 },
        emphasis: { itemStyle: { borderColor: t.text, borderWidth: 1 } },
        progressive: 0,
      }],
    });
    chartRef.current.resize();
  }, [data]);

  useEffect(() => () => { chartRef.current?.dispose(); chartRef.current = null; }, []);

  const dc = data?.day_counts ?? {};
  return (
    <section class="insights-card load-pattern">
      <header class="load-pattern-head">
        <h2>When you spend the most</h2>
        <p class="muted">
          Median household load (heat pump excluded) by day-of-week and hour — the typical
          at-home pattern the optimizer plans against.
        </p>
      </header>
      {res.loading && !data && <Spinner label="Loading load pattern…" />}
      {res.error && <p class="insights-error">Couldn't load the pattern: {res.error.message}</p>}
      {data && (
        <>
          <div ref={elRef} class="load-pattern-chart" />
          <p class="muted load-pattern-meta">
            {(dc.weekday ?? 0) + (dc.weekend ?? 0)} days learned
            ({dc.weekday ?? 0} weekday / {dc.weekend ?? 0} weekend)
            {(dc.away_excluded ?? 0) > 0 && <> · {dc.away_excluded} away day(s) excluded</>}
            {" · "}
            {data.calibrated_days}/{data.calibrated_days + data.physics_only_days} days calibrated
            to the measured heat-pump split
            {data.away_days.length > 0 && (
              <> · away: {data.away_days.slice(-5).join(", ")}{data.away_days.length > 5 ? " …" : ""}</>
            )}
          </p>
        </>
      )}
    </section>
  );
}
