import { useEffect, useRef } from "preact/hooks";
import { makeChart, baseOption, chartTheme, areaGradient, withAlpha, barGradient, timeAxis, insideZoom, forecastWash, isCoarsePointer, SLOT_MS, type EChartsType } from "../../lib/charts";
import { useResolvedTheme } from "../../lib/theme";
import { reducedMotion } from "../../lib/motion";
import { liveWindow, centerWindow, useLiveWindow, type LiveWindowBounds } from "../../lib/liveWindow";
import { useChartPan } from "../../lib/navMotion";

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
//     / month). Lines with `line: true` still render as lines — the committed
//     daily forecast IS kept (load_error_log / pv_error_log, #624) and rides
//     the bars as a dashed overlay.

export interface TimelineLine {
  name: string;
  color: string;
  data: (number | null)[];
  dashed?: boolean;      // forecast/plan styling
  line?: boolean;        // force a LINE even in barMode (forecast overlay on daily bars)
  width?: number;
  area?: boolean;        // gradient fill (the one bold "actual" line)
  step?: boolean;        // price reference
  yAxis?: number;        // 0 = left (kWh), 1 = right (price p)
  isPrice?: boolean;     // a price series: forced onto the right p-axis + p-formatted in the tooltip
}

interface MetricTimelineProps {
  labels: string[];
  lines: TimelineLine[];
  /** Price per slot (p/kWh) drawn on the right axis — IMPORT on the consumption
   * widget, EXPORT (Octopus Outgoing) on the generation widget. */
  prices?: (number | null)[];
  /** Optional price series that drives the cheap/peak/negative tariff SHADING
   * (the import price). Shading appears ONLY when this is provided — so a
   * widget can show a price LINE (e.g. export) without the import zones, which
   * avoids both timelines repeating the same tariff bands. */
  bandPrices?: (number | null)[];
  priceLabel?: string;          // right-axis price series name (Import/Export price)
  priceColor?: string;          // right-axis line colour (import red / export green)
  /** Slot index of "now" (intraday only); -1 / undefined to hide. */
  nowIdx?: number;
  /** Live-window mode (intraday day view): per-slot START timestamps + the day
   *  bounds. When present (and not barMode), the chart switches to a real time
   *  axis with the NOW-centred rolling window (shared with the other cockpit
   *  charts). Absent → the legacy category axis + integer nowIdx. */
  live?: { axisMs: number[]; dayStartMs: number; dayEndMs: number; nowMs: number };
  cheapAt?: number | null;
  peakAt?: number | null;
  /** Render `lines` as grouped bars (period mode) instead of an intraday chart. */
  barMode?: boolean;
  height?: number;
  unit?: string;
}

type Tier = "negative" | "cheap" | "standard" | "peak" | null;

export function MetricTimeline({
  labels, lines, prices, bandPrices, priceLabel = "Import price", priceColor,
  nowIdx = -1, live, cheapAt, peakAt, barMode = false, height = 260, unit = "kWh",
}: MetricTimelineProps) {
  const ref = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<EChartsType | null>(null);
  const sigRef = useRef<string>("");
  const boundsRef = useRef<LiveWindowBounds | null>(null);
  const theme = useResolvedTheme();
  // Live window (shared with the other cockpit charts) — only in intraday mode.
  useLiveWindow(chartRef, boundsRef);
  useChartPan(ref, boundsRef);

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
    // Live-window mode: real time axis + rolling window (intraday day view only).
    const lv = !barMode && live && live.axisMs.length === labels.length ? live : null;
    boundsRef.current = lv ? { dayStartMs: lv.dayStartMs, dayEndMs: lv.dayEndMs, nowMs: lv.nowMs } : null;
    const coarse = lv ? isCoarsePointer() : false;
    // In live mode, series data become [ts, value]; the tariff bands + now marker
    // use real timestamps (interval-true becomes literal).
    const pair = (arr: (number | null)[]): Array<[number, number | null]> | (number | null)[] =>
      lv ? arr.map((v, i): [number, number | null] => [lv.axisMs[i], v]) : arr;
    const nowInRange = !!lv && lv.nowMs >= lv.dayStartMs && lv.nowMs < lv.dayEndMs;
    const win = lv
      ? ((liveWindow.value.startMs && liveWindow.value.endMs && !liveWindow.value.follow)
          ? { startMs: liveWindow.value.startMs, endMs: liveWindow.value.endMs }
          : centerWindow(lv.nowMs, lv.dayStartMs, lv.dayEndMs))
      : { startMs: 0, endMs: 0 };
    const washArea = nowInRange ? forecastWash(lv!.nowMs, lv!.dayEndMs) : [];
    // Right p-axis exists when there's a `prices` series OR any isPrice line.
    const hasPriceLines = lines.some((l) => l.isPrice);
    const hasPrice = !barMode && ((prices?.some((p) => p != null) ?? false) || hasPriceLines);
    // Series rendered as prices (pence-formatted, right axis): the `prices`
    // series (named priceLabel) plus any line flagged isPrice.
    const priceNames = new Set<string>([priceLabel, ...lines.filter((l) => l.isPrice).map((l) => l.name)]);
    // Shading appears ONLY when bandPrices is given (the import price). A widget
    // that just wants a price LINE (export) passes no bandPrices → no zones, so
    // the two timelines don't both repeat the same tariff bands.
    const shadePrices = bandPrices;
    const hasBands = !barMode && (shadePrices?.some((p) => p != null) ?? false);

    // --- Tariff-tier background bands (intraday only). Classify each slot by
    // import price into negative / cheap / peak; shade contiguous runs. Mirrors
    // TodayPlanWidget so all timelines share one tariff ribbon.
    const bands: Array<[{ xAxis: number; itemStyle: object }, { xAxis: number }]> = [];
    if (hasBands && shadePrices) {
      const prices = shadePrices;  // shadow: tier classification uses the band series
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
        // Interval-true: slot i spans [start, end]. Live mode uses real
        // timestamps ([axisMs[start], axisMs[end]+SLOT_MS]); category mode keeps
        // the fractional index the old cell-centred fix required.
        if (lv) {
          bands.push([{ xAxis: lv.axisMs[runStart], itemStyle: tierFill(runTier) }, { xAxis: lv.axisMs[endIdx] + SLOT_MS }]);
        } else {
          bands.push([{ xAxis: runStart, itemStyle: tierFill(runTier) }, { xAxis: Math.min(endIdx + 1, labels.length - 0.5) }]);
        }
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
      if (barMode && !ln.line) {
        return {
          name: ln.name, type: "bar", data: ln.data, color: ln.color,
          itemStyle: { color: barGradient(ln.color), borderRadius: [3, 3, 0, 0] },
          barMaxWidth: 22, z: ln.area ? 3 : 2,
        };
      }
      return {
        name: ln.name, type: "line", smooth: true, showSymbol: false, connectNulls: false,
        color: ln.color, data: pair(ln.data), yAxisIndex: ln.isPrice ? 1 : (ln.yAxis ?? 0),
        step: ln.step || ln.isPrice ? "end" : undefined,
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

    // Same line/bar structure as the previous render → MERGE so ECharts
    // tweens the series to the new day's data (sliding-day morph); any
    // structural change (series added/removed, bar↔line) → full rebuild.
    // `lv` toggles the axis type → force a full rebuild when it flips (category↔time).
    const sig = `${barMode}|${!!lv}|${coarse}|${hasPrice}|${hasBands}|` + lines.map((l) => `${l.name}:${l.line ? "l" : ""}${l.dashed ? "d" : ""}`).join(",");
    chartRef.current.setOption({
      ...base,
      grid: { left: 16, right: hasPrice ? 44 : 16, top: 16, bottom: 24, containLabel: true },
      legend: { show: false },
      tooltip: {
        ...(base.tooltip as object),
        // Hide the silent helper series (_bands / _now) and format the price
        // series in pence, everything else in the energy unit.
        formatter: (params: Array<{ axisValue?: string; seriesName?: string; value?: number | null; marker?: string }>) => {
          const arr = Array.isArray(params) ? params : [params];
          if (!arr.length) return "";
          const rows = arr
            .filter((p) => p.seriesName && !p.seriesName.startsWith("_"))
            .map((p) => {
              if (p.value == null || !Number.isFinite(p.value)) return "";
              const isPrice = p.seriesName != null && priceNames.has(p.seriesName);
              const txt = isPrice ? `${Number(p.value).toFixed(1)}p` : `${Number(p.value).toFixed(2)} ${unit}`;
              return `<div>${p.marker ?? ""} ${p.seriesName}: <strong>${txt}</strong></div>`;
            })
            .join("");
          return `<strong>${arr[0].axisValue ?? ""}</strong>${rows}`;
        },
      },
      // Live mode → time axis (window via dataZoom on desktop, axis min/max on
      // touch); else the legacy category axis.
      xAxis: lv
        ? (coarse
            ? { ...timeAxis(win.startMs, win.endMs), axisLabel: { color: t.textMute, fontSize: 10, hideOverlap: true, formatter: "{HH}:{mm}" } }
            : { ...timeAxis(lv.dayStartMs, lv.dayEndMs), axisLabel: { color: t.textMute, fontSize: 10, hideOverlap: true, formatter: "{HH}:{mm}" } })
        : { ...(base.xAxis as object), data: labels, axisLabel: { color: t.textMute, fontSize: 10, interval: barMode ? "auto" : 5 } },
      ...(lv && !coarse ? { dataZoom: [insideZoom(win.startMs, win.endMs)] } : {}),
      yAxis: yAxes,
      series: [
        // Silent baseline carries the tariff bands + forecast wash + now marker.
        ...(!barMode ? [{
          name: "_bands", type: "line", data: lv ? lv.axisMs.map((ms) => [ms, null]) : labels.map(() => null), silent: true,
          markArea: (bands.length || washArea.length) ? { silent: true, data: [...washArea, ...bands] } : undefined,
          markLine: nowInRange ? { silent: true, symbol: "none",
            lineStyle: { color: t.text, width: 1.5, type: "solid", opacity: 0.5 },
            label: { show: false }, data: [{ xAxis: lv!.nowMs }] } : undefined,
          z: 0,
        }] : []),
        ...kwhSeries,
        // Price → dashed step on the right axis (intraday only). Named per the
        // widget (export on generation, import on consumption).
        ...(hasPrice ? [{
          name: priceLabel, type: "line", step: "end", showSymbol: false, color: priceColor ?? t.importColor,
          yAxisIndex: 1, data: pair(prices ?? []), lineStyle: { color: priceColor ?? t.importColor, width: 1.5, opacity: 0.85, type: "dashed" }, z: 1,
        }] : []),
        // Pulsing "now": a real timestamp in live mode, else the integer slot.
        ...(!barMode && (lv ? nowInRange : nowIdx >= 0) ? [{
          name: "_now", type: "effectScatter", silent: true,
          symbolSize: 9, z: 6, showEffectOn: "render",
          rippleEffect: { period: animate ? 2.4 : 0, scale: animate ? 3 : 1, brushType: "stroke" },
          itemStyle: { color: t.accent, shadowBlur: 8, shadowColor: t.accent },
          data: [[lv ? lv.nowMs : nowIdx, 0]],
        }] : []),
      ],
    }, { notMerge: sig !== sigRef.current });
    sigRef.current = sig;
  }, [labels, lines, prices, nowIdx, live, cheapAt, peakAt, barMode, theme, unit]);

  // Text alternative for screen readers — the canvas itself is opaque to AT,
  // so name the series + unit (and the price overlay when present).
  const hasPrice = !!prices && prices.some((p) => p != null);
  const chartLabel =
    `${barMode ? "Bar chart" : "Time-series chart"} in ${unit}: ` +
    `${lines.map((l) => l.name).join(", ")}` +
    `${hasPrice ? `, with ${priceLabel} overlay` : ""}`;

  return (
    <div
      ref={ref}
      role="img"
      aria-label={chartLabel}
      style={{ width: "100%", height: `${height}px` }}
    />
  );
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
