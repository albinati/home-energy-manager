import { useEffect, useRef } from "preact/hooks";
import { makeChart, baseOption, chartTheme, areaGradient, withAlpha, type EChartsType } from "../../lib/charts";
import { useResolvedTheme } from "../../lib/theme";
import type { PvTodayResponse } from "../../lib/types";
import "./today-plan.css";

interface TodayPlanWidgetProps {
  pv: PvTodayResponse | null;
  loading: boolean;
}

// Slot kinds that mean the battery is being FILLED (cheap/free energy in) vs
// EMPTIED to the grid. Mirrors DispatchPlanStrip's colour buckets so the bands
// read the same across the app.
const CHARGE_KINDS = new Set(["negative", "cheap", "solar_charge", "solar_preheat", "charge"]);
const DISCHARGE_KINDS = new Set(["peak_export", "peak", "discharge"]);

function localHM(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
}

// One chart that answers "what's the plan today?" without tab-switching:
// import price + charge/discharge windows + load forecast + PV planned vs
// realised, all on a shared 30-min slot axis. Price on the right axis (p/kWh),
// energy on the left (kWh). All series come from /pv/today — a single
// full-day, server-aligned source (no client-side ISO key-matching).
export function TodayPlanWidget({ pv, loading }: TodayPlanWidgetProps) {
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

    const pvPlanned = slots.map((s) => round2(s.pv_forecast_kwh));
    const pvActual = slots.map((s) => (s.pv_actual_kwh == null ? null : round2(s.pv_actual_kwh)));
    const load = slots.map((s) => (s.base_load_kwh == null ? null : round2(s.base_load_kwh)));
    const price = slots.map((s) => (s.import_price_p == null ? null : s.import_price_p));

    // Charge/discharge background bands — contiguous runs of charge/discharge
    // kinds. markArea references the slot INDEX (DST-safe; two slots can share
    // a local HH:MM label on the autumn clock change).
    const bands: Array<[{ xAxis: number; itemStyle: object }, { xAxis: number }]> = [];
    let runStart = -1;
    let runKind: "charge" | "discharge" | null = null;
    const flush = (endIdx: number) => {
      if (runStart < 0 || runKind == null) return;
      const color = runKind === "charge" ? t.cheap : t.peak;
      bands.push([
        { xAxis: runStart, itemStyle: { color: withAlpha(color, 0.1) } },
        { xAxis: endIdx },
      ]);
    };
    slots.forEach((s, i) => {
      const k = s.kind || undefined;
      const cur: "charge" | "discharge" | null =
        k && CHARGE_KINDS.has(k) ? "charge" : k && DISCHARGE_KINDS.has(k) ? "discharge" : null;
      if (cur !== runKind) {
        if (runKind != null) flush(i - 1);
        runKind = cur;
        runStart = cur != null ? i : -1;
      }
    });
    if (runKind != null) flush(slots.length - 1);

    // "Now" marker — only when now falls within this day's slots.
    const nowMs = pv.now_utc ? new Date(pv.now_utc).getTime() : Date.now();
    const firstMs = new Date(slots[0].slot_utc).getTime();
    const lastMs = new Date(slots[slots.length - 1].slot_utc).getTime() + 30 * 60_000;
    let nowIdx = -1;
    if (nowMs >= firstMs && nowMs < lastMs) {
      const idx = slots.findIndex((s) => new Date(s.slot_utc).getTime() > nowMs);
      nowIdx = idx <= 0 ? slots.length - 1 : idx - 1;
    }

    chartRef.current.setOption({
      ...base,
      grid: { left: 48, right: 52, top: 28, bottom: 28, containLabel: true },
      legend: { ...(base.legend as object), show: true },
      tooltip: {
        ...(base.tooltip as object),
        formatter: (params: Array<{ dataIndex: number }>) => {
          const i = params[0]?.dataIndex ?? 0;
          const s = slots[i];
          if (!s) return "";
          return `<strong>${labels[i]}</strong>${s.kind ? ` · ${s.kind}` : ""}<br/>` +
            (price[i] != null ? `Import ${price[i]!.toFixed(1)}p/kWh<br/>` : "") +
            `PV planned ${pvPlanned[i].toFixed(2)} kWh<br/>` +
            (pvActual[i] != null ? `PV actual ${pvActual[i]!.toFixed(2)} kWh<br/>` : "") +
            (load[i] != null ? `Load ${load[i]!.toFixed(2)} kWh` : "");
        },
      },
      xAxis: { ...(base.xAxis as object), data: labels, axisLabel: { color: t.textMute, fontSize: 10, interval: 5 } },
      yAxis: [
        { ...(base.yAxis as object), name: "kWh", nameTextStyle: { color: t.textDim, fontSize: 10 } },
        {
          ...(base.yAxis as object),
          name: "p/kWh", position: "right",
          nameTextStyle: { color: t.textDim, fontSize: 10 },
          splitLine: { show: false },
          axisLabel: { color: t.textMute, fontSize: 10, formatter: "{value}p" },
        },
      ],
      series: [
        {
          name: "PV planned", type: "line", smooth: true, showSymbol: false,
          data: pvPlanned, lineStyle: { color: t.pv, width: 1.5, type: "dashed" },
          areaStyle: { color: areaGradient(t.pv, 0.16, 0.02) }, z: 2,
          markArea: bands.length ? { silent: true, data: bands } : undefined,
          markLine: nowIdx >= 0 ? {
            silent: true, symbol: "none",
            lineStyle: { color: t.textDim, width: 1, type: "dotted" },
            label: { show: true, formatter: "now", color: t.textMute, fontSize: 10 },
            data: [{ xAxis: nowIdx }],
          } : undefined,
        },
        {
          name: "PV actual", type: "line", smooth: true, showSymbol: false, connectNulls: false,
          data: pvActual, lineStyle: { color: t.pv, width: 2.5 },
          areaStyle: { color: areaGradient(t.pv, 0.32, 0.04) }, z: 3,
        },
        {
          name: "Load forecast", type: "line", smooth: true, showSymbol: false,
          data: load, lineStyle: { color: t.house, width: 1.5, type: "dashed" }, z: 2,
        },
        {
          name: "Import price", type: "line", step: "middle", showSymbol: false,
          yAxisIndex: 1, data: price, lineStyle: { color: t.importColor, width: 1.5, opacity: 0.8 }, z: 1,
        },
      ],
    }, { notMerge: true });
  }, [pv, theme]);

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
      <div ref={ref} style={{ width: "100%", height: "300px" }} />
      {!pv?.slots?.length && !loading && <p class="muted">No plan data for today yet.</p>}
    </div>
  );
}

function round2(n: number): number {
  return Math.round(n * 100) / 100;
}
