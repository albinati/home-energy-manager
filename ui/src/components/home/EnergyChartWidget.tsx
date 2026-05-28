import { useEffect, useRef, useState } from "preact/hooks";
import { getEnergyPeriod } from "../../lib/endpoints";
import { makeChart, baseOption, chartTheme, type EChartsType } from "../../lib/charts";
import type { PeriodInsightsResponse, PeriodChartPoint, ExecutionTodayResponse } from "../../lib/types";
import "./energy-chart.css";

type Granularity = "day" | "week" | "month" | "year";

interface EnergyChartWidgetProps {
  // Execution slots used for the *day* view (30-min granularity) — same
  // source as the Today's bill widget so we don't duplicate the fetch.
  execution: ExecutionTodayResponse | null;
}

// Multi-series stacked chart with a granularity switcher:
//   * day   → 30-min slots from /execution/today  (consumption breakdown only;
//             solar/import/export per slot is NOT available — flagged below)
//   * week  → daily kWh from /energy/period?period=week     (7 points)
//   * month → daily kWh from /energy/period?period=month    (≤31 points)
//   * year  → monthly kWh from /energy/period?period=year   (≤12 points)
//
// Series:
//   solar (yellow positive area), import (red positive area), export
//   (green negative area), load (purple line on top), and — only on
//   day view — Daikin load (orange area) vs residual (blue area).
//
// Drill-down: clicking a bar in week/month switches to day mode for that
// date. Year → month works the same way.
export function EnergyChartWidget({ execution }: EnergyChartWidgetProps) {
  const [granularity, setGranularity] = useState<Granularity>("week");
  const [anchor, setAnchor] = useState<string>(() => new Date().toISOString().slice(0, 10));
  const [period, setPeriod] = useState<PeriodInsightsResponse | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const elRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<EChartsType | null>(null);

  // Fetch for non-day granularities (day reuses the parent's execution prop).
  useEffect(() => {
    if (granularity === "day") return;
    let alive = true;
    setLoading(true);
    setError(null);
    const opts: { date?: string; month?: string; year?: number } = {};
    if (granularity === "week") opts.date = anchor;
    else if (granularity === "month") opts.month = anchor.slice(0, 7);
    else if (granularity === "year") opts.year = Number(anchor.slice(0, 4));
    getEnergyPeriod(granularity, opts)
      .then((r) => { if (alive) { setPeriod(r); setLoading(false); } })
      .catch((e) => { if (alive) { setError(e.message || String(e)); setLoading(false); } });
    return () => { alive = false; };
  }, [granularity, anchor]);

  // Build option, write to chart.
  useEffect(() => {
    if (!elRef.current) return;
    if (!chartRef.current) chartRef.current = makeChart(elRef.current);
    const chart = chartRef.current;
    const option = granularity === "day"
      ? optionForDay(execution)
      : optionForPeriod(period, granularity);
    chart.setOption(option, true);
    const ro = new ResizeObserver(() => chart.resize());
    ro.observe(elRef.current);
    return () => ro.disconnect();
  }, [granularity, period, execution]);

  useEffect(() => () => {
    if (chartRef.current) {
      chartRef.current.dispose();
      chartRef.current = null;
    }
  }, []);

  // Click → drill down
  useEffect(() => {
    const c = chartRef.current;
    if (!c) return;
    const handler = (params: { name?: string }) => {
      const lbl = params?.name;
      if (!lbl) return;
      if (granularity === "week" || granularity === "month") {
        // lbl is YYYY-MM-DD — switch to day view (note: day view only shows
        // /execution/today which is *today*, so historical day drill-down
        // requires execution history endpoint we don't yet have).
        const today = new Date().toISOString().slice(0, 10);
        if (lbl === today) setGranularity("day");
      } else if (granularity === "year") {
        // lbl is YYYY-MM-01 → drill to month
        setAnchor(lbl);
        setGranularity("month");
      }
    };
    c.on("click", handler);
    return () => { c.off("click", handler); };
  }, [granularity]);

  const showDayMissingFlag = granularity === "day" && (!execution || !execution.slots?.length);
  const showSolarMissingFlag = granularity === "day";

  return (
    <div class="echart">
      <div class="echart-toolbar">
        <div class="echart-pills" role="tablist">
          {(["day", "week", "month", "year"] as Granularity[]).map((g) => (
            <button key={g}
                    class={`echart-pill${granularity === g ? " is-active" : ""}`}
                    onClick={() => setGranularity(g)}
                    role="tab" aria-selected={granularity === g}>
              {g === "day" ? "Today" : g === "week" ? "Week" : g === "month" ? "Month" : "Year"}
            </button>
          ))}
        </div>
        {period?.period_label && granularity !== "day" && (
          <span class="echart-label">{period.period_label}</span>
        )}
      </div>

      <div class="echart-host" ref={elRef} aria-label="Energy flow chart" />

      {loading && <div class="echart-state">Loading…</div>}
      {error && <div class="echart-state echart-state--err">{error}</div>}
      {showDayMissingFlag && <div class="echart-flag">No execution data for today yet — switch to Week.</div>}
      {showSolarMissingFlag && execution?.slots?.length && (
        <div class="echart-flag">
          Day view shows load breakdown only (Daikin vs residual). Solar /
          grid import / export per 30-min slot is <strong>not yet captured</strong>
          — see issue #424. Switch to Week for the full energy balance.
        </div>
      )}
      {granularity !== "day" && period && (
        <div class="echart-foot">
          Totals: {fmt(period.energy.solar_kwh)} solar · {fmt(period.energy.import_kwh)} import ·
          {" "}{fmt(period.energy.export_kwh)} export · {fmt(period.energy.load_kwh)} load
        </div>
      )}
    </div>
  );
}

function fmt(v: number | null | undefined): string {
  if (v == null) return "—";
  return `${v.toFixed(v >= 100 ? 0 : 1)} kWh`;
}

function optionForPeriod(period: PeriodInsightsResponse | null, gran: Granularity): Record<string, unknown> {
  const t = chartTheme();
  const base = baseOption();
  const points: PeriodChartPoint[] = period?.chart_data ?? [];
  const labels = points.map((p) => formatPointLabel(p.date, gran));

  return {
    ...base,
    legend: { ...(base.legend as object), data: ["Solar", "Import", "Export", "Load"] },
    xAxis: { ...(base.xAxis as object), data: labels },
    yAxis: [
      { ...(base.yAxis as object), name: "kWh", nameTextStyle: { color: t.textDim, fontSize: 10 } },
    ],
    series: [
      {
        name: "Solar",
        type: "bar",
        stack: "energy",
        data: points.map((p) => round1(p.solar_kwh)),
        itemStyle: { color: t.pv, borderRadius: [0, 0, 0, 0] },
        emphasis: { focus: "series" },
      },
      {
        name: "Import",
        type: "bar",
        stack: "energy",
        data: points.map((p) => round1(p.import_kwh)),
        itemStyle: { color: t.importColor, opacity: 0.85 },
        emphasis: { focus: "series" },
      },
      {
        name: "Export",
        type: "bar",
        data: points.map((p) => -round1(p.export_kwh)),
        itemStyle: { color: t.exportColor, opacity: 0.85 },
        emphasis: { focus: "series" },
      },
      {
        name: "Load",
        type: "line",
        data: points.map((p) => round1(p.load_kwh)),
        smooth: true,
        symbol: "circle",
        symbolSize: 5,
        lineStyle: { color: t.house, width: 2 },
        itemStyle: { color: t.house },
      },
    ],
  };
}

function optionForDay(exec: ExecutionTodayResponse | null): Record<string, unknown> {
  const t = chartTheme();
  const base = baseOption();
  const slots = (exec?.slots || []).slice().sort((a, b) => (a.slot_utc ?? "").localeCompare(b.slot_utc ?? ""));
  const labels = slots.map((s) => formatSlotLabel(s.slot_utc));

  return {
    ...base,
    legend: { ...(base.legend as object), data: ["Daikin", "Residual", "Price (p/kWh)"] },
    xAxis: { ...(base.xAxis as object), data: labels },
    yAxis: [
      { ...(base.yAxis as object), name: "kWh", position: "left" },
      { ...(base.yAxis as object), name: "p/kWh", position: "right", splitLine: { show: false } },
    ],
    series: [
      {
        name: "Daikin",
        type: "bar",
        stack: "load",
        data: slots.map((s) => round2(s.daikin_kwh_est ?? 0)),
        itemStyle: { color: t.warn, opacity: 0.85 },
        emphasis: { focus: "series" },
      },
      {
        name: "Residual",
        type: "bar",
        stack: "load",
        data: slots.map((s) => round2(s.residual_kwh ?? 0)),
        itemStyle: { color: t.house, opacity: 0.75 },
        emphasis: { focus: "series" },
      },
      {
        name: "Price (p/kWh)",
        type: "line",
        yAxisIndex: 1,
        data: slots.map((s) => s.agile_p ?? null),
        smooth: true,
        symbol: "none",
        lineStyle: { color: t.accent, width: 2, type: "dashed" },
        itemStyle: { color: t.accent },
      },
    ],
  };
}

function round1(v: number | null | undefined): number { return Math.round((v ?? 0) * 10) / 10; }
function round2(v: number | null | undefined): number { return Math.round((v ?? 0) * 100) / 100; }

function formatPointLabel(iso: string, gran: Granularity): string {
  // iso is YYYY-MM-DD for day/week/month, YYYY-MM-01 for year (which marks a month).
  if (gran === "year") return iso.slice(0, 7); // YYYY-MM
  const d = new Date(iso + "T00:00:00");
  return `${String(d.getDate()).padStart(2, "0")}/${String(d.getMonth() + 1).padStart(2, "0")}`;
}

function formatSlotLabel(iso?: string): string {
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  } catch { return iso; }
}
