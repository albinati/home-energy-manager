import { useEffect, useRef, useState } from "preact/hooks";
import { getEnergyPeriod, getDaikinConsumption } from "../../lib/endpoints";
import { usePeriod, setGranularity, selectedPeriod } from "../../lib/period";
import { makeChart, baseOption, chartTheme, barGradient, areaGradient, withAlpha, type EChartsType } from "../../lib/charts";
import { Icon } from "../common/Icon";
import type {
  PeriodInsightsResponse,
  PeriodChartPoint,
  ExecutionTodayResponse,
  ExecutionSlot,
  PvTodayResponse,
  DaikinConsumptionResponse,
} from "../../lib/types";
import "./energy-chart.css";

type Granularity = "day" | "week" | "month" | "year";

// Local-date ISO (matches lib/period.ts) — avoids the UTC drift that
// `toISOString().slice(0,10)` causes near the day boundary.
function localTodayISO(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

interface EnergyChartWidgetProps {
  // Execution slots used for the *day* view (30-min granularity) — same
  // source as the Today's bill widget so we don't duplicate the fetch.
  execution: ExecutionTodayResponse | null;
  // /pv/today — supplies the per-slot LOAD FORECAST (base_load_kwh) and price
  // for the day-view forecast-vs-actual comparison + tariff bands.
  pv?: PvTodayResponse | null;
}

// LOAD DETAILS chart — household demand only (no grid import/export/solar; the
// flow/source-sink view moved to Insights). Two modes:
//   * Today → 30-min forecast-vs-actual for the household (residual) load:
//     the LP's load forecast (base_load_kwh from /pv/today) vs the measured
//     residual (consumption − Daikin), mirroring the Today's-plan treatment,
//     with the Daikin (heat-pump) actual as a context line + tariff bands.
//   * Week / Month / Year → /energy/period: Load (total demand) + the two
//     Daikin slices (heating / tank) over time. Actual only — per-day forecast
//     history isn't captured yet (#424).
//
// Drill-down: clicking a label in year view → month; in week/month, only
// clicking *today's* label drills to day (historical day requires #424).
export function EnergyChartWidget({ execution, pv }: EnergyChartWidgetProps) {
  // Granularity + anchor come from the shared period navigator so the chart
  // re-scopes together with the Hero + cost breakdown.
  const { gran: granularity, anchor } = usePeriod();
  // Local "today" (matches period.ts) — the per-slot day chart only has data
  // for today (historical per-slot capture is #424).
  const todayLocalISO = localTodayISO();
  const isToday = anchor === todayLocalISO;
  const [period, setPeriod] = useState<PeriodInsightsResponse | null>(null);
  const [daikin, setDaikin] = useState<DaikinConsumptionResponse | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const elRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<EChartsType | null>(null);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError(null);
    const opts: { date?: string; month?: string; year?: number } = {};
    if (granularity === "week") opts.date = anchor;
    else if (granularity === "month") opts.month = anchor.slice(0, 7);
    else if (granularity === "year") opts.year = Number(anchor.slice(0, 4));
    else opts.date = anchor;

    // Today's day view comes from per-slot execution; a past day still fetches
    // /energy/period so we can at least render its (correct) daily totals.
    const periodPromise = granularity === "day" && isToday
      ? Promise.resolve(null)
      : getEnergyPeriod(granularity, opts).catch(() => null);
    const daikinPromise = getDaikinConsumption(granularity, opts).catch(() => null);

    Promise.all([periodPromise, daikinPromise]).then(([p, d]) => {
      if (!alive) return;
      setPeriod(p);
      setDaikin(d);
      setLoading(false);
    });
    return () => { alive = false; };
  }, [granularity, anchor]);

  useEffect(() => {
    if (!elRef.current) return;
    if (!chartRef.current) chartRef.current = makeChart(elRef.current);
    const chart = chartRef.current;
    const option = granularity === "day" && isToday
      ? optionForDay(execution, pv ?? null, daikin)
      : optionForPeriod(period, daikin, granularity);
    chart.setOption(option, true);
    const ro = new ResizeObserver(() => chart.resize());
    ro.observe(elRef.current);
    return () => ro.disconnect();
  }, [granularity, period, daikin, execution, pv, isToday]);

  useEffect(() => () => {
    if (chartRef.current) {
      chartRef.current.dispose();
      chartRef.current = null;
    }
  }, []);

  useEffect(() => {
    const c = chartRef.current;
    if (!c) return;
    const handler = (params: { name?: string }) => {
      const lbl = params?.name;
      if (!lbl) return;
      if (granularity === "week" || granularity === "month") {
        const today = new Date().toISOString().slice(0, 10);
        if (lbl === today) setGranularity("day");
      } else if (granularity === "year") {
        if (/^\d{4}-\d{2}$/.test(lbl)) {
          // Drill from a year bar into that month (set both gran + anchor).
          selectedPeriod.value = { gran: "month", anchor: `${lbl}-01` };
        }
      }
    };
    c.on("click", handler);
    return () => { c.off("click", handler); };
  }, [granularity]);

  const dayHasSlots = granularity === "day" && isToday && !!execution?.slots?.length;
  const isPastDay = granularity === "day" && !isToday;
  // Foot summary — LOAD totals only (grid/solar moved to Insights).
  const summary = period ? { load: period.energy.load_kwh } : null;
  const daikinTotals = daikin?.totals;
  const todayTotals = granularity === "day" && isToday && execution?.totals
    ? {
        load: execution.totals.load_kwh ?? null,
        residual: execution.totals.residual_kwh_est ?? null,
      }
    : null;

  return (
    <div class="echart">
      <div class="echart-toolbar">
        <span class="echart-id-icon"><Icon name={granularity === "day" ? "schedule" : "chart-bars"} size={18} /></span>
        {/* Granularity + stepping live in the shared PeriodNavigator at the top
            of the page; this just labels what the chart is currently showing. */}
        <span class="echart-label">
          {period?.period_label ?? (granularity === "day" ? (isToday ? "Today" : anchor) : "")}
        </span>
      </div>

      {/* Host wrap — loading state overlays (position:absolute) so it never
          displaces the chart, killing load-jump. */}
      <div class="echart-host-wrap">
        <div class="echart-host" ref={elRef} aria-label="Load details chart" />
        {loading && <div class="echart-state">Loading…</div>}
        {error && <div class="echart-state echart-state--err">{error}</div>}
      </div>

      {granularity === "day" && isToday && !dayHasSlots && (
        <div class="echart-flag">
          <span class="echart-flag-icon"><Icon name="schedule" size={14} /></span>
          No execution data for today yet — switch to Week.
        </div>
      )}
      {isPastDay && (
        <div class="echart-flag">
          <span class="echart-flag-icon"><Icon name="schedule" size={14} /></span>
          Daily totals shown — per-slot history for past days isn't captured yet (#424).
        </div>
      )}
      {granularity === "day" && dayHasSlots && (
        <div class="echart-flag">
          <span class="echart-flag-icon"><Icon name="schedule" size={14} /></span>
          Household load: <strong>forecast</strong> (dashed) vs <strong>actual</strong>
          {" "}(solid) — residual demand, i.e. consumption minus the heat pump.
          Daikin shown as a context line; tariff tier shades the background.
        </div>
      )}

      {summary && granularity !== "day" && (
        <div class="echart-foot">
          <span class="echart-foot-grp">
            <strong>Load</strong>&nbsp;
            <span class="echart-tok echart-tok-load">{fmt(summary.load)} total demand</span>
          </span>
          {daikinTotals && (daikinTotals.kwh_total || 0) > 0 && (
            <span class="echart-foot-grp">
              <strong>Daikin</strong>&nbsp;
              <span class="echart-tok echart-tok-daikin">{fmt(daikinTotals.kwh_total)} total</span>
              {daikinTotals.kwh_heating > 0 && (
                <> · <span class="echart-tok-mute">{fmt(daikinTotals.kwh_heating)} heating</span></>
              )}
              {daikinTotals.kwh_dhw > 0 && (
                <> · <span class="echart-tok-mute">{fmt(daikinTotals.kwh_dhw)} tank</span></>
              )}
            </span>
          )}
        </div>
      )}
      {todayTotals && (
        <div class="echart-foot">
          <span class="echart-foot-grp">
            <strong>Today so far</strong>&nbsp;
            {todayTotals.residual != null && <span class="echart-tok echart-tok-resid">{fmt(todayTotals.residual)} residual load</span>}
            {todayTotals.load != null && (
              <>{" · "}<span class="echart-tok echart-tok-load">{fmt(todayTotals.load)} total demand</span></>
            )}
            {daikinTotals && (daikinTotals.kwh_total || 0) > 0 && (
              <>
                {" · "}<span class="echart-tok echart-tok-daikin">{fmt(daikinTotals.kwh_total)} Daikin</span>
                {daikinTotals.kwh_heating > 0 && <span class="echart-tok-mute"> ({fmt(daikinTotals.kwh_heating)} heat)</span>}
                {daikinTotals.kwh_dhw > 0 && <span class="echart-tok-mute"> ({fmt(daikinTotals.kwh_dhw)} tank)</span>}
              </>
            )}
          </span>
        </div>
      )}
    </div>
  );
}

function fmt(v: number | null | undefined): string {
  if (v == null) return "—";
  return `${v.toFixed(v >= 100 ? 0 : 1)} kWh`;
}

function optionForPeriod(
  period: PeriodInsightsResponse | null,
  daikin: DaikinConsumptionResponse | null,
  gran: Granularity,
): Record<string, unknown> {
  const t = chartTheme();
  const base = baseOption();
  const points: PeriodChartPoint[] = period?.chart_data ?? [];
  const labels = points.map((p) => formatPointLabel(p.date, gran));

  // Match Daikin buckets to period chart points by date prefix. For year
  // view both use YYYY-MM; for month/week both use YYYY-MM-DD.
  const dKey = (when: string) => gran === "year" ? when.slice(0, 7) : when.slice(0, 10);
  const daikinByKey = new Map<string, { total: number; heat: number; dhw: number }>();
  (daikin?.buckets ?? []).forEach((b) => {
    daikinByKey.set(dKey(b.when), {
      total: b.kwh_total ?? 0,
      heat: b.kwh_heating ?? 0,
      dhw: b.kwh_dhw ?? 0,
    });
  });
  // Two sub-lines (heating + DHW) instead of a single combined Daikin line.
  // User asked for the breakdown to be visible everywhere, not just on hover.
  const daikinHeatLine = points.map((p) => {
    const k = gran === "year" ? p.date.slice(0, 7) : p.date.slice(0, 10);
    const d = daikinByKey.get(k);
    return d ? round1(d.heat) : null;
  });
  const daikinDhwLine = points.map((p) => {
    const k = gran === "year" ? p.date.slice(0, 7) : p.date.slice(0, 10);
    const d = daikinByKey.get(k);
    return d ? round1(d.dhw) : null;
  });

  return {
    ...base,
    legend: {
      ...(base.legend as object),
      // Load details: total demand as the bar, with the two Daikin (heat-pump)
      // slices overlaid. Grid import/export + solar live on the Insights tab.
      data: ["Load", "Daikin heating", "Daikin tank"],
    },
    xAxis: { ...(base.xAxis as object), data: labels },
    yAxis: [{ ...(base.yAxis as object), name: "kWh", nameTextStyle: { color: t.textMute, fontSize: 10 } }],
    series: [
      {
        ...seriesBar("Load", points.map((p) => round1(p.load_kwh)), t.house, "load"),
        itemStyle: { color: barGradient(t.house, 0.85, 0.4), borderRadius: [4, 4, 0, 0] },
      },
      // Daikin overlays — quiet secondary lines (dashed = heating, sparse
      // dotted = DHW estimate). No fills; they ride above the bar.
      {
        name: "Daikin heating",
        type: "line",
        data: daikinHeatLine,
        smooth: 0.3,
        symbol: "none",
        z: 11,
        lineStyle: { color: t.warn, width: 1.5, type: [4, 4], opacity: 0.9, cap: "round" },
        emphasis: { focus: "series" },
      },
      {
        name: "Daikin tank",
        type: "line",
        data: daikinDhwLine,
        smooth: 0.3,
        symbol: "none",
        z: 11,
        lineStyle: { color: t.pv, width: 1.5, type: [1, 5], opacity: 0.85, cap: "round" },
        emphasis: { focus: "series" },
      },
    ],
  };
}

function seriesBar(name: string, data: number[], color: string, stack: string, sink = false) {
  // Rounded + gradient bars. Sources (above zero) round their top; sinks
  // (below zero) round their bottom. Gradient fades top→bottom per domain.
  return {
    name,
    type: "bar",
    stack,
    data,
    itemStyle: {
      color: barGradient(color, sink ? 0.55 : 0.95, sink ? 0.25 : 0.45),
      borderRadius: sink ? [0, 0, 4, 4] : [4, 4, 0, 0],
    },
    emphasis: { focus: "series", itemStyle: { opacity: 1 } },
    barCategoryGap: "38%",
    universalTransition: { enabled: true },
    animationDelay: (idx: number) => idx * 8,
  };
}

type DayTier = "negative" | "cheap" | "standard" | "peak" | null;

// Day view = household LOAD forecast vs actual (residual = consumption − heat
// pump), mirroring the Today's-plan line treatment: forecast dashed, actual
// solid + fill. Daikin (heat-pump) actual rides as a quiet context line; the
// tariff tier shades the background. No grid/solar series — this is load only.
function optionForDay(
  exec: ExecutionTodayResponse | null,
  pv: PvTodayResponse | null,
  _daikinConsumption: DaikinConsumptionResponse | null,
): Record<string, unknown> {
  const t = chartTheme();
  const base = baseOption();

  // Axis: the full day from /pv/today (server-aligned, all 48 slots). Fall back
  // to the execution slots when pv is missing so the view still renders.
  const pvSlots = pv?.slots ?? [];
  const axis: string[] = pvSlots.length
    ? pvSlots.map((s) => s.slot_utc)
    : (exec?.slots ?? [])
        .slice()
        .sort((a, b) => (a.slot_utc ?? "").localeCompare(b.slot_utc ?? ""))
        .map((s) => s.slot_utc ?? "");
  const labels = axis.map((iso) => formatSlotLabel(iso));

  const execBy = new Map<string, ExecutionSlot>();
  for (const e of exec?.slots ?? []) if (e.slot_utc) execBy.set(e.slot_utc, e);
  const fcastBy = new Map<string, number | null>();
  const priceBy = new Map<string, number | null>();
  for (const s of pvSlots) {
    fcastBy.set(s.slot_utc, s.base_load_kwh ?? null);
    priceBy.set(s.slot_utc, s.import_price_p ?? null);
  }

  // Forecast residual household load (LP's load forecast, excludes heat pump).
  const loadForecast = axis.map((iso) => {
    const v = fcastBy.get(iso);
    return v == null ? null : round2(v);
  });
  // Measured residual load = consumption − Daikin. Prefer residual_kwh; else
  // base+appliance estimate; else consumption − daikin estimate.
  const loadActual = axis.map((iso) => {
    const e = execBy.get(iso);
    if (!e) return null;
    if (e.residual_kwh != null) return round2(e.residual_kwh);
    if (e.base_load_kwh_est != null) return round2((e.base_load_kwh_est ?? 0) + (e.appliance_kwh_est ?? 0));
    if (e.consumption_kwh != null) return round2(Math.max(0, e.consumption_kwh - (e.daikin_kwh_est ?? 0)));
    return null;
  });
  // Daikin (heat-pump) actual — context only, not part of the forecast pair.
  const daikinActual = axis.map((iso) => {
    const e = execBy.get(iso);
    return e?.daikin_kwh_est == null ? null : round2(e.daikin_kwh_est);
  });
  const price = axis.map((iso) => priceBy.get(iso) ?? null);

  // --- Tariff-tier background bands (paid / cheap / peak) — same context wash
  // as the Today's-plan chart. Mid-priced slots get a faint neutral fill.
  const known = price.filter((p): p is number => p != null).slice().sort((a, b) => a - b);
  const pct = (q: number) => (known.length ? known[Math.min(known.length - 1, Math.floor(q * known.length))] : null);
  const cheapAt = pct(0.33);
  const peakAt = pct(0.75);
  const tierOf = (p: number | null): DayTier => {
    if (p == null) return null;
    if (p < 0) return "negative";
    if (cheapAt != null && p <= cheapAt) return "cheap";
    if (peakAt != null && p >= peakAt) return "peak";
    return "standard";
  };
  const tierColor = (k: DayTier): string =>
    k === "negative" ? t.neg : k === "cheap" ? t.cheap : k === "peak" ? t.peak : t.textMute;
  const tierFill = (k: DayTier): object =>
    k === "negative"
      ? { color: withAlpha(t.neg, 0.26), borderColor: withAlpha(t.neg, 0.9), borderWidth: 1 }
      : k === "standard"
        ? { color: withAlpha(t.textMute, 0.05) }
        : { color: withAlpha(tierColor(k), 0.10) };
  const bands: Array<[{ xAxis: number; itemStyle: object }, { xAxis: number }]> = [];
  let runStart = -1;
  let runTier: DayTier = null;
  const flush = (endIdx: number) => {
    if (runStart < 0 || runTier == null) return;
    bands.push([{ xAxis: runStart - 0.5, itemStyle: tierFill(runTier) }, { xAxis: endIdx + 0.5 }]);
  };
  axis.forEach((_, i) => {
    const cur = tierOf(price[i]);
    if (cur !== runTier) {
      if (runTier != null) flush(i - 1);
      runTier = cur;
      runStart = cur != null ? i : -1;
    }
  });
  if (runTier != null) flush(axis.length - 1);

  // "Now" marker.
  const nowMs = pv?.now_utc ? new Date(pv.now_utc).getTime() : Date.now();
  let nowIdx = -1;
  if (axis.length) {
    const firstMs = new Date(axis[0]).getTime();
    const lastMs = new Date(axis[axis.length - 1]).getTime() + 30 * 60_000;
    if (nowMs >= firstMs && nowMs < lastMs) {
      const idx = axis.findIndex((iso) => new Date(iso).getTime() > nowMs);
      nowIdx = idx <= 0 ? axis.length - 1 : idx - 1;
    }
  }

  return {
    ...base,
    legend: { ...(base.legend as object), data: ["Load forecast", "Load actual", "Daikin"] },
    tooltip: {
      ...(base.tooltip as object),
      formatter: (params: Array<{ dataIndex: number }>) => {
        const i = params[0]?.dataIndex ?? 0;
        const tier = tierOf(price[i]);
        const scale = Math.max(0.01, loadForecast[i] ?? 0, loadActual[i] ?? 0, daikinActual[i] ?? 0);
        const bar = (label: string, val: number | null, col: string) => {
          if (val == null || !Number.isFinite(val)) return "";
          const w = Math.round(Math.max(0, Math.min(1, val / scale)) * 78);
          return `<div style="display:flex;align-items:center;gap:6px;margin-top:3px;">` +
            `<span style="width:74px;color:${t.textMute};font-size:11px;">${label}</span>` +
            `<span style="display:inline-block;width:${w}px;height:7px;border-radius:3px;background:${col};"></span>` +
            `<span style="font-size:11px;color:${t.text};">${val.toFixed(2)}</span></div>`;
        };
        let missRow = "";
        if (loadActual[i] != null && loadForecast[i] != null) {
          const miss = loadActual[i]! - loadForecast[i]!;
          const col = miss <= 0 ? t.cheap : t.importColor;
          missRow = `<div style="margin-top:4px;font-size:11px;color:${col};">` +
            `load ${miss <= 0 ? "under forecast −" : "over forecast +"}${Math.abs(miss).toFixed(2)} kWh</div>`;
        }
        const head = `<strong>${labels[i]}</strong>${tier ? ` · ${tier}` : ""}` +
          (price[i] != null ? ` · ${price[i]!.toFixed(1)}p/kWh` : "");
        return head +
          bar("Load forecast", loadForecast[i], withAlpha(t.textMute, 0.5)) +
          bar("Load actual", loadActual[i], withAlpha(t.textMute, 0.95)) +
          missRow +
          (daikinActual[i] != null
            ? `<div style="margin-top:5px;border-top:1px solid ${withAlpha(t.textMute, 0.25)};padding-top:2px;"></div>` +
              bar("Daikin (heat)", daikinActual[i], t.warn)
            : "");
      },
    },
    xAxis: { ...(base.xAxis as object), data: labels, axisLabel: { color: t.textMute, fontSize: 10, interval: 5 } },
    yAxis: [
      { ...(base.yAxis as object), name: "kWh", position: "left" },
      {
        ...(base.yAxis as object),
        name: "p",
        position: "right",
        splitLine: { show: false },
        axisLabel: { color: t.textMute, fontSize: 11, formatter: "{value}p" },
      },
    ],
    series: [
      // Tariff bands + now marker on a silent baseline series.
      {
        name: "_bands", type: "line", data: axis.map(() => null), silent: true,
        markArea: bands.length ? { silent: true, data: bands } : undefined,
        markLine: nowIdx >= 0 ? {
          silent: true, symbol: "none",
          lineStyle: { color: t.text, width: 1.5, type: "solid", opacity: 0.5 },
          label: { show: false }, data: [{ xAxis: nowIdx }],
        } : undefined,
        z: 0,
      },
      // Load forecast — dim dashed line (the LP's prediction).
      {
        name: "Load forecast", type: "line", smooth: true, showSymbol: false,
        data: loadForecast,
        lineStyle: { color: withAlpha(t.textMute, 0.5), width: 1.5, type: "dashed", cap: "round" }, z: 2,
      },
      // Load actual — the ONE bold line: solid grey + gradient fill.
      {
        name: "Load actual", type: "line", smooth: true, showSymbol: false, connectNulls: false,
        data: loadActual,
        lineStyle: { color: withAlpha(t.house, 0.95), width: 2.75, cap: "round" },
        areaStyle: { color: areaGradient(t.house, 0.28, 0.02) }, z: 4,
      },
      // Daikin (heat-pump) actual — quiet amber context line.
      {
        name: "Daikin", type: "line", smooth: true, showSymbol: false, connectNulls: false,
        data: daikinActual,
        lineStyle: { color: withAlpha(t.warn, 0.85), width: 1.25, type: [4, 4], cap: "round" }, z: 3,
      },
      // Import price → dashed step on the right axis (reference, not a hard line).
      {
        name: "Import price", type: "line", step: "middle", showSymbol: false,
        yAxisIndex: 1, data: price,
        lineStyle: { color: t.importColor, width: 1.25, opacity: 0.7, type: "dashed" }, z: 1,
      },
      // Pulsing "now".
      ...(nowIdx >= 0 ? [{
        name: "_now", type: "effectScatter", silent: true,
        coordinateSystem: "cartesian2d", symbolSize: 9, z: 6, showEffectOn: "render",
        rippleEffect: { period: 2.4, scale: 3.0, brushType: "stroke" },
        itemStyle: { color: t.accent, shadowBlur: 8, shadowColor: t.accent },
        data: [[nowIdx, 0]],
      }] : []),
    ],
  };
}

function round1(v: number | null | undefined): number { return Math.round((v ?? 0) * 10) / 10; }
function round2(v: number | null | undefined): number { return Math.round((v ?? 0) * 100) / 100; }

function formatPointLabel(iso: string, gran: Granularity): string {
  if (gran === "year") return iso.slice(0, 7);
  const d = new Date(iso + "T00:00:00");
  return `${String(d.getDate()).padStart(2, "0")}/${String(d.getMonth() + 1).padStart(2, "0")}`;
}

function formatSlotLabel(iso?: string): string {
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  } catch { return iso; }
}
