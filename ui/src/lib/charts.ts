// Thin ECharts wrapper. Reads CSS custom properties so chart colours follow
// the design tokens — change tokens.css and charts re-theme. ECharts is loaded
// directly (no lazy chunk needed; vite.config.ts manualChunks already splits
// it into its own chunk).

import * as echarts from "echarts/core";
import {
  LineChart,
  BarChart,
  PieChart,
} from "echarts/charts";
import {
  GridComponent,
  TooltipComponent,
  LegendComponent,
  AxisPointerComponent,
  MarkAreaComponent,
  MarkLineComponent,
  TitleComponent,
  DataZoomComponent,
} from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import type { EChartsType } from "echarts/core";

echarts.use([
  LineChart,
  BarChart,
  PieChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  AxisPointerComponent,
  MarkAreaComponent,
  MarkLineComponent,
  TitleComponent,
  DataZoomComponent,
  CanvasRenderer,
]);

function cssVar(name: string, fallback: string): string {
  if (typeof getComputedStyle !== "function") return fallback;
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

export function chartTheme() {
  return {
    bg: cssVar("--bg-card", "#111827"),
    text: cssVar("--text", "#f3f4f6"),
    textDim: cssVar("--text-dim", "#9ca3af"),
    border: cssVar("--border", "#374151"),
    accent: cssVar("--accent", "#3b82f6"),
    ok: cssVar("--ok", "#10b981"),
    warn: cssVar("--warn", "#f59e0b"),
    bad: cssVar("--bad", "#ef4444"),
    cheap: cssVar("--cheap", "#10b981"),
    peak: cssVar("--peak", "#f59e0b"),
    neg: cssVar("--neg-price", "#2563eb"),
    pv: cssVar("--pv", "#fbbf24"),
    batt: cssVar("--batt", "#10b981"),
    grid: cssVar("--grid", "#60a5fa"),
    house: cssVar("--house", "#c084fc"),
    importColor: cssVar("--import", "#ef4444"),
    exportColor: cssVar("--export", "#10b981"),
  };
}

export function baseOption(): Record<string, unknown> {
  const t = chartTheme();
  return {
    backgroundColor: "transparent",
    textStyle: { color: t.text, fontFamily: "system-ui, sans-serif" },
    tooltip: {
      trigger: "axis",
      backgroundColor: t.bg,
      borderColor: t.border,
      borderWidth: 1,
      textStyle: { color: t.text, fontSize: 12 },
      axisPointer: { lineStyle: { color: t.border }, type: "cross" },
    },
    grid: { left: 48, right: 24, top: 24, bottom: 28, containLabel: true },
    legend: {
      textStyle: { color: t.textDim, fontSize: 11 },
      top: 4,
      right: 8,
      icon: "roundRect",
      itemWidth: 10,
      itemHeight: 6,
    },
    xAxis: {
      type: "category",
      axisLine: { lineStyle: { color: t.border } },
      axisTick: { show: false },
      axisLabel: { color: t.textDim, fontSize: 10 },
    },
    yAxis: {
      type: "value",
      splitLine: { lineStyle: { color: t.border, opacity: 0.4 } },
      axisLabel: { color: t.textDim, fontSize: 10 },
    },
  };
}

export function makeChart(el: HTMLDivElement): EChartsType {
  return echarts.init(el, undefined, { renderer: "canvas" });
}

export { echarts };
export type { EChartsType };
