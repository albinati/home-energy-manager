import { useEffect, useRef, useState } from "preact/hooks";
import { useFetch } from "../../lib/poll";
import { getIndoorRollup } from "../../lib/endpoints";
import type { IndoorRollupResponse } from "../../lib/types";
import { makeChart, chartTheme, withAlpha, type EChartsType } from "../../lib/charts";
import { Spinner } from "../common/Spinner";

const WINDOWS = [30, 90, 365] as const;

/** Long-term indoor-temperature trend from the permanent 15-min WARM rollup
 *  (#540) — mean line with a min/max band, over a selectable window. Reads the
 *  rollup table, so it keeps working long after the raw readings are archived
 *  out of SQLite. Viewer / read-only. */
export function IndoorHistoryCard() {
  const [days, setDays] = useState<number>(30);
  const res = useFetch<IndoorRollupResponse>(
    () => getIndoorRollup(days),
    [days],
    { cacheKey: `indoor-rollup:${days}`, track: true },
  );
  const data = res.data;
  const elRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<EChartsType | null>(null);

  const buckets = data?.buckets ?? [];
  const hasData = buckets.length > 0;

  useEffect(() => {
    if (!elRef.current) return;
    if (!hasData) { chartRef.current?.clear(); return; }
    if (!chartRef.current) chartRef.current = makeChart(elRef.current);
    const t = chartTheme();

    const ts = (b: string) => new Date(b.replace(" ", "T")).getTime();
    const mean = buckets.map((b) => [ts(b.bucket_utc), Number(b.mean_c.toFixed(2))]);
    // Filled min/max band via stacked lines: lower = min, upper = (max − min).
    const lo = buckets.map((b) => [ts(b.bucket_utc), Number(b.min_c.toFixed(2))]);
    const band = buckets.map((b) => [ts(b.bucket_utc), Number((b.max_c - b.min_c).toFixed(2))]);

    chartRef.current.setOption({
      backgroundColor: "transparent",
      tooltip: {
        trigger: "axis",
        formatter: (ps: { dataIndex: number }[]) => {
          const b = buckets[ps[0].dataIndex];
          if (!b) return "";
          const d = new Date(b.bucket_utc.replace(" ", "T"));
          return `${d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}<br/>`
            + `mean <b>${b.mean_c.toFixed(1)}°C</b> · range ${b.min_c.toFixed(1)}–${b.max_c.toFixed(1)}°C · n=${b.n}`;
        },
      },
      grid: { left: 40, right: 14, top: 12, bottom: 24 },
      xAxis: {
        type: "time",
        axisLine: { lineStyle: { color: t.border } },
        axisTick: { show: false },
        axisLabel: { color: t.textMute, fontSize: 10 },
      },
      yAxis: {
        type: "value",
        scale: true,
        axisLabel: { color: t.textMute, fontSize: 10, formatter: "{value}°" },
        splitLine: { lineStyle: { color: t.border, opacity: 0.3 } },
      },
      series: [
        { name: "min", type: "line", data: lo, stack: "band", symbol: "none",
          lineStyle: { opacity: 0 }, areaStyle: { opacity: 0 }, silent: true, sampling: "lttb" },
        { name: "range", type: "line", data: band, stack: "band", symbol: "none",
          lineStyle: { opacity: 0 }, areaStyle: { color: withAlpha(t.cool, 0.13) }, silent: true, sampling: "lttb" },
        { name: "mean", type: "line", data: mean, symbol: "none", smooth: true,
          lineStyle: { color: t.cool, width: 2, cap: "round" }, sampling: "lttb", z: 3 },
      ],
    }, { notMerge: true });
    chartRef.current.resize();
  }, [buckets, hasData]);

  useEffect(() => () => { chartRef.current?.dispose(); chartRef.current = null; }, []);

  return (
    <section class={`insights-card indoor-history${res.loading && data ? " is-updating" : ""}`}>
      <header class="load-pattern-head">
        <h2>Indoor temperature history</h2>
        <p class="muted">
          House temperature from the room sensors — mean with the
          {" "}<span class="ih-key">min–max band</span> per 15 minutes. Kept
          permanently, so it survives the raw readings being archived.
        </p>
        <div class="ih-windows">
          {WINDOWS.map((w) => (
            <button
              key={w}
              type="button"
              class={`ih-win${days === w ? " is-on" : ""}`}
              onClick={() => setDays(w)}
            >{w === 365 ? "1y" : `${w}d`}</button>
          ))}
        </div>
      </header>
      {res.loading && !data && <Spinner label="Loading indoor history…" />}
      {res.error && <p class="insights-error">Couldn't load indoor history: {res.error.message}</p>}
      <div ref={elRef} class="load-pattern-chart" hidden={!hasData} />
      {data && !hasData && (
        <p class="muted insights-empty">
          No indoor rollup yet — it builds from the room sensor daily (~03:15 UTC).
        </p>
      )}
    </section>
  );
}
