import { useEffect, useRef } from "preact/hooks";
import { makeChart, type EChartsType } from "../../lib/charts";
import "./forecast.css";

interface ForecastChartProps {
  option: Record<string, unknown>;
  height?: number;
  group?: string;
}

// Generic ECharts wrapper. Groups all charts sharing the same `group` id so
// hover sync works (axisPointer.link). Resizes on window resize.
export function ForecastChart({ option, height = 220, group }: ForecastChartProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<EChartsType | null>(null);

  useEffect(() => {
    if (!containerRef.current) return;
    const ch = makeChart(containerRef.current);
    chartRef.current = ch;
    if (group) ch.group = group;
    const onResize = () => ch.resize();
    window.addEventListener("resize", onResize);
    return () => {
      window.removeEventListener("resize", onResize);
      ch.dispose();
      chartRef.current = null;
    };
  }, [group]);

  useEffect(() => {
    if (!chartRef.current) return;
    chartRef.current.setOption(option, { notMerge: true });
  }, [option]);

  return <div ref={containerRef} class="forecast-chart" style={{ height: `${height}px` }} />;
}
