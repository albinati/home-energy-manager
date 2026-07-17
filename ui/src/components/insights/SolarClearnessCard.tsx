import { useEffect, useRef } from "preact/hooks";
import { useFetch } from "../../lib/poll";
import { getPvClearness, type PvClearnessResponse } from "../../lib/endpoints";
import { makeChart, baseOption, chartTheme, withAlpha, type EChartsType } from "../../lib/charts";
import { Spinner } from "../common/Spinner";

/** Sky vs system: daily PV production (inverter meter) against a rolling
 *  21-day clear-sky envelope — the array's own best recent day. A dip in the
 *  bars while the dashed envelope holds = the SKY was weaker (haze counts,
 *  even with no visible cloud: 2026-07-15/16 measured −25 %). A run of
 *  near-envelope days stepping down TOGETHER = the system underperforming.
 *  Clearness % per day in the tooltip, with the day's load for context. */
export function SolarClearnessCard() {
  const res = useFetch<PvClearnessResponse>(
    () => getPvClearness(30),
    [],
    { track: true },
  );
  const data = res.data;
  const elRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<EChartsType | null>(null);

  useEffect(() => {
    if (!elRef.current) return;
    const ch = makeChart(elRef.current);
    chartRef.current = ch;
    const onResize = () => ch.resize();
    window.addEventListener("resize", onResize);
    return () => {
      window.removeEventListener("resize", onResize);
      ch.dispose();
      chartRef.current = null;
    };
  }, []);

  useEffect(() => {
    if (!chartRef.current || !data?.days?.length) return;
    const t = chartTheme();
    const base = baseOption();
    const days = data.days;
    const labels = days.map((d) => d.date.slice(5));
    const solar = days.map((d) => d.solar_kwh);
    const envelope = days.map((d) => d.envelope_kwh);
    chartRef.current.setOption({
      ...base,
      legend: { show: false },
      grid: { left: 8, right: 8, top: 20, bottom: 24, containLabel: true },
      tooltip: {
        ...(base.tooltip as object),
        formatter: (params: Array<{ dataIndex: number }>) => {
          const d = days[params[0]?.dataIndex ?? 0];
          if (!d) return "";
          const rows = [`<strong>${d.date}</strong>${d.partial ? " · partial (awaiting rollup)" : ""}`];
          if (d.solar_kwh != null) rows.push(`Solar <strong>${d.solar_kwh.toFixed(1)} kWh</strong>`);
          if (d.clearness != null) {
            rows.push(`Sky <strong>${Math.round(d.clearness * 100)}%</strong> of the recent clear-day ceiling`);
          }
          if (d.envelope_kwh != null) rows.push(`<span style="color:${t.textMute}">ceiling ${d.envelope_kwh.toFixed(1)} kWh (best of trailing ${data.window_days}d)</span>`);
          if (d.load_kwh != null) rows.push(`<span style="color:${t.textMute}">house load ${d.load_kwh.toFixed(1)} kWh</span>`);
          return rows.join("<br/>");
        },
      },
      xAxis: {
        type: "category", data: labels,
        axisLabel: { color: t.textMute, fontSize: 10, hideOverlap: true },
        axisTick: { show: false }, axisLine: { show: false },
      },
      yAxis: {
        ...(base.yAxis as object), name: "kWh/day",
        nameTextStyle: { color: t.textMute, fontSize: 10 },
        axisLabel: { color: t.textMute, fontSize: 10 },
      },
      series: [
        {
          name: "Solar", type: "bar", data: solar.map((v, i) => ({
            value: v,
            itemStyle: {
              color: days[i].partial
                ? withAlpha(t.thermal, 0.35)
                : (days[i].clearness != null && (days[i].clearness as number) < 0.8)
                  ? withAlpha(t.thermal, 0.55)
                  : t.thermal,
            },
          })),
          barMaxWidth: 14, z: 3,
        },
        {
          name: "Clear-sky ceiling", type: "line", data: envelope, step: "middle",
          showSymbol: false, silent: true,
          lineStyle: { color: t.textMute, width: 1.5, type: "dashed" }, z: 4,
        },
      ],
    }, { notMerge: true });
  }, [data]);

  return (
    <section class={`insights-card solar-clearness${res.loading && data ? " is-updating" : ""}`}>
      <header class="solar-clearness-head">
        <h2>Solar sky check</h2>
      </header>
      <p class="muted">
        Daily PV vs the array's own clear-day ceiling (best of the trailing{" "}
        {data?.window_days ?? 21} days). Dimmed bars = the sky delivered under
        80% — haze counts even when it doesn't look cloudy. Bars stepping down
        WITH the dashed ceiling would mean the system, not the sky.
      </p>
      {!data && res.loading && <Spinner label="Loading solar history…" />}
      {res.error && <p class="insights-error">Couldn't load solar history: {res.error.message}</p>}
      <div ref={elRef} style={{ width: "100%", height: "220px" }} />
    </section>
  );
}
