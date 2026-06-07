import { useEffect, useRef } from "preact/hooks";
import { makeChart, baseOption, chartTheme, areaGradient, withAlpha, barGradient, type EChartsType } from "../../lib/charts";
import { useResolvedTheme } from "../../lib/theme";
import { reducedMotion } from "../../lib/motion";

// One reusable timeline chart shared by the Solar / Grid / Load widgets so the
// three read identically (the user's "4 widgets, same forecast-vs-actual line"
// ask) without each re-implementing the tier-band + now-marker machinery that
// used to live only in TodayPlanWidget.
//
// Two modes:
//   * INTRADAY (barMode=false): per 30-min slot — plan-vs-actual lines, an
//     import-price reference on the right axis, cheap/peak/negative tariff
//     background bands, and a pulsing "now" marker. For the "day" granularity.
//   * PERIOD (barMode=true): one bar group per point (day-of-week / day-of-month
//     / month) — actuals only, since historical intraday forecast isn't kept.

export interface TimelineLine {
  name: string;
  color: string;
  data: (number | null)[];
  dashed?: boolean;      // forecast/plan styling
  width?: number;
  area?: boolean;        // gradient fill (the one bold "actual" line)
  step?: boolean;        // price reference
  yAxis?: number;        // 0 = left (kWh), 1 = right (price p)
}

interface MetricTimelineProps {
  labels: string[];
  lines: TimelineLine[];
  /** Import price per slot (p/kWh) — drives the tariff bands + right axis. */
  prices?: (number | null)[];
  /** Slot index of "now" (intraday only); -1 / undefined to hide. */
  nowIdx?: number;
  cheapAt?: number | null;
  peakAt?: number | null;
  /** Render `lines` as grouped bars (period mode) instead of an intraday chart. */
  barMode?: boolean;
  height?: number;
  unit?: string;
}

type Tier = "negative" | "cheap" | "standard" | "peak" | null;

export function MetricTimeline({
  labels, lines, prices, nowIdx = -1, cheapAt, peakAt, barMode = false, height = 260, unit = "kWh",
}: MetricTimelineProps) {
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
    if (!chartRef.current || !labels.length) return;
    const t = chartTheme();
    const base = baseOption();
    const animate = !reducedMotion();
    const hasPrice = !barMode && (prices?.some((p) => p != null) ?? false);

    // --- Tariff-tier background bands (intraday only). Classify each slot by
    // import price into negative / cheap / peak; shade contiguous runs. Mirrors
    // TodayPlanWidget so all timelines share one tariff ribbon.
    const bands: Array<[{ xAxis: number; itemStyle: object }, { xAxis: number }]> = [];
    if (hasPrice && prices) {
      const known = prices.filter((p): p is number => p != null).slice().sort((a, b) => a - b);
      const pct = (q: number) => (known.length ? known[Math.min(known.length - 1, Math.floor(q * known.length))] : null);
      const cAt = cheapAt ?? pct(0.33);
      const pAt = peakAt ?? pct(0.75);
      const tierOf = (p: number | null): Tier => {
        if (p == null) return null;
        if (p < 0) return "negative";
        if (cAt != null && p <= cAt) return "cheap";
        if (pAt != null && p >= pAt) return "peak";
        return "standard";
      };
      const tierColor = (k: Tier): string =>
        k === "negative" ? t.neg : k === "cheap" ? t.cheap : k === "peak" ? t.peak : t.textMute;
      const tierFill = (k: Tier): object =>
        k === "negative"
          ? { color: withAlpha(t.neg, 0.26), borderColor: withAlpha(t.neg, 0.9), borderWidth: 1 }
          : k === "standard"
          ? { color: withAlpha(t.textMute, 0.05) }
          : { color: withAlpha(tierColor(k), 0.1) };
      let runStart = -1;
      let runTier: Tier = null;
      const flush = (endIdx: number) => {
        if (runStart < 0 || runTier == null) return;
        bands.push([{ xAxis: runStart - 0.5, itemStyle: tierFill(runTier) }, { xAxis: endIdx + 0.5 }]);
      };
      labels.forEach((_, i) => {
        const cur = tierOf(prices[i] ?? null);
        if (cur !== runTier) {
          if (runTier != null) flush(i - 1);
          runTier = cur;
          runStart = cur != null ? i : -1;
        }
      });
      if (runTier != null) flush(labels.length - 1);
    }

    const kwhSeries = lines.map((ln) => {
      if (barMode) {
        return {
          name: ln.name, type: "bar", data: ln.data, color: ln.color,
          itemStyle: { color: barGradient(ln.color), borderRadius: [3, 3, 0, 0] },
          barMaxWidth: 22, z: ln.area ? 3 : 2,
        };
      }
      return {
        name: ln.name, type: "line", smooth: true, showSymbol: false, connectNulls: false,
        color: ln.color, data: ln.data, yAxisIndex: ln.yAxis ?? 0,
        step: ln.step ? "middle" : undefined,
        lineStyle: {
          color: ln.color, width: ln.width ?? (ln.dashed ? 1.25 : 2.5),
          type: ln.dashed ? "dashed" : "solid", opacity: ln.dashed ? 0.8 : 1,
        },
        areaStyle: ln.area ? { color: areaGradient(ln.color, 0.4, 0.04) } : undefined,
        z: ln.area ? 4 : ln.dashed ? 2 : 3,
      };
    });

    const yAxes: object[] = [
      { ...(base.yAxis as object), axisLabel: { color: t.textMute, fontSize: 10, formatter: "{value}" } },
    ];
    if (hasPrice) {
      yAxes.push({
        ...(base.yAxis as object), position: "right", splitLine: { show: false },
        axisLabel: { color: t.textMute, fontSize: 10, formatter: "{value}p" },
      });
    }

    chartRef.current.setOption({
      ...base,
      grid: { left: 16, right: hasPrice ? 44 : 16, top: 16, bottom: 24, containLabel: true },
      legend: { show: false },
      tooltip: {
        ...(base.tooltip as object),
        valueFormatter: (v: number | null) => (v == null ? "—" : `${Number(v).toFixed(2)} ${unit}`),
      },
      xAxis: {
        ...(base.xAxis as object), data: labels,
        axisLabel: { color: t.textMute, fontSize: 10, interval: barMode ? "auto" : 5 },
      },
      yAxis: yAxes,
      series: [
        // Silent baseline carries the tariff bands + now marker (intraday only).
        ...(!barMode ? [{
          name: "_bands", type: "line", data: labels.map(() => null), silent: true,
          markArea: bands.length ? { silent: true, data: bands } : undefined,
          z: 0,
        }] : []),
        ...kwhSeries,
        // Import price → dashed step on the right axis (intraday only).
        ...(hasPrice ? [{
          name: "Import price", type: "line", step: "middle", showSymbol: false, color: t.importColor,
          yAxisIndex: 1, data: prices, lineStyle: { color: t.importColor, width: 1.5, opacity: 0.8, type: "dashed" }, z: 1,
        }] : []),
        // Pulsing "now" ripple at the current slot.
        ...(!barMode && nowIdx >= 0 ? [{
          name: "_now", type: "effectScatter", silent: true, coordinateSystem: "cartesian2d",
          symbolSize: 9, z: 6, showEffectOn: "render",
          rippleEffect: { period: animate ? 2.4 : 0, scale: animate ? 3 : 1, brushType: "stroke" },
          itemStyle: { color: t.accent, shadowBlur: 8, shadowColor: t.accent },
          data: [[nowIdx, 0]],
        }] : []),
      ],
    }, { notMerge: true });
  }, [labels, lines, prices, nowIdx, cheapAt, peakAt, barMode, theme, unit]);

  return <div ref={ref} style={{ width: "100%", height: `${height}px` }} />;
}

export function localHM(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
}

/** Slot index of "now" within a day's slots, or -1 if outside. */
export function nowIndexOf(slots: { slot_utc: string }[], nowUtc?: string): number {
  if (!slots.length) return -1;
  const nowMs = nowUtc ? new Date(nowUtc).getTime() : Date.now();
  const firstMs = new Date(slots[0].slot_utc).getTime();
  const lastMs = new Date(slots[slots.length - 1].slot_utc).getTime() + 30 * 60_000;
  if (nowMs < firstMs || nowMs >= lastMs) return -1;
  const idx = slots.findIndex((s) => new Date(s.slot_utc).getTime() > nowMs);
  return idx <= 0 ? slots.length - 1 : idx - 1;
}

/** Short label for a period chart point's `date` given the granularity. */
export function periodPointLabel(dateISO: string, gran: string): string {
  const d = new Date(`${dateISO}T00:00:00`);
  if (gran === "year") return d.toLocaleDateString([], { month: "short" });
  if (gran === "week") return d.toLocaleDateString([], { weekday: "short" });
  return String(d.getDate()); // month → day-of-month
}
