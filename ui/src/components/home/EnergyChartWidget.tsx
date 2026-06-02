import { useEffect, useRef, useState } from "preact/hooks";
import { getEnergyPeriod, getDaikinConsumption } from "../../lib/endpoints";
import { usePeriod, setGranularity, selectedPeriod } from "../../lib/period";
import { makeChart, baseOption, chartTheme, barGradient, areaGradient, type EChartsType } from "../../lib/charts";
import { Icon } from "../common/Icon";
import type {
  PeriodInsightsResponse,
  PeriodChartPoint,
  ExecutionTodayResponse,
  ExecutionSlot,
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
}

// Energy flow chart with proper source / sink split. House load is fed by
// three things: solar self-use, battery discharge, and grid import — the
// last is the only one that costs money, so cost figures derive from grid
// only (never load × price, since solar self-use is free).
//
// Granularity switcher:
//   * Today → 30-min from /execution/today, with Daikin actuals from the
//     Onecta consumption table mapped client-side to slots when present
//     (physics estimate is the fallback; it returns 0 above the
//     weather-curve cutoff — that's why the chart used to look empty).
//   * Week / Month / Year → /energy/period:
//       positive bars: Solar | Discharge | Grid Import   (sources)
//       negative bars: Charge | Grid Export              (sinks)
//       line:          Load                              (total demand)
//       overlay:       Daikin   (heating+DHW slice of load)
//
// Drill-down: clicking a label in year view → month; in week/month, only
// clicking *today's* label drills to day (historical day requires #424).
export function EnergyChartWidget({ execution }: EnergyChartWidgetProps) {
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
      ? optionForDay(execution, daikin)
      : optionForPeriod(period, daikin, granularity);
    chart.setOption(option, true);
    const ro = new ResizeObserver(() => chart.resize());
    ro.observe(elRef.current);
    return () => ro.disconnect();
  }, [granularity, period, daikin, execution, isToday]);

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
  // Detect which source dominates the Daikin buckets — surfaced in the flag
  // text so the user knows whether they're reading an Onecta integer or
  // a telemetry-integral decimal refinement.
  const sourceCounts = (daikin?.buckets ?? []).reduce(
    (acc, b) => {
      const s = b.source || "";
      if (s) acc[s] = (acc[s] ?? 0) + 1;
      return acc;
    },
    {} as Record<string, number>,
  );
  const daikinSourceLabel =
    sourceCounts["telemetry_integral"] && !sourceCounts["onecta_cache"]
      ? "telemetry-integral (sub-integer precision)"
      : sourceCounts["telemetry_integral"] && sourceCounts["onecta_cache"]
        ? "mixed Onecta integer + telemetry-integral decimals"
        : sourceCounts["onecta_cache"]
          ? "Onecta cache (integer-rounded)"
          : "physics estimate (no actuals yet)";

  // Foot summary — totals + the grid-only cost view.
  const summary = period
    ? {
        solar: period.energy.solar_kwh,
        load: period.energy.load_kwh,
        import: period.energy.import_kwh,
        export: period.energy.export_kwh,
        charge: period.energy.charge_kwh,
        discharge: period.energy.discharge_kwh,
        importCost: period.cost?.import_cost_pounds ?? 0,
        exportRevenue: period.cost?.export_earnings_pounds ?? 0,
        netCost: period.cost?.net_cost_pounds ?? 0,
      }
    : null;
  const daikinTotals = daikin?.totals;
  const todayTotals = granularity === "day" && execution?.totals
    ? {
        load: execution.totals.load_kwh ?? null,
        residual: execution.totals.residual_kwh_est ?? null,
        grid_cost_p: execution.totals.cost_realised_p ?? null,
        svt_cost_p: execution.totals.cost_svt_p ?? null,
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
        <div class="echart-host" ref={elRef} aria-label="Energy flow chart" />
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
          Day view: Daikin <strong>{daikinSourceLabel}</strong> / Residual stacked,
          + <strong>realised grid cost</strong>. Per-slot solar / import / export
          are not yet captured — see #424.
        </div>
      )}

      {summary && granularity !== "day" && (
        <div class="echart-foot">
          <span class="echart-foot-grp">
            <strong>Sources</strong>&nbsp;
            <span class="echart-tok echart-tok-solar">{fmt(summary.solar)} solar</span> ·
            <span class="echart-tok echart-tok-imp">{fmt(summary.import)} import</span>
          </span>
          <span class="echart-foot-grp">
            <strong>Sinks</strong>&nbsp;
            <span class="echart-tok echart-tok-load">{fmt(summary.load)} load</span> ·
            <span class="echart-tok echart-tok-exp">{fmt(summary.export)} export</span>
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
          <span class="echart-foot-grp">
            <strong>Grid £</strong>&nbsp;
            <span class="echart-tok echart-tok-cost">£{summary.importCost.toFixed(2)} paid</span> −
            <span class="echart-tok echart-tok-rev">£{summary.exportRevenue.toFixed(2)} earned</span> =
            <strong class={summary.netCost >= 0 ? "echart-tok-cost" : "echart-tok-rev"}>
              £{summary.netCost.toFixed(2)} net
            </strong>
          </span>
        </div>
      )}
      {todayTotals && (
        <div class="echart-foot">
          <span class="echart-foot-grp">
            <strong>Today so far</strong>&nbsp;
            {daikinTotals && (daikinTotals.kwh_total || 0) > 0 && (
              <>
                <span class="echart-tok echart-tok-daikin">{fmt(daikinTotals.kwh_total)} Daikin</span>
                {daikinTotals.kwh_heating > 0 && <span class="echart-tok-mute"> ({fmt(daikinTotals.kwh_heating)} heat)</span>}
                {daikinTotals.kwh_dhw > 0 && <span class="echart-tok-mute"> ({fmt(daikinTotals.kwh_dhw)} tank)</span>}
                {" · "}
              </>
            )}
            {todayTotals.residual != null && <span class="echart-tok echart-tok-resid">{fmt(todayTotals.residual)} residual</span>}
            {todayTotals.load != null && (
              <>{" · "}<span class="echart-tok echart-tok-load">{fmt(todayTotals.load)} total load</span></>
            )}
          </span>
          {todayTotals.grid_cost_p != null && (
            <span class="echart-foot-grp">
              <strong>Grid cost</strong>&nbsp;
              <span class="echart-tok echart-tok-cost">{pence(todayTotals.grid_cost_p)}</span>
              {todayTotals.svt_cost_p != null && (
                <> <span class="echart-tok-mute">(SVT would be {pence(todayTotals.svt_cost_p)})</span></>
              )}
            </span>
          )}
        </div>
      )}
    </div>
  );
}

function fmt(v: number | null | undefined): string {
  if (v == null) return "—";
  return `${v.toFixed(v >= 100 ? 0 : 1)} kWh`;
}

function pence(p: number | null | undefined): string {
  if (p == null) return "—";
  if (Math.abs(p) >= 100) return `£${(p / 100).toFixed(2)}`;
  return `${p.toFixed(1)}p`;
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
      // Per operator request: only solar, grid import/export, load + the two
      // Daikin slices. Battery charge/discharge bars intentionally dropped.
      data: ["Solar", "Grid Import", "Grid Export", "Load", "Daikin heating", "Daikin tank"],
    },
    xAxis: { ...(base.xAxis as object), data: labels },
    yAxis: [{ ...(base.yAxis as object), name: "kWh", nameTextStyle: { color: t.textMute, fontSize: 10 } }],
    series: [
      {
        ...seriesBar("Solar", points.map((p) => round1(p.solar_kwh)), t.pv, "house"),
        // The single structural rule separating sources (above) from sinks (below).
        markLine: {
          silent: true, symbol: "none",
          lineStyle: { color: t.border, width: 1, opacity: 0.5 },
          data: [{ yAxis: 0 }], label: { show: false },
        },
      },
      seriesBar("Grid Import", points.map((p) => round1(p.import_kwh)),     t.importColor, "house"),
      seriesBar("Grid Export", points.map((p) => -round1(p.export_kwh)),    t.exportColor, "out", true),
      {
        name: "Load",
        type: "line",
        data: points.map((p) => round1(p.load_kwh)),
        smooth: 0.4,
        symbol: "none",
        z: 10,
        lineStyle: { color: t.house, width: 2.5, cap: "round" },
        areaStyle: { color: areaGradient(t.house, 0.28, 0.02) },
        emphasis: { focus: "series" },
        universalTransition: { enabled: true },
      },
      // Daikin overlays — quiet secondary lines (dashed = heating, sparse
      // dotted = DHW estimate). No fills; they ride above the stack.
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

// Map Onecta's 2-hour Daikin buckets onto 30-min execution slots client-side.
// Each bucket covers 2 local-time hours; we spread its kWh evenly over the
// four 30-min slots inside it. Local-day boundary handling is approximate
// (within an hour at BST/GMT); fine-grained alignment lands when the backend
// writes per-slot Daikin directly. Returns separate heating + DHW so the
// day-view chart can stack them as distinct segments.
interface DaikinSplitPerSlot {
  total: number;
  heating: number;
  dhw: number;
}

function daikinSplitBySlotIso(daikin: DaikinConsumptionResponse | null, slots: ExecutionSlot[]): Map<string, DaikinSplitPerSlot> {
  const out = new Map<string, DaikinSplitPerSlot>();
  if (!daikin || !daikin.buckets?.length) return out;
  if (!slots.length) return out;
  for (const b of daikin.buckets) {
    if (!b.when || (b.kwh_total ?? 0) <= 0) continue;
    const baseUtc = new Date(b.when + "Z");
    if (Number.isNaN(baseUtc.getTime())) continue;
    const totPer30  = (b.kwh_total   ?? 0) / 4;
    const heatPer30 = (b.kwh_heating ?? 0) / 4;
    const dhwPer30  = (b.kwh_dhw     ?? 0) / 4;
    for (let k = 0; k < 4; k++) {
      const slotDt = new Date(baseUtc.getTime() + k * 30 * 60 * 1000);
      const iso = slotDt.toISOString().replace(/\.\d{3}Z$/, "Z").replace(/T(\d{2}):(\d{2}):\d{2}Z/, "T$1:$2:00Z");
      out.set(iso, { total: totPer30, heating: heatPer30, dhw: dhwPer30 });
    }
  }
  return out;
}

function optionForDay(
  exec: ExecutionTodayResponse | null,
  daikinConsumption: DaikinConsumptionResponse | null,
): Record<string, unknown> {
  const t = chartTheme();
  const base = baseOption();
  const slots = (exec?.slots || []).slice().sort((a, b) => (a.slot_utc ?? "").localeCompare(b.slot_utc ?? ""));
  const labels = slots.map((s) => formatSlotLabel(s.slot_utc));
  const splitBySlot = daikinSplitBySlotIso(daikinConsumption, slots);

  // Three-segment load stack — DHW (lightest) + Heating + Residual. When
  // we only have a single Daikin total (e.g. physics-estimate fallback
  // where heating/DHW split isn't known), bucket the unknown share into
  // Heating so the visual sums match consumption_kwh.
  const dhwPerSlot = slots.map((s) => {
    const split = s.slot_utc ? splitBySlot.get(s.slot_utc) : undefined;
    return round2(split?.dhw ?? 0);
  });
  const heatingPerSlot = slots.map((s) => {
    const split = s.slot_utc ? splitBySlot.get(s.slot_utc) : undefined;
    if (split) return round2(split.heating);
    // Fallback: whole physics estimate goes into Heating
    return round2(s.daikin_kwh_est ?? 0);
  });
  const residualPerSlot = slots.map((s, i) => {
    const load = s.consumption_kwh ?? 0;
    const daikin = (dhwPerSlot[i] ?? 0) + (heatingPerSlot[i] ?? 0);
    return round2(Math.max(0, load - daikin));
  });

  return {
    ...base,
    legend: { ...(base.legend as object), data: ["Daikin tank", "Daikin heating", "Residual", "Realised grid cost"] },
    // Quiet ghost note — per-slot solar/import/export genuinely unavailable
    // (#424). A text annotation, NOT a fabricated series/markArea.
    graphic: [{
      type: "text", right: 28, top: 30,
      style: { text: "per-slot solar · import · export — coming (#424)", fill: t.textMute, font: "600 11px system-ui", opacity: 0.55 },
    }],
    xAxis: { ...(base.xAxis as object), data: labels },
    yAxis: [
      { ...(base.yAxis as object), name: "kWh",  position: "left" },
      {
        ...(base.yAxis as object),
        name: "p",
        position: "right",
        splitLine: { show: false },
        axisLabel: { color: t.textMute, fontSize: 11, formatter: "{value}p" },
      },
    ],
    series: [
      {
        name: "Daikin tank",
        type: "bar",
        stack: "load",
        data: dhwPerSlot,
        itemStyle: { color: barGradient(t.pv, 0.9, 0.5) },
        emphasis: { focus: "series" },
        barCategoryGap: "40%",
      },
      {
        name: "Daikin heating",
        type: "bar",
        stack: "load",
        data: heatingPerSlot,
        itemStyle: { color: barGradient(t.warn, 0.9, 0.5) },
        emphasis: { focus: "series" },
      },
      {
        name: "Residual",
        type: "bar",
        stack: "load",
        data: residualPerSlot,
        // Top segment of the stack — rounds the crown so the bar reads as one.
        itemStyle: { color: barGradient(t.house, 0.75, 0.4), borderRadius: [3, 3, 0, 0] },
        emphasis: { focus: "series" },
      },
      {
        name: "Realised grid cost",
        type: "line",
        yAxisIndex: 1,
        data: slots.map((s) => s.cost_realised_p == null ? null : round2(s.cost_realised_p)),
        smooth: 0.4,
        symbol: "none",
        z: 10,
        lineStyle: { color: t.bad, width: 2, cap: "round" },
        areaStyle: { color: areaGradient(t.bad, 0.10, 0.0) },
      },
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
