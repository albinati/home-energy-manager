import { useEffect, useRef } from "preact/hooks";
import { makeChart, baseOption, chartTheme, areaGradient, withAlpha, type EChartsType } from "../../lib/charts";
import { useResolvedTheme } from "../../lib/theme";
import { reducedMotion } from "../../lib/motion";
import type { PvTodayResponse, ExecutionTodayResponse } from "../../lib/types";
import "./today-plan.css";

interface TodayPlanWidgetProps {
  pv: PvTodayResponse | null;
  loading: boolean;
  // Execution slots — used to overlay MEASURED base load (consumption − daikin −
  // appliance) against the forecast base-load line.
  execution?: ExecutionTodayResponse | null;
  // Tariff-tier thresholds (p/kWh) for the cheap/peak background shading. From
  // /metrics — same thresholds the rest of the app classifies bands with.
  cheapThresholdP?: number | null;
  peakThresholdP?: number | null;
}

function localHM(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
}

type Tier = "negative" | "cheap" | "standard" | "peak" | null;

// One chart that answers "what's the plan today?" without tab-switching:
// import price + cheap/peak rate windows (background) + load forecast + the
// heating plan (tank-temp trajectory) + PV planned (background) vs realised
// (foreground). PV/load/DHW on the left kWh axis, price on the right p axis,
// tank °C on a second right axis. All series come from /pv/today — one
// full-day, server-aligned source (no client-side ISO key-matching).
export function TodayPlanWidget({ pv, loading, execution, cheapThresholdP, peakThresholdP }: TodayPlanWidgetProps) {
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
    // Measured base load (consumption − daikin − appliance), aligned to these
    // slots by slot_utc — the actual to compare against the forecast `load`.
    const execBase = new Map<string, number>();
    for (const e of execution?.slots ?? []) {
      if (e.slot_utc && e.base_load_kwh_est != null) execBase.set(e.slot_utc, e.base_load_kwh_est);
    }
    const loadActual = slots.map((s) => {
      const v = execBase.get(s.slot_utc);
      return v == null ? null : round2(v);
    });
    const price = slots.map((s) => (s.import_price_p == null ? null : s.import_price_p));

    // --- Rate-tier background bands. Classify each slot by import price into
    // negative / cheap / peak (standard → no shade), then shade contiguous
    // runs. Thresholds come from /metrics; if absent, fall back to this day's
    // own price distribution (33rd pct = cheap, 75th = peak).
    const known = price.filter((p): p is number => p != null).slice().sort((a, b) => a - b);
    const pct = (q: number) => (known.length ? known[Math.min(known.length - 1, Math.floor(q * known.length))] : null);
    const cheapAt = cheapThresholdP ?? pct(0.33);
    const peakAt = peakThresholdP ?? pct(0.75);
    // Every slot with a known price gets a tier — no blank gaps. Mid-priced
    // slots are "standard" (a neutral wash) rather than nothing, so the band
    // reads as a continuous tariff ribbon: paid / cheap / standard / peak.
    const tierOf = (p: number | null): Tier => {
      if (p == null) return null;
      if (p < 0) return "negative";
      if (cheapAt != null && p <= cheapAt) return "cheap";
      if (peakAt != null && p >= peakAt) return "peak";
      return "standard";
    };
    const tierColor = (k: Tier): string =>
      k === "negative" ? t.neg : k === "cheap" ? t.cheap : k === "peak" ? t.peak : t.textMute;
    // Negative ("paid to import") is the rare money-maker → the only strong band
    // (visible border, distinct blue). Cheap/peak are soft context washes;
    // standard is the faintest neutral so it fills the gap without greying the
    // plot. (The load line is greyed elsewhere, not by these bands.)
    const tierFill = (k: Tier): object =>
      k === "negative"
        ? { color: withAlpha(t.neg, 0.26), borderColor: withAlpha(t.neg, 0.9), borderWidth: 1 }
        : k === "standard"
        ? { color: withAlpha(t.textMute, 0.05) }
        : { color: withAlpha(tierColor(k), 0.10) };
    // markArea references slot INDEX (DST-safe; two slots can share a label).
    const bands: Array<[{ xAxis: number; itemStyle: object }, { xAxis: number }]> = [];
    let runStart = -1;
    let runTier: Tier = null;
    // Expand each run by half a category cell on both sides so a SINGLE-slot
    // tier (e.g. one 30-min negative window) still renders a full-width cell.
    // Without this, runStart === endIdx → a zero-width band → invisible, which
    // is why today's short negative windows weren't showing up.
    const flush = (endIdx: number) => {
      if (runStart < 0 || runTier == null) return;
      bands.push([
        { xAxis: runStart - 0.5, itemStyle: tierFill(runTier) },
        { xAxis: endIdx + 0.5 },
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

    // ONE projected-PV line: behind 'now' it's the committed plan we executed,
    // ahead it's the live forecast — but drawn in a single uniform thin dotted
    // light-yellow style so it reads as "the projection", with ACTUAL as the one
    // bold line. (Past vs future is still distinguished in the hover tooltip.)
    const mergeIdx = nowIdx; // -1 when 'now' is outside this day
    const planLine = slots.map((_, i) =>
      mergeIdx >= 0 && i > mergeIdx ? pvForecastLive[i] : (pvCommitted[i] ?? pvForecastLive[i]));

    chartRef.current.setOption({
      ...base,
      // Legend pinned bottom so it never collides with the top axis labels or
      // the plot (the caption-overlap fix).
      grid: { left: 16, right: 44, top: 16, bottom: 24, containLabel: true },
      // Legend removed — the lines are explained by the caption below the chart
      // (bold = actual, dotted = projection) + the tier-band legend, so the
      // floating legend over the plot was redundant clutter.
      legend: { show: false },
      tooltip: {
        ...(base.tooltip as object),
        formatter: (params: Array<{ dataIndex: number }>) => {
          const i = params[0]?.dataIndex ?? 0;
          const s = slots[i];
          if (!s) return "";
          const tier = tierOf(price[i]);
          const isPast = nowIdx >= 0 && i <= nowIdx;
          const planVal = isPast ? (pvCommitted[i] ?? pvForecastLive[i]) : pvForecastLive[i];
          // A mini horizontal bar so plan vs actual compare at a glance — the
          // "show the error graphically on hover" ask. All bars share one scale.
          const scale = Math.max(0.01, planVal ?? 0, pvActual[i] ?? 0, load[i] ?? 0, loadActual[i] ?? 0);
          const bar = (label: string, val: number | null, col: string, sub?: string) => {
            if (val == null || !Number.isFinite(val)) return "";
            const w = Math.round(Math.max(0, Math.min(1, val / scale)) * 78);
            return `<div style="display:flex;align-items:center;gap:6px;margin-top:3px;">` +
              `<span style="width:62px;color:${t.textMute};font-size:11px;">${label}</span>` +
              `<span style="display:inline-block;width:${w}px;height:7px;border-radius:3px;background:${col};"></span>` +
              `<span style="font-size:11px;color:${t.text};">${val.toFixed(2)}${sub || ""}</span></div>`;
          };
          // Miss readout (actual − committed plan), coloured by direction.
          let missRow = "";
          if (isPast && pvActual[i] != null && pvCommitted[i] != null) {
            const miss = pvActual[i]! - pvCommitted[i]!;
            const col = miss >= 0 ? t.cheap : t.importColor;
            missRow = `<div style="margin-top:4px;font-size:11px;color:${col};">` +
              `solar ${miss >= 0 ? "beat plan by +" : "fell short −"}${Math.abs(miss).toFixed(2)} kWh</div>`;
          }
          const head = `<strong>${labels[i]}</strong>${tier ? ` · ${tier}` : ""}` +
            (price[i] != null ? ` · ${price[i]!.toFixed(1)}p/kWh` : "");
          return head +
            bar(isPast ? "PV plan" : "PV forecast", planVal, withAlpha(t.pv, 0.55)) +
            bar("PV actual", pvActual[i], t.pv) +
            missRow +
            (load[i] != null || loadActual[i] != null
              ? `<div style="margin-top:5px;border-top:1px solid ${withAlpha(t.textMute, 0.25)};padding-top:2px;"></div>` : "") +
            bar("Load fcast", load[i], withAlpha(t.textMute, 0.5)) +
            bar("Load actual", loadActual[i], withAlpha(t.textMute, 0.9));
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
        // PV plan/forecast — one thin dotted light-yellow projection line.
        {
          name: "PV plan", type: "line", smooth: true, showSymbol: false, connectNulls: false,
          color: withAlpha(t.pv, 0.5),
          data: planLine, lineStyle: { color: withAlpha(t.pv, 0.5), width: 1, type: "dotted" }, z: 3,
        },
        // PV actual — the ONE bold line: vivid PV colour, thick, gradient fill.
        {
          name: "PV actual", type: "line", smooth: true, showSymbol: false, connectNulls: false, color: t.pv,
          data: pvActual, lineStyle: { color: t.pv, width: 3 },
          areaStyle: { color: areaGradient(t.pv, 0.46, 0.05) }, z: 4,
        },
        // Load — neutral GREY (doesn't clash with PV yellow / price red).
        // Forecast = dim dashed; ACTUAL (measured base load) = solid, mirroring
        // the PV actual-vs-plan treatment so load reads the same way.
        {
          name: "Load forecast", type: "line", smooth: true, showSymbol: false, color: withAlpha(t.textMute, 0.55),
          data: load, lineStyle: { color: withAlpha(t.textMute, 0.45), width: 1.25, type: "dashed" }, z: 2,
        },
        {
          name: "Load actual", type: "line", smooth: true, showSymbol: false, connectNulls: false,
          color: withAlpha(t.textMute, 0.95),
          data: loadActual, lineStyle: { color: withAlpha(t.textMute, 0.9), width: 1.75 }, z: 3,
        },
        // Import price → dashed step (reads as a reference, not a hard line).
        {
          name: "Import price", type: "line", step: "middle", showSymbol: false, color: t.importColor,
          yAxisIndex: 1, data: price, lineStyle: { color: t.importColor, width: 1.5, opacity: 0.8, type: "dashed" }, z: 1,
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
  }, [pv, execution, theme, cheapThresholdP, peakThresholdP]);

  const acc = pv?.accuracy;
  // Forecast for elapsed slots isn't persisted yet (#462), so by evening the
  // "expected by now" baseline collapses toward 0 — which would make any real
  // generation read as a huge bogus "above forecast". Detect that and show an
  // honest "comparison unavailable" instead of a misleading number.
  const forecastMissing = acc != null && acc.forecast_kwh < 0.1 && acc.actual_kwh >= 0.1;
  // Plain-language accuracy: did solar come in above or below the forecast so
  // far, and by how much. The per-slot detail (miss bar) is in the chart hover.
  const biasWord = acc == null ? "" :
    forecastMissing ? "forecast comparison unavailable"
    : Math.abs(acc.bias_kwh) < 0.1 ? "tracking forecast"
    : acc.bias_kwh > 0 ? `${acc.bias_kwh.toFixed(1)} kWh above forecast`
    : `${Math.abs(acc.bias_kwh).toFixed(1)} kWh below forecast`;
  const biasTone = acc == null || forecastMissing ? "" : Math.abs(acc.bias_kwh) < 0.1 ? "" : acc.bias_kwh > 0 ? " today-plan-acc--pos" : " today-plan-acc--neg";

  // Headline day total = solar ALREADY generated (actual, locked) + forecast for
  // the slots still to come. Use actual ONLY for fully-elapsed slots; the current
  // in-progress slot has just a partial actual (lower than its full-slot forecast),
  // so counting that would make the headline DIP at every slot boundary — use the
  // slot's forecast there instead. The realised part can't change, so only the
  // shrinking remainder moves: steady, and the right magnitude.
  const dayTotalNowMs = pv?.now_utc ? new Date(pv.now_utc).getTime() : Date.now();
  const dayTotal = pv?.slots?.length
    ? pv.slots.reduce((sum, s) => {
        const elapsed = new Date(s.slot_utc).getTime() + 30 * 60_000 <= dayTotalNowMs;
        const v = elapsed ? (s.pv_actual_kwh ?? s.pv_forecast_kwh) : s.pv_forecast_kwh;
        return sum + (v ?? 0);
      }, 0)
    : (pv?.forecast_kwh_day_total ?? null);

  return (
    <div class="today-plan">
      {dayTotal != null && (
        <div class="today-plan-summary">
          <span class="today-plan-summary-icon" aria-hidden="true">☀</span>
          <span class="today-plan-summary-value">{dayTotal.toFixed(1)}<span class="today-plan-summary-unit"> kWh</span></span>
          <span class="today-plan-summary-label">solar expected today (generated + forecast)</span>
        </div>
      )}
      {acc && (
        <div class="today-plan-acc">
          {forecastMissing ? (
            <span>Solar so far today: <strong>{acc.actual_kwh.toFixed(1)}</strong> kWh generated</span>
          ) : (
            <span>Solar so far today: <strong>{acc.actual_kwh.toFixed(1)}</strong> kWh vs <strong>{acc.forecast_kwh.toFixed(1)}</strong> expected by now</span>
          )}
          <span class="today-plan-acc-sep">·</span>
          <span
            class={biasTone.trim()}
            title={forecastMissing
              ? "The forecast for slots earlier today isn't persisted yet (#462), so there's no baseline to compare against this far into the day."
              : "Actual vs the forecast for the slots elapsed so far (not the full-day total in the card header). Hover any slot for its miss."}
          >
            {biasWord}
          </span>
        </div>
      )}
      <div ref={ref} style={{ width: "100%", height: "320px" }} />
      {pv?.slots?.length ? (
        <div class="today-plan-legend" role="note" aria-label="Chart legend">
          <span class="tpl-grp">
            <strong>Solar</strong>
            <span class="tpl-tok"><span class="tpl-line tpl-line--actual" aria-hidden="true" /> actual</span>
            <span class="tpl-tok"><span class="tpl-line tpl-line--plan" aria-hidden="true" /> plan</span>
          </span>
          <span class="tpl-grp">
            <strong>Tariff</strong>
            <span class="tpl-tok"><span class="tpl-sw tpl-sw--neg" aria-hidden="true" /> paid</span>
            <span class="tpl-tok"><span class="tpl-sw tpl-sw--cheap" aria-hidden="true" /> cheap</span>
            <span class="tpl-tok"><span class="tpl-sw tpl-sw--peak" aria-hidden="true" /> peak</span>
          </span>
          <span class="tpl-hint">◉ now · hover a slot for detail</span>
        </div>
      ) : null}
      {!pv?.slots?.length && !loading && <p class="muted">No plan data for today yet.</p>}
    </div>
  );
}

function round2(n: number): number {
  return Math.round(n * 100) / 100;
}
