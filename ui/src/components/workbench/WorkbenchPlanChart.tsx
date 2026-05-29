import { useEffect, useRef } from "preact/hooks";
import { makeChart, baseOption, chartTheme, barGradient, type EChartsType } from "../../lib/charts";
import { useResolvedTheme } from "../../lib/theme";
import type { WorkbenchSimSlot } from "../../lib/types";

interface WorkbenchPlanChartProps {
  slots: WorkbenchSimSlot[];
}

function localHM(iso: string | null): string {
  if (!iso) return "";
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
}

// Compact plan shape for a simulated LP run: grid import (up) / export (down)
// bars + battery SoC trajectory (right axis) + import price in the tooltip.
// Lets the operator see how a knob tweak reshapes the plan before promoting.
export function WorkbenchPlanChart({ slots }: WorkbenchPlanChartProps) {
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
    if (!chartRef.current || !slots.length) return;
    const t = chartTheme();
    const base = baseOption();
    const labels = slots.map((s) => localHM(s.t));
    const imp = slots.map((s) => round2(s.import_kwh));
    const exp = slots.map((s) => (s.export_kwh == null ? null : -(Math.round(s.export_kwh * 100) / 100)));
    const soc = slots.map((s) => round2(s.soc_kwh));
    const price = slots.map((s) => s.price_p);

    chartRef.current.setOption({
      ...base,
      grid: { left: 44, right: 48, top: 24, bottom: 24, containLabel: true },
      legend: { ...(base.legend as object), show: true },
      tooltip: {
        ...(base.tooltip as object),
        formatter: (params: Array<{ dataIndex: number }>) => {
          const i = params[0]?.dataIndex ?? 0;
          return `<strong>${labels[i]}</strong><br/>` +
            (price[i] != null ? `Price ${price[i]!.toFixed(1)}p/kWh<br/>` : "") +
            `Import ${(imp[i] ?? 0).toFixed(2)} kWh<br/>` +
            (exp[i] != null ? `Export ${(-exp[i]!).toFixed(2)} kWh<br/>` : "") +
            (soc[i] != null ? `SoC ${soc[i]!.toFixed(2)} kWh` : "");
        },
      },
      xAxis: { ...(base.xAxis as object), data: labels, axisLabel: { color: t.textMute, fontSize: 10, interval: 5 } },
      yAxis: [
        { ...(base.yAxis as object), name: "kWh", nameTextStyle: { color: t.textDim, fontSize: 10 } },
        {
          ...(base.yAxis as object),
          name: "SoC", position: "right",
          nameTextStyle: { color: t.textDim, fontSize: 10 },
          splitLine: { show: false },
          axisLabel: { color: t.textMute, fontSize: 10 },
        },
      ],
      series: [
        { name: "Import", type: "bar", stack: "grid", data: imp,
          itemStyle: { color: barGradient(t.importColor), borderRadius: [2, 2, 0, 0] }, z: 1 },
        { name: "Export", type: "bar", stack: "grid", data: exp,
          itemStyle: { color: barGradient(t.exportColor), borderRadius: [0, 0, 2, 2] }, z: 1 },
        { name: "SoC", type: "line", smooth: true, showSymbol: false, yAxisIndex: 1,
          data: soc, lineStyle: { color: t.batt, width: 2 }, z: 3 },
      ],
    }, { notMerge: true });
  }, [slots, theme]);

  return <div ref={ref} style={{ width: "100%", height: "220px" }} />;
}

function round2(n: number | null): number | null {
  return n == null ? null : Math.round(n * 100) / 100;
}
