// Thin ECharts wrapper. Reads CSS custom properties so chart colours follow
// the design tokens — change tokens.css and charts re-theme. ECharts is loaded
// directly (no lazy chunk needed; vite.config.ts manualChunks already splits
// it into its own chunk).

import * as echarts from "echarts/core";
import { reducedMotion } from "./motion";
import {
  LineChart,
  BarChart,
  PieChart,
  EffectScatterChart,
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
  EffectScatterChart,
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
    bgCard2: cssVar("--bg-card-2", "#1f2937"),
    text: cssVar("--text", "#f3f4f6"),
    textDim: cssVar("--text-dim", "#9ca3af"),
    textMute: cssVar("--text-mute", "#6b7280"),
    border: cssVar("--border", "#374151"),
    radius: cssVar("--radius", "10px"),
    accent: cssVar("--accent", "#3b82f6"),
    ok: cssVar("--ok", "#10b981"),
    warn: cssVar("--warn", "#f59e0b"),
    bad: cssVar("--bad", "#ef4444"),
    cheap: cssVar("--cheap", "#10b981"),
    peak: cssVar("--peak", "#f59e0b"),
    neg: cssVar("--neg-price", "#2563eb"),
    pv: cssVar("--pv", "#fbbf24"),
    thermal: cssVar("--thermal", "#fb923c"),
    batt: cssVar("--batt", "#10b981"),
    grid: cssVar("--grid", "#60a5fa"),
    house: cssVar("--house", "#c084fc"),
    importColor: cssVar("--import", "#ef4444"),
    exportColor: cssVar("--export", "#10b981"),
  };
}

// Drives ECharts animation:false under reduced motion — honours the in-app
// motion override (default on), not just the OS setting.
function prefersReducedMotion(): boolean {
  return reducedMotion();
}

// #rrggbb + alpha → rgba() string (ECharts canvas doesn't accept color-mix()).
export function withAlpha(hex: string, a: number): string {
  const h = hex.trim().replace("#", "");
  if (h.length !== 6) return hex;
  const r = parseInt(h.slice(0, 2), 16);
  const g = parseInt(h.slice(2, 4), 16);
  const b = parseInt(h.slice(4, 6), 16);
  return `rgba(${r}, ${g}, ${b}, ${a})`;
}

// Vertical gradient for bars/areas — strong at top, fading down.
export function barGradient(color: string, top = 0.95, bottom = 0.55) {
  return {
    type: "linear", x: 0, y: 0, x2: 0, y2: 1,
    colorStops: [
      { offset: 0, color: withAlpha(color, top) },
      { offset: 1, color: withAlpha(color, bottom) },
    ],
  };
}
// Soft area fill under a line — fades to transparent.
export function areaGradient(color: string, top = 0.28, bottom = 0.02) {
  return {
    type: "linear", x: 0, y: 0, x2: 0, y2: 1,
    colorStops: [
      { offset: 0, color: withAlpha(color, top) },
      { offset: 1, color: withAlpha(color, bottom) },
    ],
  };
}

export function baseOption(): Record<string, unknown> {
  const t = chartTheme();
  const reduced = prefersReducedMotion();
  return {
    backgroundColor: "transparent",
    textStyle: { color: t.text, fontFamily: "system-ui, sans-serif" },
    // Whole-chart morph timing aligned to the design language (--dur-enter
    // 420ms cubicOut). Disabled under reduced motion.
    animation: !reduced,
    animationDuration: 420,
    animationDurationUpdate: 420,
    animationEasing: "cubicOut",
    animationEasingUpdate: "cubicOut",
    tooltip: {
      trigger: "axis",
      // Vibrancy panel: solid card-2 surface + blur + soft shadow + rounded.
      // Concrete rgba (canvas can't take color-mix); blur via extraCssText.
      backgroundColor: withAlpha(t.bgCard2.startsWith("#") ? t.bgCard2 : "#1f2937", 0.92),
      borderColor: t.border,
      borderWidth: 1,
      padding: [8, 12],
      textStyle: { color: t.text, fontSize: 12 },
      extraCssText: "backdrop-filter: blur(12px); border-radius: 12px; box-shadow: 0 8px 24px rgba(0,0,0,0.35);",
      axisPointer: { lineStyle: { color: t.border, opacity: 0.5 }, type: "line" },
    },
    grid: { left: 48, right: 24, top: 24, bottom: 28, containLabel: true },
    legend: {
      textStyle: { color: t.textMute, fontSize: 11 },
      top: 4,
      right: 8,
      icon: "roundRect",
      itemWidth: 10,
      itemHeight: 6,
    },
    xAxis: {
      type: "category",
      // No baseline rule — the data carries the structure (Apple/Tesla dataviz).
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { color: t.textMute, fontSize: 11 },
    },
    yAxis: {
      type: "value",
      // Faint dashed gridlines only — quiet, not a cage.
      splitLine: { lineStyle: { color: t.border, opacity: 0.12, type: "dashed" } },
      axisLine: { show: false },
      axisLabel: { color: t.textMute, fontSize: 11 },
    },
  };
}

export function makeChart(el: HTMLDivElement): EChartsType {
  const chart = echarts.init(el, undefined, { renderer: "canvas" });
  return chart;
}

export { echarts };
export type { EChartsType };
