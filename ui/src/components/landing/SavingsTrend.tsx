import { useEffect, useRef } from "preact/hooks";
import { makeChart, baseOption, chartTheme, type EChartsType } from "../../lib/charts";
import type { MonthlyEnergy } from "../../lib/types";

interface SavingsTrendProps {
  monthly: MonthlyEnergy[];
}

// Tiny inline ECharts line — monthly savings vs SVT shadow. We use SVT
// (variable tariff) as the baseline because it's the universal point of
// comparison; fixed-tariff comparison is an alt KPI in the strip above.
export function SavingsTrend({ monthly }: SavingsTrendProps) {
  const ref = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<EChartsType | null>(null);

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
    const data = monthly.slice().sort((a, b) => a.month.localeCompare(b.month));
    const xs = data.map((m) => m.month);
    const savings = data.map((m) => m.savings_vs_svt_gbp ?? 0);

    chartRef.current.setOption({
      ...base,
      legend: { show: false },
      grid: { left: 36, right: 16, top: 18, bottom: 24, containLabel: true },
      xAxis: { ...(base.xAxis as object), data: xs, axisLabel: { color: t.textDim, fontSize: 10, formatter: (v: string) => v.slice(5) } },
      yAxis: { ...(base.yAxis as object), name: "£", nameTextStyle: { color: t.textDim, fontSize: 10 } },
      series: [
        {
          type: "bar",
          name: "Savings vs SVT",
          data: savings,
          itemStyle: {
            color: (params: { value: number }) => (params.value >= 0 ? t.ok : t.bad),
            borderRadius: [4, 4, 0, 0],
          },
          barWidth: "60%",
        },
      ],
    }, { notMerge: true });
  }, [monthly]);

  return <div ref={ref} style={{ width: "100%", height: "220px" }} />;
}
