import { useEffect, useRef } from "preact/hooks";
import { makeChart, baseOption, chartTheme, type EChartsType } from "../../lib/charts";
import { useResolvedTheme } from "../../lib/theme";
import type { AgileDaySlotsResponse } from "../../lib/types";

interface SevenDayBarProps {
  days: AgileDaySlotsResponse[];
}

// Daily mean rate per day for the last N days, with min/max whiskers and the
// daily min/max in tooltip. Bars coloured by mean tone vs the median.
export function SevenDayBar({ days }: SevenDayBarProps) {
  const ref = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<EChartsType | null>(null);
  const theme = useResolvedTheme();

  useEffect(() => {
    if (!ref.current) return;
    const ch = makeChart(ref.current);
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
    if (!chartRef.current) return;
    const t = chartTheme();
    const base = baseOption();
    const sorted = days.slice().sort((a, b) => a.date.localeCompare(b.date));

    const summaries = sorted.map((d) => {
      const slots = d.slots || [];
      if (slots.length === 0) return { date: d.date, mean: null, min: null, max: null };
      let mn = Infinity;
      let mx = -Infinity;
      let sum = 0;
      for (const s of slots) {
        if (s.p < mn) mn = s.p;
        if (s.p > mx) mx = s.p;
        sum += s.p;
      }
      return { date: d.date, mean: sum / slots.length, min: mn, max: mx };
    });

    const xs = summaries.map((s) => s.date.slice(5));    // MM-DD
    const meanData = summaries.map((s) => s.mean);
    const errData = summaries.map((s) =>
      s.min != null && s.max != null && s.mean != null ? [s.min, s.max] : null,
    );

    chartRef.current.setOption({
      ...base,
      legend: { show: false },
      grid: { left: 40, right: 16, top: 20, bottom: 28, containLabel: true },
      tooltip: {
        ...(base.tooltip as object),
        formatter: (params: Array<{ dataIndex: number }>) => {
          const idx = params[0]?.dataIndex ?? 0;
          const s = summaries[idx];
          if (!s) return "";
          return `<strong>${s.date}</strong><br/>
                  Mean ${s.mean?.toFixed(1)}p<br/>
                  Range ${s.min?.toFixed(1)}p — ${s.max?.toFixed(1)}p`;
        },
      },
      xAxis: { ...(base.xAxis as object), data: xs },
      yAxis: { ...(base.yAxis as object), name: "p/kWh", nameTextStyle: { color: t.textDim, fontSize: 10 } },
      series: [
        {
          name: "Range",
          type: "custom",
          renderItem: (_p: unknown, api: { value: (i: number) => number; coord: (xy: [number | string, number]) => [number, number] }) => {
            const x = api.value(0) as unknown as string;
            const lo = api.value(1) as number;
            const hi = api.value(2) as number;
            const [px, pyLo] = api.coord([x, lo]);
            const [_px2, pyHi] = api.coord([x, hi]);
            return {
              type: "line",
              shape: { x1: px, y1: pyLo, x2: px, y2: pyHi },
              style: { stroke: t.border, lineWidth: 2, opacity: 0.7 },
            };
          },
          data: errData.map((r, i) => (r ? [xs[i], r[0], r[1]] : null)),
          z: 1,
        },
        {
          name: "Mean",
          type: "bar",
          barWidth: "60%",
          data: meanData.map((m) => (m == null ? null : {
            value: m,
            itemStyle: { color: t.accent, borderRadius: [3, 3, 0, 0] },
          })),
          z: 2,
        },
      ],
    }, { notMerge: true });
  }, [days, theme]);

  return <div ref={ref} style={{ width: "100%", height: "200px" }} />;
}
