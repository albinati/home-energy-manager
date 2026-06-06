import { useEffect, useRef } from "preact/hooks";
import { makeChart, baseOption, chartTheme, areaGradient, withAlpha, type EChartsType } from "../../lib/charts";
import { useResolvedTheme } from "../../lib/theme";
import { reducedMotion } from "../../lib/motion";
import type { HeatingPlanResponse } from "../../lib/types";
import "./heating-plan.css";

interface Props {
  plan: HeatingPlanResponse | null;
  loading: boolean;
}

function localHM(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
}

// Heating-plan timeline (yesterday · today · tomorrow). The story it tells, in
// one temperature axis: the OUTDOOR forecast drives the weather-curve LWT (the
// natural radiator water temp), and HEM nudges that into the actual RADIATOR
// setpoint = curve + offset — boosting ahead of expensive periods, setting back
// through them. The gap between the faint curve line and the bold radiator line
// IS the offset. Tank target rides alongside; warm wash = heating, blue = paid.
export function HeatingPlanWidget({ plan, loading }: Props) {
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
    if (!chartRef.current || !plan?.slots?.length) return;
    const t = chartTheme();
    const base = baseOption();
    const slots = plan.slots;
    const n = slots.length;
    const labels = slots.map((s) => localHM(s.slot_utc));

    const outdoor = slots.map((s) => (s.outdoor_c == null ? null : s.outdoor_c));
    const lwtBase = slots.map((s) => (s.lwt_base_c == null ? null : s.lwt_base_c));
    const lwtSet = slots.map((s) => (s.lwt_setpoint_c == null ? null : s.lwt_setpoint_c));
    const tank = slots.map((s) => (s.tank_temp_c == null ? null : s.tank_temp_c));
    const animate = !reducedMotion();

    // Background bands: heating-on warm wash + a stronger blue band on
    // negative-price ("paid to import") slots.
    type BandItem = [{ xAxis: number; itemStyle: object }, { xAxis: number }];
    const bands: BandItem[] = [];
    const runs = (pred: (i: number) => boolean, fill: object) => {
      let start = -1;
      for (let i = 0; i <= n; i++) {
        const on = i < n && pred(i);
        if (on && start < 0) start = i;
        if (!on && start >= 0) { bands.push([{ xAxis: start - 0.5, itemStyle: fill }, { xAxis: i - 1 + 0.5 }]); start = -1; }
      }
    };
    runs((i) => !!slots[i].heating_on, { color: withAlpha(t.warn, 0.06) });
    runs((i) => slots[i].tier === "negative", { color: withAlpha(t.neg, 0.2), borderColor: withAlpha(t.neg, 0.75), borderWidth: 1 });

    const dayStartIdx = (plan.days || []).map((d) => slots.findIndex((s) => s.slot_utc >= d.start_utc)).filter((i) => i > 0);
    const nowMs = plan.now_utc ? new Date(plan.now_utc).getTime() : Date.now();
    const firstMs = new Date(slots[0].slot_utc).getTime();
    const lastMs = new Date(slots[n - 1].slot_utc).getTime() + 30 * 60_000;
    let nowIdx = -1;
    if (nowMs >= firstMs && nowMs < lastMs) {
      const idx = slots.findIndex((s) => new Date(s.slot_utc).getTime() > nowMs);
      nowIdx = idx <= 0 ? n - 1 : idx - 1;
    }
    const dayLines = dayStartIdx.map((i) => ({
      xAxis: i, lineStyle: { color: withAlpha(t.textMute, 0.35), width: 1, type: "solid" as const }, label: { show: false },
    }));
    const nowLine = nowIdx >= 0
      ? [{ xAxis: nowIdx, lineStyle: { color: t.text, width: 1.5, opacity: 0.5 }, label: { show: false } }]
      : [];

    chartRef.current.setOption({
      ...base,
      legend: { show: false },
      grid: { left: 14, right: 16, top: 14, bottom: 22, containLabel: true },
      tooltip: {
        ...(base.tooltip as object),
        formatter: (params: Array<{ dataIndex: number }>) => {
          const i = params[0]?.dataIndex ?? 0;
          const s = slots[i];
          if (!s) return "";
          const rows: string[] = [`<strong>${labels[i]}</strong>${s.heating_on ? " · heating" : " · idle"}`];
          if (s.outdoor_c != null) rows.push(`Outdoor <strong>${s.outdoor_c.toFixed(1)}°C</strong>`);
          if (s.lwt_setpoint_c != null) {
            const off = s.lwt_offset || 0;
            const offTxt = off ? ` (curve ${s.lwt_base_c?.toFixed(0)} ${off > 0 ? "+" : "−"}${Math.abs(off)})` : "";
            rows.push(`Radiator LWT <strong>${s.lwt_setpoint_c.toFixed(0)}°C</strong>${offTxt}`);
          }
          if (s.tank_temp_c != null) rows.push(`Tank <strong>${s.tank_temp_c}°C</strong>${s.tank_kind ? ` · ${s.tank_kind}` : ""}`);
          if (s.price_p != null) rows.push(`<span style="color:${t.textMute}">${s.price_p.toFixed(1)}p${s.tier ? ` · ${s.tier}` : ""}</span>`);
          return rows.join("<br/>");
        },
      },
      xAxis: { ...(base.xAxis as object), data: labels, axisLabel: { color: t.textMute, fontSize: 10, interval: 11 } },
      yAxis: [{ ...(base.yAxis as object), name: "°C", nameTextStyle: { color: t.textMute, fontSize: 10 },
        axisLabel: { color: t.textMute, fontSize: 10, formatter: "{value}" } }],
      series: [
        { name: "_bg", type: "line", data: slots.map(() => null), silent: true, z: 0,
          markArea: bands.length ? { silent: true, data: bands } : undefined,
          markLine: (dayLines.length || nowLine.length) ? { silent: true, symbol: "none", data: [...dayLines, ...nowLine] } : undefined },
        // Outdoor — the cause (cool blue, soft fill, sits low).
        { name: "Outdoor", type: "line", smooth: true, showSymbol: false, connectNulls: true,
          data: outdoor, lineStyle: { color: t.grid, width: 2, cap: "round" },
          areaStyle: { color: areaGradient(t.grid, 0.14, 0.0) }, z: 2 },
        // Weather-curve LWT (offset 0) — faint dotted reference.
        { name: "Curve LWT", type: "line", smooth: true, showSymbol: false, connectNulls: false,
          data: lwtBase, lineStyle: { color: withAlpha(t.house, 0.45), width: 1, type: "dotted" }, z: 3 },
        // Radiator LWT (curve + offset) — the hero: what HEM actually commands.
        { name: "Radiator LWT", type: "line", smooth: true, showSymbol: false, connectNulls: false,
          data: lwtSet, lineStyle: { color: t.house, width: 2.75, cap: "round" },
          areaStyle: { color: areaGradient(t.house, 0.12, 0.0) }, z: 5 },
        // Tank target — orange step.
        { name: "Tank", type: "line", step: "middle", showSymbol: false, connectNulls: false,
          data: tank, lineStyle: { color: t.thermal, width: 1.5, type: "dashed", cap: "round" }, z: 4 },
        ...(nowIdx >= 0 ? [{
          name: "_now", type: "effectScatter", silent: true, coordinateSystem: "cartesian2d",
          symbolSize: 8, z: 6, showEffectOn: "render",
          rippleEffect: { period: animate ? 2.4 : 0, scale: animate ? 3.0 : 1, brushType: "stroke" },
          itemStyle: { color: t.accent, shadowBlur: 8, shadowColor: t.accent },
          data: [[nowIdx, lwtSet[nowIdx] ?? outdoor[nowIdx] ?? 20]],
        }] : []),
      ],
    }, { notMerge: true });
  }, [plan, theme]);

  return (
    <div class="heating-plan-chart">
      <div class="hpl-days">
        {(plan?.days || []).map((d) => (
          <span key={d.date} class={`hpl-day${d.label === "Today" ? " hpl-day--today" : ""}`}>{d.label}</span>
        ))}
      </div>
      <div ref={ref} style={{ width: "100%", height: "300px" }} />
      {plan?.slots?.length ? (
        <div class="hpl-legend" role="note" aria-label="Chart legend">
          <span class="hpl-tok"><span class="hpl-line hpl-line--outdoor" /> outdoor</span>
          <span class="hpl-tok"><span class="hpl-line hpl-line--lwt" /> radiator LWT</span>
          <span class="hpl-tok"><span class="hpl-line hpl-line--curve" /> curve (no offset)</span>
          <span class="hpl-tok"><span class="hpl-line hpl-line--tank" /> tank</span>
          <span class="hpl-tok"><span class="hpl-sw hpl-sw--heat" /> heating</span>
          <span class="hpl-tok"><span class="hpl-sw hpl-sw--neg" /> paid to import</span>
          <span class="hpl-hint">◉ now · hover for detail</span>
        </div>
      ) : null}
      {!plan?.slots?.length && !loading && <p class="muted">No heating plan available yet.</p>}
    </div>
  );
}
