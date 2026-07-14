import { useEffect, useRef } from "preact/hooks";
import { makeChart, baseOption, chartTheme, areaGradient, withAlpha, timeAxis, insideZoom, forecastWash, isCoarsePointer, SLOT_MS as LW_SLOT_MS, type EChartsType } from "../../lib/charts";
import { liveWindow, centerWindow, useLiveWindow, type LiveWindowBounds } from "../../lib/liveWindow";
import { useChartPan } from "../../lib/navMotion";
import { useResolvedTheme } from "../../lib/theme";
import { reducedMotion } from "../../lib/motion";
import type { HeatingPlanResponse, ExecutionTodayResponse, IndoorReadingsResponse } from "../../lib/types";
import { NowDot } from "../common/NowDot";
import "./heating-plan.css";

interface Props {
  plan: HeatingPlanResponse | null;
  loading: boolean;
  // Realised Daikin telemetry per slot (logged LWT) — the solid "what actually
  // happened" against the dotted committed plan.
  execution?: ExecutionTodayResponse | null;
  // Realised indoor-temp history (room sensors, #540 W1) — the solid realised
  // room-temperature line.
  indoor?: IndoorReadingsResponse | null;
}

function localHM(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
}

// Heating-plan timeline (today), on one temperature axis. Colour = domain:
//   • COOL (cyan/blue) = reference air temps — indoor REALISED (solid, room
//     sensors) + outdoor forecast/ESTIMATE (dotted, Open-Meteo).
//   • ORANGE = tank / DHW target.
//   • PURPLE = radiator LWT / heating — PLANNED (dotted, committed setpoint) vs
//     REALISED (solid, Daikin logged). Their gap is the actuation error.
// Style = solid → realised/measured, dotted → planned/estimate. Import price is
// a thin solid line on the right axis. Warm wash = heating, blue band = paid.
export function HeatingPlanWidget({ plan, loading, execution, indoor }: Props) {
  const ref = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<EChartsType | null>(null);
  const boundsRef = useRef<LiveWindowBounds | null>(null);
  const theme = useResolvedTheme();
  // Shares the live window with the Consumption chart (they pan/follow together,
  // vertically aligned). The single "● now" chip lives on Consumption and
  // re-follows all three, so this chart needs no chip of its own.
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
    if (!chartRef.current || !plan?.slots?.length) return;
    const t = chartTheme();
    const base = baseOption();
    // Today only — yesterday/tomorrow dropped (the user finds today enough).
    const todayKey = new Date().toDateString();
    const slots = plan.slots.filter((s) => new Date(s.slot_utc).toDateString() === todayKey);
    if (!slots.length) return;
    const n = slots.length;
    const labels = slots.map((s) => localHM(s.slot_utc));
    // TIME AXIS — slot START instants; interval-true bands become literal.
    const axisMs = slots.map((s) => new Date(s.slot_utc).getTime());
    const dayStartMs = axisMs[0];
    const dayEndMs = axisMs[n - 1] + LW_SLOT_MS;
    const pair = (arr: Array<number | null>): Array<[number, number | null]> =>
      arr.map((v, i) => [axisMs[i], v]);
    boundsRef.current = {
      dayStartMs, dayEndMs,
      nowMs: plan.now_utc ? new Date(plan.now_utc).getTime() : Date.now(),
    };
    const coarse = isCoarsePointer();

    const tank = slots.map((s) => (s.tank_temp_c == null ? null : s.tank_temp_c));
    const animate = !reducedMotion();

    // REALISED radiator LWT — the Daikin's logged leaving-water temp per slot
    // (execution_today), aligned to the plan slots by 30-min bucket. Only past
    // slots have it, so the solid line naturally stops at "now" and any gap to
    // the dotted plan is where the command didn't land (arbitration / drift).
    const SLOT_MS = 30 * 60_000;
    const bucket = (iso: string) => Math.floor(new Date(iso).getTime() / SLOT_MS);
    const lwtRealByBucket = new Map<number, number>();
    for (const e of execution?.slots ?? []) {
      if (e.slot_utc && e.daikin_lwt_c != null) lwtRealByBucket.set(bucket(e.slot_utc), e.daikin_lwt_c);
    }
    const lwtReal = slots.map((s) => lwtRealByBucket.get(bucket(s.slot_utc)) ?? null);

    // REALISED indoor temp — mean of the room-sensor readings in each 30-min
    // slot (#540 W1). Only slots the sensors covered have a value, so the solid
    // line appears from when the first sensor came online.
    const inSum = new Map<number, { sum: number; n: number }>();
    for (const r of indoor?.readings ?? []) {
      if (r.captured_at == null || r.temp_c == null) continue;
      const b = bucket(r.captured_at);
      const cur = inSum.get(b) ?? { sum: 0, n: 0 };
      cur.sum += r.temp_c; cur.n += 1; inSum.set(b, cur);
    }
    const indoorReal = slots.map((s) => {
      const c = inSum.get(bucket(s.slot_utc));
      return c && c.n > 0 ? Math.round((c.sum / c.n) * 10) / 10 : null;
    });
    // PLANNED indoor — the LP's committed t_in trajectory (W3). All null until
    // W3 is enabled, so the dashed line simply doesn't render before then.
    const indoorPlanned = slots.map((s) => s.indoor_planned_c ?? null);
    const hasIndoorPlan = indoorPlanned.some((v) => v != null);

    // REALISED tank temp — the Daikin's logged tank temperature per slot.
    const tankRealByBucket = new Map<number, number>();
    for (const e of execution?.slots ?? []) {
      if (e.slot_utc && e.daikin_tank_c != null) tankRealByBucket.set(bucket(e.slot_utc), e.daikin_tank_c);
    }
    const tankReal = slots.map((s) => tankRealByBucket.get(bucket(s.slot_utc)) ?? null);

    // Background bands: tariff tiers (same context wash as the Consumption chart)
    // — cheap green, peak amber, negative blue. Replaces the price LINE.
    // Interval-true tier bands on the time axis: a run [start..end] spans
    // [axisMs[start], axisMs[end] + SLOT_MS] — the run's first slot START to the
    // last slot's END. Unifies the band geometry with the Consumption chart (this
    // widget's old `[start-0.5, end+0.5]` convention was the third variant).
    type BandItem = [{ xAxis: number; itemStyle: object }, { xAxis: number }];
    const bands: BandItem[] = [];
    const runs = (pred: (i: number) => boolean, fill: object) => {
      let start = -1;
      for (let i = 0; i <= n; i++) {
        const on = i < n && pred(i);
        if (on && start < 0) start = i;
        if (!on && start >= 0) { bands.push([{ xAxis: axisMs[start], itemStyle: fill }, { xAxis: axisMs[i - 1] + LW_SLOT_MS }]); start = -1; }
      }
    };
    runs((i) => slots[i].tier === "cheap", { color: withAlpha(t.cheap, 0.09) });
    runs((i) => slots[i].tier === "peak", { color: withAlpha(t.peak, 0.10) });
    runs((i) => slots[i].tier === "negative", { color: withAlpha(t.neg, 0.22), borderColor: withAlpha(t.neg, 0.85), borderWidth: 1 });

    const nowMs = plan.now_utc ? new Date(plan.now_utc).getTime() : Date.now();
    const nowInRange = nowMs >= dayStartMs && nowMs < dayEndMs;
    // Day boundary lines → real timestamps.
    const dayLines = (plan.days || [])
      .map((d) => new Date(d.start_utc).getTime())
      .filter((ms) => ms > dayStartMs && ms < dayEndMs)
      .map((ms) => ({ xAxis: ms, lineStyle: { color: withAlpha(t.textMute, 0.35), width: 1, type: "solid" as const }, label: { show: false } }));
    const nowLine = nowInRange
      ? [{ xAxis: nowMs, lineStyle: { color: t.text, width: 1.5, opacity: 0.5 }, label: { show: false } }]
      : [];
    const washArea = nowInRange ? forecastWash(nowMs, dayEndMs) : [];
    // Initial window (same follow/browse-preserving read as the Consumption chart).
    const lw = liveWindow.value;
    const win = (lw.startMs && lw.endMs && !lw.follow)
      ? { startMs: lw.startMs, endMs: lw.endMs }
      : centerWindow(nowMs, dayStartMs, dayEndMs);
    // Y value at now for the pulse (nearest slot's realised temp).
    const nowY = (() => {
      if (!nowInRange) return 20;
      let idx = axisMs.findIndex((ms) => ms > nowMs);
      idx = idx <= 0 ? n - 1 : idx - 1;
      return indoorReal[idx] ?? lwtReal[idx] ?? tankReal[idx] ?? 20;
    })();

    chartRef.current.setOption({
      ...base,
      legend: { show: false },
      // Match the Generation/Consumption grid (incl. the right:44 the price axis
      // reserves there) so all three intraday charts line up on the x-axis and
      // a given time reads straight down the screen.
      grid: { left: 16, right: 44, top: 16, bottom: 24, containLabel: true },
      tooltip: {
        ...(base.tooltip as object),
        formatter: (params: Array<{ dataIndex: number }>) => {
          const i = params[0]?.dataIndex ?? 0;
          const s = slots[i];
          if (!s) return "";
          const rows: string[] = [`<strong>${labels[i]}</strong>${s.heating_on ? " · heating" : " · idle"}`];
          if (indoorReal[i] != null) rows.push(`Indoor <strong>${(indoorReal[i] as number).toFixed(1)}°C</strong> · realised`);
          if (indoorPlanned[i] != null) rows.push(`Indoor <strong>${(indoorPlanned[i] as number).toFixed(1)}°C</strong> · planned`);
          if (lwtReal[i] != null) rows.push(`LWT real <strong>${(lwtReal[i] as number).toFixed(0)}°C</strong>`);
          if (s.tank_temp_c != null) rows.push(`Tank plan <strong>${s.tank_temp_c}°C</strong>${s.tank_kind ? ` · ${s.tank_kind}` : ""}`);
          if (tankReal[i] != null) rows.push(`Tank real <strong>${(tankReal[i] as number).toFixed(0)}°C</strong>`);
          if (s.price_p != null) rows.push(`<span style="color:${t.textMute}">${s.price_p.toFixed(1)}p${s.tier ? ` · ${s.tier}` : ""}</span>`);
          return rows.join("<br/>");
        },
      },
      // Live window (see Consumption chart): desktop = full-day axis + inside
      // dataZoom; touch = window as axis min/max + useChartPan.
      xAxis: coarse
        ? { ...timeAxis(win.startMs, win.endMs), axisLabel: { color: t.textMute, fontSize: 10, hideOverlap: true, formatter: "{HH}:{mm}" } }
        : { ...timeAxis(dayStartMs, dayEndMs), axisLabel: { color: t.textMute, fontSize: 10, hideOverlap: true, formatter: "{HH}:{mm}" } },
      ...(coarse ? {} : { dataZoom: [insideZoom(win.startMs, win.endMs)] }),
      // Single °C axis (price is now conveyed by the tariff bands, not a line).
      // grid.right kept so the plot box still lines up with Generation/Consumption.
      yAxis: [
        { ...(base.yAxis as object), name: "°C", nameTextStyle: { color: t.textMute, fontSize: 10 },
          axisLabel: { color: t.textMute, fontSize: 10, formatter: "{value}" } },
      ],
      series: [
        { name: "_bg", type: "line", data: axisMs.map((ms) => [ms, null]), silent: true, z: 0,
          markArea: (bands.length || washArea.length) ? { silent: true, data: [...washArea, ...bands] } : undefined,
          markLine: (dayLines.length || nowLine.length) ? { silent: true, symbol: "none", data: [...dayLines, ...nowLine] } : undefined },
        // ── REFERENCE — indoor room temp, REALISED (cyan solid). Outdoor
        //    removed per request. ──
        { name: "Indoor", type: "line", smooth: true, showSymbol: false, connectNulls: false,
          data: pair(indoorReal), lineStyle: { color: t.cool, width: 2.5, cap: "round" }, z: 4 },
        // W3: planned indoor (LP committed) — dashed cyan, only when W3 is on.
        ...(hasIndoorPlan ? [{
          name: "Indoor planned", type: "line" as const, smooth: true, showSymbol: false,
          connectNulls: false, data: pair(indoorPlanned),
          lineStyle: { color: t.cool, width: 1.5, type: "dashed" as const, cap: "round" as const, opacity: 0.8 }, z: 3,
        }] : []),
        // ── TANK / DHW (orange). Planned target (dashed) vs realised (solid). ──
        { name: "Tank planned", type: "line", step: "middle", showSymbol: false, connectNulls: false,
          data: pair(tank), lineStyle: { color: t.thermal, width: 1.5, type: "dashed", cap: "round" }, z: 3 },
        { name: "Tank realised", type: "line", step: "middle", showSymbol: false, connectNulls: false,
          data: pair(tankReal), lineStyle: { color: t.thermal, width: 2.5, cap: "round" }, z: 4 },
        // ── HEATING / radiator LWT (purple) — REALISED only (the Daikin's logged
        //    leaving-water temp across the day). Plan line dropped per request.
        { name: "LWT realised", type: "line", smooth: true, showSymbol: false, connectNulls: false,
          data: pair(lwtReal), lineStyle: { color: t.house, width: 3, cap: "round" },
          areaStyle: { color: areaGradient(t.house, 0.12, 0.0) }, z: 5 },
        ...(nowInRange ? [{
          name: "_now", type: "effectScatter", silent: true,
          symbolSize: 8, z: 6, showEffectOn: "render",
          rippleEffect: { period: animate ? 2.4 : 0, scale: animate ? 3.0 : 1, brushType: "stroke" },
          itemStyle: { color: t.accent, shadowBlur: 8, shadowColor: t.accent },
          data: [[nowMs, nowY]],
        }] : []),
      ],
    }, { notMerge: true });
  }, [plan, execution, indoor, theme]);

  return (
    <div class="heating-plan-chart">
      <div ref={ref} style={{ width: "100%", height: "300px" }} />
      {plan?.slots?.length ? (
        <div class="hpl-legend" role="note" aria-label="Chart legend">
          <span class="hpl-tok"><span class="hpl-line hpl-line--indoor" /> indoor (real)</span>
          <span class="hpl-legend-grp">DHW</span>
          <span class="hpl-tok"><span class="hpl-line hpl-line--tank-real" /> tank real</span>
          <span class="hpl-tok"><span class="hpl-line hpl-line--tank" /> tank plan</span>
          <span class="hpl-legend-grp">heating</span>
          <span class="hpl-tok"><span class="hpl-line hpl-line--realised" /> LWT real</span>
          <span class="hpl-legend-grp">tariff</span>
          <span class="hpl-tok"><span class="hpl-sw hpl-sw--cheap" /> cheap</span>
          <span class="hpl-tok"><span class="hpl-sw hpl-sw--peak" /> peak</span>
          <span class="hpl-tok"><span class="hpl-sw hpl-sw--neg" /> paid to import</span>
          <span class="hpl-hint"><NowDot /> now · hover for detail</span>
        </div>
      ) : null}
      {!plan?.slots?.length && !loading && <p class="muted">No heating plan available yet.</p>}
    </div>
  );
}
