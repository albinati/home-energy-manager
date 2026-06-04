import { useEffect, useRef } from "preact/hooks";
import { makeChart, baseOption, chartTheme, areaGradient, withAlpha, type EChartsType } from "../../lib/charts";
import { useResolvedTheme } from "../../lib/theme";
import { reducedMotion } from "../../lib/motion";
import type { PvTodayResponse } from "../../lib/types";
import "./today-plan.css";

interface TodayPlanWidgetProps {
  pv: PvTodayResponse | null;
  loading: boolean;
  // Tariff-tier thresholds (p/kWh) for the cheap/peak background shading. From
  // /metrics — same thresholds the rest of the app classifies bands with.
  cheapThresholdP?: number | null;
  peakThresholdP?: number | null;
}

function localHM(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
}

type Tier = "negative" | "cheap" | "peak" | null;

// One chart that answers "what's the plan today?" without tab-switching:
// import price + cheap/peak rate windows (background) + load forecast + the
// heating plan (tank-temp trajectory) + PV planned (background) vs realised
// (foreground). PV/load/DHW on the left kWh axis, price on the right p axis,
// tank °C on a second right axis. All series come from /pv/today — one
// full-day, server-aligned source (no client-side ISO key-matching).
export function TodayPlanWidget({ pv, loading, cheapThresholdP, peakThresholdP }: TodayPlanWidgetProps) {
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
    if (!chartRef.current || !pv?.slots?.length) return;
    const t = chartTheme();
    const base = baseOption();
    const slots = pv.slots;
    const labels = slots.map((s) => localHM(s.slot_utc));

    // Three PV lines: the COMMITTED plan (frozen since the last LP solve), the
    // LIVE forecast (re-fetched per request — revises through the day), and the
    // realised actual. See get_pv_today: pv_planned_kwh vs pv_forecast_kwh.
    const pvCommitted = slots.map((s) => (s.pv_planned_kwh == null ? null : round2(s.pv_planned_kwh)));
    const pvForecastLive = slots.map((s) => round2(s.pv_forecast_kwh));
    const pvActual = slots.map((s) => (s.pv_actual_kwh == null ? null : round2(s.pv_actual_kwh)));
    const load = slots.map((s) => (s.base_load_kwh == null ? null : round2(s.base_load_kwh)));
    const price = slots.map((s) => (s.import_price_p == null ? null : s.import_price_p));

    // --- Rate-tier background bands. Classify each slot by import price into
    // negative / cheap / peak (standard → no shade), then shade contiguous
    // runs. Thresholds come from /metrics; if absent, fall back to this day's
    // own price distribution (33rd pct = cheap, 75th = peak).
    const known = price.filter((p): p is number => p != null).slice().sort((a, b) => a - b);
    const pct = (q: number) => (known.length ? known[Math.min(known.length - 1, Math.floor(q * known.length))] : null);
    const cheapAt = cheapThresholdP ?? pct(0.33);
    const peakAt = peakThresholdP ?? pct(0.75);
    const tierOf = (p: number | null): Tier => {
      if (p == null) return null;
      if (p < 0) return "negative";
      if (cheapAt != null && p <= cheapAt) return "cheap";
      if (peakAt != null && p >= peakAt) return "peak";
      return null;
    };
    const tierColor = (k: Tier): string =>
      k === "negative" ? t.neg : k === "cheap" ? t.cheap : t.peak;
    // markArea references slot INDEX (DST-safe; two slots can share a label).
    const bands: Array<[{ xAxis: number; itemStyle: object }, { xAxis: number }]> = [];
    let runStart = -1;
    let runTier: Tier = null;
    const flush = (endIdx: number) => {
      if (runStart < 0 || runTier == null) return;
      bands.push([
        { xAxis: runStart, itemStyle: { color: withAlpha(tierColor(runTier), runTier === "peak" ? 0.16 : 0.20) } },
        { xAxis: endIdx },
      ]);
    };
    slots.forEach((_, i) => {
      const cur = tierOf(price[i]);
      if (cur !== runTier) {
        if (runTier != null) flush(i - 1);
        runTier = cur;
        runStart = cur != null ? i : -1;
      }
    });
    if (runTier != null) flush(slots.length - 1);

    // "Now" marker — only when now falls within this day's slots.
    const nowMs = pv.now_utc ? new Date(pv.now_utc).getTime() : Date.now();
    const firstMs = new Date(slots[0].slot_utc).getTime();
    const lastMs = new Date(slots[slots.length - 1].slot_utc).getTime() + 30 * 60_000;
    let nowIdx = -1;
    if (nowMs >= firstMs && nowMs < lastMs) {
      const idx = slots.findIndex((s) => new Date(s.slot_utc).getTime() > nowMs);
      nowIdx = idx <= 0 ? slots.length - 1 : idx - 1;
    }
    const animate = !reducedMotion();

    chartRef.current.setOption({
      ...base,
      // Legend pinned bottom so it never collides with the top axis labels or
      // the plot (the caption-overlap fix).
      grid: { left: 16, right: 44, top: 16, bottom: 44, containLabel: true },
      legend: {
        ...(base.legend as object),
        show: true, top: undefined, right: undefined, bottom: 4, left: "center",
        data: ["PV actual", "PV plan (committed)", "PV forecast (live)", "Load forecast", "Import price"],
      },
      tooltip: {
        ...(base.tooltip as object),
        formatter: (params: Array<{ dataIndex: number }>) => {
          const i = params[0]?.dataIndex ?? 0;
          const s = slots[i];
          if (!s) return "";
          const tier = tierOf(price[i]);
          return `<strong>${labels[i]}</strong>${tier ? ` · ${tier}` : ""}<br/>` +
            (price[i] != null ? `Import ${price[i]!.toFixed(1)}p/kWh<br/>` : "") +
            (pvCommitted[i] != null ? `PV plan ${pvCommitted[i]!.toFixed(2)} kWh<br/>` : "") +
            `PV forecast (live) ${pvForecastLive[i].toFixed(2)} kWh<br/>` +
            (pvActual[i] != null ? `PV actual ${pvActual[i]!.toFixed(2)} kWh<br/>` : "") +
            (load[i] != null ? `Load ${load[i]!.toFixed(2)} kWh` : "");
        },
      },
      xAxis: { ...(base.xAxis as object), data: labels, axisLabel: { color: t.textMute, fontSize: 10, interval: 5 } },
      yAxis: [
        { ...(base.yAxis as object), axisLabel: { color: t.textMute, fontSize: 10, formatter: "{value}" } },
        {
          ...(base.yAxis as object),
          position: "right", splitLine: { show: false },
          axisLabel: { color: t.textMute, fontSize: 10, formatter: "{value}p" },
        },
      ],
      series: [
        // Rate-tier shading lives on a silent baseline series (z below all).
        {
          name: "_bands", type: "line", data: pvForecastLive.map(() => null), silent: true,
          markArea: bands.length ? { silent: true, data: bands } : undefined,
          markLine: nowIdx >= 0 ? {
            silent: true, symbol: "none",
            lineStyle: { color: t.text, width: 1.5, type: "solid", opacity: 0.5 },
            label: { show: false },
            data: [{ xAxis: nowIdx }],
          } : undefined,
          z: 0,
        },
        // Three PV lines share the PV hue, distinguished by treatment:
        //   committed plan = dotted, mid-alpha (the frozen plan being executed)
        //   live forecast  = dashed, dim (revises through the day)
        //   actual         = solid, bright, filled (realised)
        // `color` is set so the legend swatch matches the line (ECharts colours
        // the legend marker from series.color, NOT lineStyle).
        {
          name: "PV plan (committed)", type: "line", smooth: true, showSymbol: false,
          connectNulls: false, color: withAlpha(t.pv, 0.7),
          data: pvCommitted, lineStyle: { color: withAlpha(t.pv, 0.7), width: 1.5, type: "dotted" }, z: 3,
        },
        {
          name: "PV forecast (live)", type: "line", smooth: true, showSymbol: false, color: withAlpha(t.pv, 0.4),
          data: pvForecastLive, lineStyle: { color: withAlpha(t.pv, 0.4), width: 1.25, type: "dashed" },
          areaStyle: { color: withAlpha(t.pv, 0.05) }, z: 2,
        },
        // PV actual — realised in the FOREGROUND: vivid PV colour, thick, gradient fill.
        {
          name: "PV actual", type: "line", smooth: true, showSymbol: false, connectNulls: false, color: t.pv,
          data: pvActual, lineStyle: { color: t.pv, width: 2.75 },
          areaStyle: { color: areaGradient(t.pv, 0.36, 0.04) }, z: 4,
        },
        // Load forecast is a reference → dim, dashed (its own cool hue).
        {
          name: "Load forecast", type: "line", smooth: true, showSymbol: false, color: withAlpha(t.grid, 0.5),
          data: load, lineStyle: { color: withAlpha(t.grid, 0.5), width: 1.25, type: "dashed" }, z: 3,
        },
        {
          name: "Import price", type: "line", step: "middle", showSymbol: false, color: t.importColor,
          yAxisIndex: 1, data: price, lineStyle: { color: t.importColor, width: 1.5, opacity: 0.75 }, z: 1,
        },
        // Blinking "now" — a pulsing ripple at the current slot on the baseline.
        ...(nowIdx >= 0 ? [{
          name: "_now", type: "effectScatter", silent: true,
          coordinateSystem: "cartesian2d", symbolSize: 10, z: 6,
          showEffectOn: "render",
          rippleEffect: { period: animate ? 2.4 : 0, scale: animate ? 3.2 : 1, brushType: "stroke" },
          itemStyle: { color: t.accent, shadowBlur: 8, shadowColor: t.accent },
          data: [[nowIdx, 0]],
        }] : []),
      ],
    }, { notMerge: true });
  }, [pv, theme, cheapThresholdP, peakThresholdP]);

  const acc = pv?.accuracy;

  return (
    <div class="today-plan">
      {acc && (
        <div class="today-plan-acc">
          <span>PV today so far: <strong>{acc.actual_kwh.toFixed(1)}</strong> kWh actual vs <strong>{acc.forecast_kwh.toFixed(1)}</strong> planned</span>
          <span class="today-plan-acc-sep">·</span>
          <span title="Mean absolute error per slot, and forecast bias (positive = forecast under-predicted).">
            MAE {acc.mae_kwh.toFixed(2)} kWh · bias {acc.bias_kwh >= 0 ? "+" : ""}{acc.bias_kwh.toFixed(1)} kWh
          </span>
        </div>
      )}
      <div ref={ref} style={{ width: "100%", height: "320px" }} />
      <div class="today-plan-bands">
        <span class="today-plan-band today-plan-band--cheap">cheap rate</span>
        <span class="today-plan-band today-plan-band--peak">peak rate</span>
        <span class="today-plan-band today-plan-band--neg">negative price</span>
        <span class="today-plan-band-hint">shaded = tariff tier · ◉ now</span>
      </div>
      {pv?.plan_committed_at && (
        <p class="today-plan-note muted">
          Committed plan from {localHM(pv.plan_committed_at)} (the line the system is executing);
          the live forecast re-fetches from Quartz through the day, so it moves.
        </p>
      )}
      {!pv?.slots?.length && !loading && <p class="muted">No plan data for today yet.</p>}
    </div>
  );
}

function round2(n: number): number {
  return Math.round(n * 100) / 100;
}
