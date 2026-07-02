import { useEffect, useRef, useState } from "preact/hooks";
import { getEnergyPeriod, getDaikinConsumption, getGridToday, getExecutionToday, getPvToday, getForecastDaily } from "../../lib/endpoints";
import { usePeriod, setGranularity, selectedPeriod, isCurrentPeriod, periodDateRange } from "../../lib/period";
import { getImmutableCache, setImmutableCache } from "../../lib/poll";
import { makeChart, baseOption, chartTheme, barGradient, areaGradient, withAlpha, type EChartsType } from "../../lib/charts";
import { Icon } from "../common/Icon";
import { NowDot } from "../common/NowDot";
import type {
  PeriodInsightsResponse, ForecastDailyResponse,
  PeriodChartPoint,
  ExecutionTodayResponse,
  ExecutionSlot,
  PvTodayResponse,
  GridTodayResponse,
  DaikinConsumptionResponse,
} from "../../lib/types";
import "./energy-chart.css";

type Granularity = "day" | "week" | "month" | "year";

interface DayPastData {
  exec: ExecutionTodayResponse | null;
  pv: PvTodayResponse | null;
  grid: GridTodayResponse | null;
  daikin: DaikinConsumptionResponse | null;
}

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
  // /pv/today — supplies the per-slot committed LOAD FORECAST (load_forecast_kwh) and price
  // for the day-view forecast-vs-actual comparison + tariff bands.
  pv?: PvTodayResponse | null;
}

// LOAD DETAILS chart — household demand only (no grid import/export/solar; the
// flow/source-sink view moved to Insights). Two modes:
//   * Today → 30-min forecast-vs-actual for the household load: the LP's
//     committed TOTAL load forecast (load_forecast_kwh from /pv/today, base +
//     dhw + space) vs the measured total-demand stack — same units both sides
//     — plus the measured
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
  // Local "today" (matches period.ts).
  const todayLocalISO = localTodayISO();
  const isToday = anchor === todayLocalISO;
  // Yesterday's per-slot consumption_kwh is still "settling": the nightly
  // consumption backfill (~04:00 local) rewrites the noisy heartbeat estimate
  // with the metered reading. So only days STRICTLY older than yesterday are
  // safe to cache immutably; yesterday is refetched on each visit.
  const yesterdayLocalISO = (() => {
    const d = new Date();
    d.setDate(d.getDate() - 1);
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  })();
  const [period, setPeriod] = useState<PeriodInsightsResponse | null>(null);
  const [fcDaily, setFcDaily] = useState<ForecastDailyResponse | null>(null);
  const [daikin, setDaikin] = useState<DaikinConsumptionResponse | null>(null);
  // Day view: per-slot grid import + battery discharge — for the "by source" stack.
  const [grid, setGrid] = useState<GridTodayResponse | null>(null);
  // Past day: the same per-slot data the live "today" view uses, fetched for the
  // selected date (the today endpoints all accept ?date=). Lets a past day render
  // the real intraday chart instead of a single daily-total bar.
  const [dayData, setDayData] = useState<DayPastData | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const elRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<EChartsType | null>(null);

  useEffect(() => {
    let alive = true;
    setError(null);

    // "Past is past": a period that doesn't contain today never changes, so
    // serve it from the immutable cache (instant, no flash) and skip the fetch.
    const isPast = !isCurrentPeriod({ gran: granularity, anchor });
    const cacheKey = `energychart:${granularity}:${anchor}`;

    // PAST DAY → fetch the same per-slot data the live "today" view uses, for
    // that date (the today endpoints all accept ?date=), and render the real
    // intraday chart. Per-slot history IS captured (execution_log etc.), so the
    // old single-bar fallback (#424) is no longer needed.
    if (granularity === "day" && !isToday) {
      const settled = anchor < yesterdayLocalISO; // consumption backfill done
      const cachedDay = settled ? getImmutableCache<DayPastData>(cacheKey) : undefined;
      if (cachedDay) {
        setDayData(cachedDay);
        setLoading(false);
        return () => { alive = false; };
      }
      setLoading(true);
      setDayData(null);
      setPeriod(null);
      setDaikin(null);
      setGrid(null);
      Promise.all([
        getExecutionToday(anchor).catch(() => null),
        getPvToday(anchor).catch(() => null),
        getGridToday(anchor).catch(() => null),
        getDaikinConsumption("day", { date: anchor }).catch(() => null),
      ]).then(([e, p, g, dk]) => {
        if (!alive) return;
        const v: DayPastData = { exec: e, pv: p, grid: g, daikin: dk };
        setDayData(v);
        setLoading(false);
        if (settled) setImmutableCache(cacheKey, v); // only fully-settled past days
      });
      return () => { alive = false; };
    }

    // Non-past-day path uses the period/daikin state — clear any past-day data.
    setDayData(null);

    type Cached = {
      period: PeriodInsightsResponse | null;
      daikin: DaikinConsumptionResponse | null;
      grid: GridTodayResponse | null;
      fc: ForecastDailyResponse | null;
};
    const cached = isPast ? getImmutableCache<Cached>(cacheKey) : undefined;
    if (cached) {
      setPeriod(cached.period);
      setDaikin(cached.daikin);
      setGrid(cached.grid);
      setFcDaily(cached.fc ?? null);
      setLoading(false);
      return () => { alive = false; };
    }

    // Clear the previous period's data BEFORE the async fetch so the render
    // effect can't paint a stale chart while the new data is in flight (the
    // "chart doesn't update when navigating back" bug).
    setLoading(true);
    setPeriod(null);
    setDaikin(null);
    setGrid(null);
    setFcDaily(null);

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
    // Committed daily forecast overlay (#624) — week/month only; a year view's
    // partially-logged months would read as fake under-forecast.
    const range = periodDateRange({ gran: granularity, anchor });
    // `undefined` = fetch FAILED (transient) — distinct from null (not
    // requested) so a blip never gets frozen into the immutable cache as
    // "no forecast data for this period".
    const fcPromise: Promise<ForecastDailyResponse | null | undefined> =
      granularity === "week" || granularity === "month"
        ? getForecastDaily(range.start, range.end).catch(() => undefined)
        : Promise.resolve(null);
    // Grid import + battery discharge per slot for the day source-stack.
    const gridPromise = granularity === "day" && isToday
      ? getGridToday().catch(() => null)
      : Promise.resolve(null);

    Promise.all([periodPromise, daikinPromise, gridPromise, fcPromise]).then(([p, d, g, fc]) => {
      if (!alive) return;
      setPeriod(p);
      setDaikin(d);
      setGrid(g);
      setFcDaily(fc ?? null);
      setLoading(false);
      // Don't cache a bundle whose fc fetch FAILED — retry next visit instead
      // of silently losing the overlay for the whole session (review MED).
      if (isPast && fc !== undefined) setImmutableCache(cacheKey, { period: p, daikin: d, grid: g, fc });
    });
    return () => { alive = false; };
  }, [granularity, anchor]);

  useEffect(() => {
    if (!elRef.current) return;
    if (!chartRef.current) chartRef.current = makeChart(elRef.current);
    const chart = chartRef.current;
    const pastDayView = granularity === "day" && !isToday;
    const option = granularity === "day"
      ? optionForDay(
          pastDayView ? dayData?.exec ?? null : execution,
          pastDayView ? dayData?.pv ?? null : (pv ?? null),
          pastDayView ? dayData?.grid ?? null : grid,
        )
      : optionForPeriod(period, daikin, granularity, fcDaily);
    chart.setOption(option, true);
    // Resize handling now lives centrally in makeChart (rAF-debounced
    // ResizeObserver) — the per-effect observer this widget carried would
    // double every resize() call.
  }, [granularity, period, daikin, grid, fcDaily, dayData, execution, pv, isToday]);

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
        // LOCAL today — the bar labels are built from local dates, so a UTC date
        // here failed the match in the 00:00–01:00 BST window (drill-down dead).
        const today = localTodayISO();
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

  // Per-slot data for the day view — props for today, the date-fetched set for
  // a past day (so both render the same intraday chart).
  const pastDayView = granularity === "day" && !isToday;
  const effExec = pastDayView ? dayData?.exec ?? null : execution;
  const effDaikin = pastDayView ? dayData?.daikin ?? null : daikin;
  const dayHasSlots = granularity === "day" && !!effExec?.slots?.length;
  // Foot summary — LOAD totals only (grid/solar moved to Insights).
  const summary = period ? { load: period.energy.load_kwh } : null;
  const daikinTotals = effDaikin?.totals;
  const dayTotals = granularity === "day" && effExec?.totals
    ? {
        load: effExec.totals.load_kwh ?? null,
        residual: effExec.totals.residual_kwh_est ?? null,
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
        <div class="echart-host" ref={elRef} role="img" aria-label="Load details chart" />
        {loading && <div class="echart-state">Loading…</div>}
        {error && <div class="echart-state echart-state--err">{error}</div>}
      </div>

      {granularity === "day" && !dayHasSlots && !loading && (
        <div class="echart-flag">
          <span class="echart-flag-icon"><Icon name="schedule" size={14} /></span>
          {isToday ? "No execution data for today yet — switch to Week." : "No per-slot data recorded for this day."}
        </div>
      )}
      {granularity === "day" && dayHasSlots && (
        <div class="echart-legend2">
          <span class="echart-l2"><span class="echart-l2-sw" style="background:var(--house)" /> Base</span>
          <span class="echart-l2"><span class="echart-l2-sw" style="background:var(--accent)" /> Appliances</span>
          <span class="echart-l2"><span class="echart-l2-sw" style="background:var(--warn)" /> Heat pump</span>
          <span class="echart-l2"><span class="echart-l2-line" style="border-top-color:var(--import)" /> Grid</span>
          <span class="echart-l2"><span class="echart-l2-line" style="border-top-color:var(--batt)" /> Battery</span>
          <span class="echart-l2"><span class="echart-l2-line" style="border-top-color:var(--accent);border-top-style:dashed" /> SoC %</span>
          <span class="echart-l2-hint">stack = load by use · grid/battery = how it was sourced · SoC right · <NowDot /> now</span>
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
      {dayTotals && (
        <div class="echart-foot">
          <span class="echart-foot-grp">
            <strong>{isToday ? "Today so far" : "Total"}</strong>&nbsp;
            {dayTotals.residual != null && <span class="echart-tok echart-tok-resid">{fmt(dayTotals.residual)} residual load</span>}
            {dayTotals.load != null && (
              <>{" · "}<span class="echart-tok echart-tok-load">{fmt(dayTotals.load)} total demand</span></>
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
  fcDaily: ForecastDailyResponse | null,
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

  // Committed daily load forecast (#624) — dashed grey line over the bars,
  // same treatment as the day view's Forecast line. Days missing from
  // load_error_log (pre-logging, or today before the ~04:22 rebuild) stay
  // null — honest gap, no profile splice.
  const fcBy = new Map<string, number>();
  for (const d of fcDaily?.days ?? []) {
    if (d.load_forecast_kwh != null) fcBy.set(d.date, d.load_forecast_kwh);
  }
  const forecastLine = points.map((p) => {
    const v = fcBy.get(p.date.slice(0, 10));
    return v == null ? null : round1(v);
  });
  const hasForecast = forecastLine.some((v) => v != null);

  return {
    ...base,
    legend: {
      ...(base.legend as object),
      // Load details: total demand as the bar, with the two Daikin (heat-pump)
      // slices overlaid. Grid import/export + solar live on the Insights tab.
      data: ["Load", ...(hasForecast ? ["Forecast"] : []), "Daikin heating", "Daikin tank"],
    },
    xAxis: { ...(base.xAxis as object), data: labels },
    yAxis: [{ ...(base.yAxis as object), name: "kWh", nameTextStyle: { color: t.textMute, fontSize: 10 } }],
    series: [
      {
        ...seriesBar("Load", points.map((p) => round1(p.load_kwh)), t.house, "load"),
        itemStyle: { color: barGradient(t.house, 0.85, 0.4), borderRadius: [4, 4, 0, 0] },
      },
      ...(hasForecast ? [{
        name: "Forecast",
        type: "line",
        data: forecastLine,
        smooth: 0.3,
        symbol: "none",
        connectNulls: false,
        z: 12,
        lineStyle: { color: withAlpha(t.textMute, 0.85), width: 1.25, type: "dashed", cap: "round" },
        emphasis: { focus: "series" },
      }] : []),
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
  grid: GridTodayResponse | null,
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
  const priceBy = new Map<string, number | null>();
  for (const s of pvSlots) priceBy.set(s.slot_utc, s.import_price_p ?? null);
  // Grid import + battery discharge per slot, keyed by slot_utc (...Z form).
  const gridImpBy = new Map<string, number | null>();
  const battDisBy = new Map<string, number | null>();
  for (const gs of grid?.slots ?? []) {
    if (gs.slot_utc) { gridImpBy.set(gs.slot_utc, gs.import_actual_kwh); battDisBy.set(gs.slot_utc, gs.discharge_actual_kwh ?? null); }
  }

  // Consumption BY SOURCE — how the load was covered, slot by slot:
  //   solar self-use (free) + battery discharge + grid import = total load.
  // solar self-use is the residual (load − grid − battery), so the stack always
  // sums to the measured consumption. This shows whether the battery is pulling
  // its weight vs the grid; the SoC line shows if charge was left unused.
  const consumption = axis.map((iso) => {
    const e = execBy.get(iso);
    return e?.consumption_kwh != null ? round2(e.consumption_kwh) : null;
  });
  const gridImp = axis.map((iso) => { const v = gridImpBy.get(iso); return v == null ? null : round2(v); });
  const battDis = axis.map((iso) => { const v = battDisBy.get(iso); return v == null ? null : round2(v); });
  const solarSelf = axis.map((_iso, k) => {
    const c = consumption[k]; if (c == null) return null;
    return round2(Math.max(0, c - (gridImp[k] ?? 0) - (battDis[k] ?? 0)));
  });
  const soc = axis.map((iso) => { const e = execBy.get(iso); return e?.soc_percent ?? null; });
  const totalActual = consumption;
  const price = axis.map((iso) => priceBy.get(iso) ?? null);

  // Load BY USE — where the energy went: base (rest of house) + appliances +
  // heat pump. The filled stack (sums to total load) is the breakdown; grid +
  // battery ride over it as lines showing how that load was sourced.
  const baseActual = axis.map((iso) => {
    const e = execBy.get(iso);
    if (!e) return null;
    if (e.base_load_kwh_est != null) return round2(e.base_load_kwh_est);
    if (e.consumption_kwh != null) return round2(Math.max(0, e.consumption_kwh - (e.daikin_kwh_est ?? 0) - (e.appliance_kwh_est ?? 0)));
    return null;
  });
  const applianceActual = axis.map((iso) => { const e = execBy.get(iso); return e?.appliance_kwh_est == null ? null : round2(e.appliance_kwh_est); });
  const daikinActual = axis.map((iso) => { const e = execBy.get(iso); return e?.daikin_kwh_est == null ? null : round2(e.daikin_kwh_est); });
  const hasAppliance = applianceActual.some((v) => (v ?? 0) > 0);
  // Committed TOTAL household load (load_forecast_kwh, frozen at solve time)
  // so the dashed line is comparable to the total-demand stack below it.
  // Slots no solve covered stay null ON PURPOSE — falling back to the
  // residual profile would splice residual-only values into a total-load
  // line (a fake step at every coverage boundary); connectNulls:false
  // renders the gap honestly instead.
  const loadForecastBy = new Map<string, number>();
  for (const s of pvSlots) {
    if (s.slot_utc && s.load_forecast_kwh != null) loadForecastBy.set(s.slot_utc, s.load_forecast_kwh);
  }
  const loadForecast = axis.map((iso) => {
    const v = loadForecastBy.get(iso);
    return v == null ? null : round2(v);
  });

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

  const stackArea = (color: string, top: number) => ({
    opacity: 1, color: areaGradient(color, top, top * 0.55),
  });
  return {
    ...base,
    legend: { show: false },
    tooltip: {
      ...(base.tooltip as object),
      formatter: (params: Array<{ dataIndex: number }>) => {
        const i = params[0]?.dataIndex ?? 0;
        const tier = tierOf(price[i]);
        const scale = Math.max(0.01, totalActual[i] ?? 0);
        const bar = (label: string, val: number | null, col: string) => {
          if (val == null || !Number.isFinite(val) || val <= 0) return "";
          const w = Math.round(Math.max(0, Math.min(1, val / scale)) * 70);
          return `<div style="display:flex;align-items:center;gap:6px;margin-top:3px;">` +
            `<span style="width:70px;color:${t.textMute};font-size:11px;">${label}</span>` +
            `<span style="display:inline-block;width:${w}px;height:7px;border-radius:3px;background:${col};"></span>` +
            `<span style="font-size:11px;color:${t.text};">${val.toFixed(2)}</span></div>`;
        };
        const head = `<strong>${labels[i]}</strong>${tier ? ` · ${tier}` : ""}` +
          (price[i] != null ? ` · ${price[i]!.toFixed(1)}p/kWh` : "");
        const totalRow = totalActual[i] != null
          ? `<div style="margin-top:2px;font-size:11px;color:${t.text};">Load <strong>${totalActual[i]!.toFixed(2)} kWh</strong></div>` : "";
        const socRow = soc[i] != null
          ? `<div style="margin-top:3px;font-size:11px;color:${t.text};">Battery SoC <strong>${Math.round(soc[i]!)}%</strong></div>` : "";
        const div = `<div style="margin-top:4px;border-top:1px solid ${withAlpha(t.textMute, 0.25)};padding-top:2px;font-size:10px;color:${t.textMute};">sourced from</div>`;
        return head + totalRow +
          bar("Base", baseActual[i], t.house) +
          (hasAppliance ? bar("Appliances", applianceActual[i], t.accent) : "") +
          bar("Heat pump", daikinActual[i], t.warn) +
          div +
          bar("Solar", solarSelf[i], t.pv) +
          bar("Battery", battDis[i], t.batt) +
          bar("Grid", gridImp[i], t.importColor) +
          socRow;
      },
    },
    grid: { left: 16, right: 44, top: 16, bottom: 24, containLabel: true },
    xAxis: { ...(base.xAxis as object), data: labels, axisLabel: { color: t.textMute, fontSize: 10, interval: 5 } },
    yAxis: [
      { ...(base.yAxis as object), name: "kWh", position: "left" },
      // Right axis: battery SoC (%). Watching this against the source stack
      // shows whether the battery was used (SoC falling while it discharges) or
      // whether charge was left on the table (SoC high while the grid covers load).
      {
        ...(base.yAxis as object), position: "right", min: 0, max: 100, splitLine: { show: false },
        axisLabel: { color: t.textMute, fontSize: 10, formatter: "{value}%" },
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
      // Load BY USE — base (bottom) + appliances + heat pump, stacked filled
      // areas. The breakdown the user reads "where the energy went".
      {
        name: "Base", type: "line", stack: "load", smooth: true, showSymbol: false,
        data: baseActual, lineStyle: { width: 0 }, areaStyle: stackArea(t.house, 0.65), z: 2,
      },
      ...(hasAppliance ? [{
        name: "Appliances", type: "line", stack: "load", smooth: true, showSymbol: false,
        data: applianceActual, lineStyle: { width: 0 }, areaStyle: stackArea(t.accent, 0.65), z: 2,
      }] : []),
      {
        name: "Heat pump", type: "line", stack: "load", smooth: true, showSymbol: false,
        data: daikinActual, lineStyle: { width: 0 }, areaStyle: stackArea(t.warn, 0.7), z: 2,
      },
      // Forecast household demand — dashed grey line over the stack.
      {
        name: "Forecast", type: "line", smooth: true, showSymbol: false, connectNulls: false,
        data: loadForecast, lineStyle: { color: withAlpha(t.textMute, 0.85), width: 1.25, type: "dashed", cap: "round" }, z: 4,
      },
      // SOURCE overlay — how that load was covered: grid import + battery
      // discharge as thin stepped lines (kWh, left axis), riding over the stack.
      {
        name: "Grid", type: "line", step: "middle", showSymbol: false, connectNulls: false,
        data: gridImp, lineStyle: { color: t.importColor, width: 1.5, type: "solid", cap: "round" }, z: 5,
      },
      {
        name: "Battery", type: "line", step: "middle", showSymbol: false, connectNulls: false,
        data: battDis, lineStyle: { color: t.batt, width: 1.5, type: "solid", cap: "round" }, z: 5,
      },
      // Battery SoC → dashed line on the right axis (is there spare charge?).
      {
        name: "SoC", type: "line", smooth: true, showSymbol: false, connectNulls: true,
        yAxisIndex: 1, data: soc,
        lineStyle: { color: t.accent, width: 1.5, type: "dashed", cap: "round" }, z: 6,
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
