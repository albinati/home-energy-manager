import { useEffect, useMemo, useRef, useState } from "preact/hooks";
import { Link } from "wouter-preact";
import type { CockpitNow, PlanFrontsResponse } from "../../../lib/types";
import { usePoll } from "../../../lib/poll";
import { getPlanFronts, getPvToday, getExecutionToday, getSchedulerTimeline } from "../../../lib/endpoints";
import {
  frac, tariffSegments, heatingCell, waterSegments, hourTicks, hourWord, tierOf, span, FAIXA_WORD,
  type HolidayFlags, type Axis,
} from "../simple/logic";
import {
  localYmd, addDaysYmd, localDayBounds, utcDatesOf, hhmm, buildDaySeries, linePath, areaPath, kwCeil,
  daySummary, fullAt, fmt, type Frame, type PeakInfo, type Pt,
} from "./dayChart";
import "../../../styles/home-v3-day.css";

// Dev-only fixture switch (`?mock=1`); eliminated from the production bundle.
const MOCK = import.meta.env.DEV && new URLSearchParams(location.search).get("mock") === "1";

// A segment shows its text only when it is at least this wide; the full text is in the tooltip.
const MIN_TEXT_PX = 56;
const FRAME_H = 200;
const FRAME_W = 960;

type DayKey = "hoje" | "amanha";

// "O dia" (#898): the day as one hand-rolled SVG chart (sol, bateria %, consumo) with
// the Cosy tariff / heating / hot-water lanes under it, on one shared x axis.
// Hoje = measured + plan; Amanhã = what the planner already knows (nothing is guessed).
export function DayCard({ now, fronts, holiday }: {
  now: CockpitNow; fronts: PlanFrontsResponse | null; holiday: HolidayFlags;
}) {
  const tz = now.planner_tz;
  const nowMs = Date.parse(now.now_utc);
  const [day, setDay] = useState<DayKey>("hoje");
  const [show, setShow] = useState({ pv: true, soc: true, load: false });
  const isToday = day === "hoje";
  const todayYmd = localYmd(nowMs, tz);
  const ymd = isToday ? todayYmd : addDaysYmd(todayYmd, 1);
  const bounds = useMemo(() => localDayBounds(ymd, tz), [ymd, tz]);
  const dates = utcDatesOf(bounds);
  const dkey = dates.join(",");

  // The "today" endpoints serve UTC days; a local day can touch two of them (BST).
  const pv = usePoll(async () => {
    if (import.meta.env.DEV && MOCK) return (await import("../../../lib/mock/day")).mockPvToday(dates);
    return Promise.all(dates.map((d) => getPvToday(d)));
  }, 120_000, [dkey]);
  const exec = usePoll(async () => {
    if (!isToday) return [];
    if (import.meta.env.DEV && MOCK) return (await import("../../../lib/mock/day")).mockExecToday(dates);
    return Promise.all(dates.map((d) => getExecutionToday(d)));
  }, 120_000, [dkey, isToday]);
  const timeline = usePoll(async () => {
    if (import.meta.env.DEV && MOCK) return (await import("../../../lib/mock/day")).mockTimeline();
    return getSchedulerTimeline();
  }, 120_000);
  const tomorrowFronts = usePoll(async () => {
    if (isToday) return null;
    if (import.meta.env.DEV && MOCK) return (await import("../../../lib/mock/day")).mockFrontsTomorrow();
    return getPlanFronts(ymd);
  }, 60_000, [ymd, isToday]);

  const dayFronts = isToday ? fronts : tomorrowFronts.data;
  const st = now.state;
  const capacityKwh = (fronts?.battery && !fronts.battery.error ? fronts.battery.capacity_kwh : null)
    ?? (st.soc_pct > 5 && st.soc_kwh > 0 ? (st.soc_kwh / st.soc_pct) * 100 : null);

  const series = useMemo(() => buildDaySeries({
    bounds, nowMs, isToday, pv: pv.data ?? [], exec: exec.data ?? [], timeline: timeline.data,
    capacityKwh, socNowPct: Number.isFinite(st.soc_pct) ? st.soc_pct : null,
  }), [bounds, nowMs, isToday, pv.data, exec.data, timeline.data, capacityKwh, st.soc_pct]);

  const tariff = dayFronts?.tariff && !dayFronts.tariff.error ? dayFronts.tariff : null;
  const axis: Axis = { startMs: bounds.startMs, endMs: bounds.endMs };
  const peak = useMemo<PeakInfo | null>(() => {
    const w = tariff?.windows.find((x) => tierOf(x) === "peak");
    if (!w) return null;
    const row = dayFronts?.battery?.by_band?.find((r) => r.start_local === w.start_local && r.end_local === w.end_local);
    return {
      startMs: Date.parse(w.start_utc), endMs: Date.parse(w.end_utc), label: span(w.start_local, w.end_local),
      plannedImportKwh: row?.planned_import_kwh ?? null,
    };
  }, [tariff, dayFronts]);
  const summary = daySummary({ isToday, series, peak, tz });

  // ---- chart geometry (SVG viewBox units; the element stretches with preserveAspectRatio=none)
  const frame: Frame = { w: FRAME_W, h: FRAME_H, pad: 6, startMs: bounds.startMs, endMs: bounds.endMs };
  const kwSeries: Pt[][] = [];
  if (show.pv) kwSeries.push(series.pvFc, series.pvAct);
  if (show.load) kwSeries.push(series.loadFc, series.loadAct);
  const kwMax = kwCeil(kwSeries);
  const useKw = show.pv || show.load;
  const axisTop = useKw ? `${fmt(kwMax, 1)} kW` : show.soc ? "100 %" : "";
  const axisMid = useKw ? fmt(kwMax / 2, 1) : show.soc ? "50 %" : "";
  const hasAny = (show.pv && (series.pvFc.length > 0 || series.pvAct.length > 0))
    || (show.soc && (series.socAct.length > 0 || series.socPlan.length > 0))
    || (show.load && (series.loadFc.length > 0 || series.loadAct.length > 0));

  // ---- header numbers
  const metrics: Array<{ label: string; value: string; unit?: string }> = isToday
    ? [
      { label: "Sol gerado até agora", value: series.pvSoFarKwh != null ? fmt(series.pvSoFarKwh, 1) : "—", unit: "kWh" },
      { label: "Previsto no dia", value: series.pvForecastKwh != null ? fmt(series.pvForecastKwh, 1) : "—", unit: "kWh" },
    ]
    : [
      { label: "Sol previsto amanhã", value: series.pvForecastKwh != null ? fmt(series.pvForecastKwh, 1) : "—", unit: "kWh" },
      ...(fullAt(series) != null ? [{ label: "Bateria cheia às", value: hhmm(fullAt(series) as number, tz) }] : []),
    ];
  const ariaLabel = `${isToday ? "Hoje" : "Amanhã"}: ${metrics.map((m) => `${m.label.toLowerCase()} ${m.value}${m.unit ? ` ${m.unit}` : ""}`).join("; ")}.${summary ? ` ${summary}` : ""}`;

  // ---- lanes
  const trackRef = useRef<HTMLDivElement>(null);
  const [trackPx, setTrackPx] = useState(0);
  useEffect(() => {
    const el = trackRef.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => setTrackPx(el.clientWidth));
    ro.observe(el);
    setTrackPx(el.clientWidth);
    return () => ro.disconnect();
  }, [tariff]);
  const pct = (f: number) => `${(f * 100).toFixed(3)}%`;
  const pos = (a: number, b: number) => ({ left: `calc(${pct(a)} + 1px)`, width: `calc(${pct(b - a)} - 2px)` });
  const fits = (a: number, b: number) => (b - a) * trackPx >= MIN_TEXT_PX;

  const wins = tariff
    ? [...tariff.windows].sort((a, b) => Date.parse(a.start_utc) - Date.parse(b.start_utc))
        .map((w) => ({ w, a: frac(axis, Date.parse(w.start_utc)), b: frac(axis, Date.parse(w.end_utc)) }))
    : [];
  const segs = tariffSegments(tariff, axis);
  const water = waterSegments(dayFronts?.tank, axis);
  const heatCells = wins
    .map(({ w, a, b }) => ({ a, b, cell: heatingCell(w, dayFronts?.heating, holiday.climate) }))
    .filter((x) => x.cell != null);

  const ticks: Array<{ f: number; label: string }> = [];
  if (tariff && tariff.structure === "banded" && wins.length <= 9) {
    const seen = new Set<string>();
    for (const { w, a } of wins) {
      if (seen.has(w.start_local)) continue;
      seen.add(w.start_local);
      ticks.push({ f: a, label: hourWord(w.start_local) });
    }
  } else {
    for (const t of hourTicks(axis, tz, [0, 6, 12, 18])) ticks.push({ f: t.f, label: String(t.hour).padStart(2, "0") });
  }
  const nowF = frac(axis, nowMs);

  const chip = (k: "pv" | "soc" | "load", label: string, dot: string) => (
    <button type="button" class="dc-chip" aria-pressed={show[k]} onClick={() => setShow({ ...show, [k]: !show[k] })}>
      <span class={`dc-dot dc-dot--${dot}`} aria-hidden="true" />{label}
    </button>
  );

  return (
    <section class="hs-card dc-day" aria-label="O dia">
      <div class="dc-head">
        <h2 class="hs-eyebrow">O dia · sol, bateria e plano</h2>
        <div class="dc-seg" role="group" aria-label="Dia">
          <button type="button" aria-pressed={isToday} onClick={() => setDay("hoje")}>Hoje</button>
          <button type="button" aria-pressed={!isToday} onClick={() => setDay("amanha")}>Amanhã</button>
        </div>
      </div>

      <div class="dc-metrics">
        {metrics.map((m) => (
          <div key={m.label} class="dc-metric">
            <span class="dc-metric-label">{m.label}</span>
            <span class="dc-metric-value">{m.value}{m.unit && <span class="dc-metric-unit"> {m.unit}</span>}</span>
          </div>
        ))}
      </div>

      <div class="dc-chips" role="group" aria-label="Séries do gráfico">
        {chip("pv", "Sol (kW)", "sol")}
        {chip("soc", "Bateria (%)", "bateria")}
        {chip("load", "Consumo da casa (kW)", "casa")}
      </div>

      <div class="dc-plot">
        <div class="dc-main">
          {tariff && (
            <div class="hs-lane">
              <span class="hs-lane-label">Tarifa</span>
              <div class="hs-track" ref={trackRef}>
                {segs.map((s, i) => (
                  <span key={i} class={`hs-seg hs-seg--${s.tier}`} style={pos(s.start, s.end)}
                        title={`${FAIXA_WORD[s.tier]} ${s.startLocal}–${s.endLocal}${s.label ? ` · ${s.label}` : ""}`}>
                    {fits(s.start, s.end) ? s.label : ""}
                  </span>
                ))}
              </div>
            </div>
          )}

          <div class="dc-chart-row">
            <div class="dc-axis" aria-hidden="true"><span>{axisTop}</span><span>{axisMid}</span><span>{useKw || show.soc ? "0" : ""}</span></div>
            <svg class="dc-svg" viewBox={`0 0 ${FRAME_W} ${FRAME_H}`} preserveAspectRatio="none" role="img" aria-label={ariaLabel}>
              {[frame.pad, FRAME_H / 2, FRAME_H - frame.pad].map((y) => (
                <line key={y} x1="0" x2={FRAME_W} y1={y} y2={y} class="dc-grid" vector-effect="non-scaling-stroke" />
              ))}
              {show.pv && series.pvFc.length > 1 && <path d={areaPath(series.pvFc, frame, kwMax)} class="dc-pv-area" />}
              {show.pv && series.pvFc.length > 0 && <path d={linePath(series.pvFc, frame, kwMax)} class="dc-pv-fc" vector-effect="non-scaling-stroke" />}
              {show.pv && series.pvAct.length > 0 && <path d={linePath(series.pvAct, frame, kwMax)} class="dc-pv-act" vector-effect="non-scaling-stroke" />}
              {show.load && series.loadFc.length > 0 && <path d={linePath(series.loadFc, frame, kwMax)} class="dc-load-fc" vector-effect="non-scaling-stroke" />}
              {show.load && series.loadAct.length > 0 && <path d={linePath(series.loadAct, frame, kwMax)} class="dc-load-act" vector-effect="non-scaling-stroke" />}
              {show.soc && series.socPlan.length > 0 && <path d={linePath(series.socPlan, frame, 100)} class="dc-soc-plan" vector-effect="non-scaling-stroke" />}
              {show.soc && series.socAct.length > 0 && <path d={linePath(series.socAct, frame, 100)} class="dc-soc-act" vector-effect="non-scaling-stroke" />}
            </svg>
          </div>

          {heatCells.length > 0 && (
            <div class="hs-lane">
              <span class="hs-lane-label">Aquecer</span>
              <div class="hs-track">
                {heatCells.map(({ a, b, cell }, i) => (
                  <span key={i} class={`hs-seg hs-seg--heat-${cell!.mode}`} style={pos(a, b)} title={cell!.text}>{fits(a, b) ? cell!.text : ""}</span>
                ))}
              </div>
            </div>
          )}

          {water.length > 0 && (
            <div class="hs-lane">
              <span class="hs-lane-label">Água</span>
              <div class="hs-track">
                {water.map((s, i) => (
                  <span key={i} class={`hs-seg ${s.warm && !holiday.tank ? "hs-seg--water" : "hs-seg--water-idle"}`}
                        style={pos(s.start, s.end)} title={holiday.tank ? "desligado" : s.text}>
                    {fits(s.start, s.end) ? (holiday.tank ? "desligado" : s.text) : ""}
                  </span>
                ))}
              </div>
            </div>
          )}

          {isToday && nowMs >= bounds.startMs && nowMs <= bounds.endMs && (
            <span class="dc-now" style={{ "--f": nowF } as never} aria-hidden="true" />
          )}
        </div>

        <div class="dc-ruler-row">
          <span class="hs-lane-label" />
          <div class="hs-track dc-ruler">
            {ticks.map((t, i) => <span key={i} class="hs-tick" style={{ left: pct(t.f) }}>{t.label}</span>)}
          </div>
        </div>
      </div>

      {!hasAny && (
        <p class="hs-muted">
          {pv.loading || timeline.loading ? "Carregando o gráfico…" : isToday ? "Sem dados do dia ainda." : "O plano ainda não cobre amanhã."}
        </p>
      )}
      {summary && <p class="dc-summary">{summary}</p>}
      <p class="hs-caption">
        <span class="hs-caption-legend">
          Linha cheia = medido · tracejada = previsão e plano{isToday ? " · linha vertical = agora" : ""} · laranja nas faixas = aquecendo.{" "}
        </span>
        <Link href="/insights" class="hs-link">Detalhes e histórico →</Link>
      </p>
    </section>
  );
}
