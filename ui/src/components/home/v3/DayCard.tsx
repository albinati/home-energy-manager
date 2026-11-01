import { useEffect, useMemo, useRef, useState } from "preact/hooks";
import { Link } from "wouter-preact";
import type { CockpitNow, PlanFrontsResponse } from "../../../lib/types";
import { usePoll } from "../../../lib/poll";
import { getPlanFronts, getPvToday, getExecutionToday, getSchedulerTimeline } from "../../../lib/endpoints";
import {
  frac, tariffSegments, heatingCell, waterSegments, hourTicks, hourWord, tierOf, peakSpans, upcomingPeakSpan, span, peakCoverage, pp,
  FAIXA_WORD, type HolidayFlags, type Axis,
} from "../simple/logic.ts";
import {
  localYmd, addDaysYmd, localDayBounds, utcDatesOf, hhmm, buildDaySeries, linePath, areaPath, kwCeil,
  daySummary, fullAt, fmt, bandDetail, bandRange, cachedPast, isPastUtcDate,
  type Frame, type PeakInfo, type Pt, type PvTodayResp, type ExecTodayResp,
} from "./dayChart";
import "../../../styles/home-v3-day.css";

// Dev-only fixture switch (`?mock=1`); eliminated from the production bundle.
const MOCK = import.meta.env.DEV && new URLSearchParams(location.search).get("mock") === "1";

// A segment shows its text only when it is at least this wide; the full text is always
// available as real text in the band detail line below the lanes (title does not work on touch).
const MIN_TEXT_PX = 56;
const FRAME_H = 200;
const FRAME_W = 960;

type DayKey = "hoje" | "amanha";

// Past UTC days never change: cache them for the session (BST local days span two UTC dates).
const pvPast = new Map<string, PvTodayResp>();
const execPast = new Map<string, ExecTodayResp>();

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
  const [picked, setPicked] = useState<{ ymd: string; start: string } | null>(null);
  const isToday = day === "hoje";
  const todayYmd = localYmd(nowMs, tz);
  const ymd = isToday ? todayYmd : addDaysYmd(todayYmd, 1);
  const bounds = useMemo(() => localDayBounds(ymd, tz), [ymd, tz]);
  const dates = utcDatesOf(bounds);
  const dkey = dates.join(",");

  // Every response is stored under the key it was requested for and read back by the CURRENT key,
  // so a late reply for the other day can never render (and never blanks the right one).
  const pvStore = useRef(new Map<string, PvTodayResp[]>());
  const execStore = useRef(new Map<string, ExecTodayResp[]>());
  const frontsStore = useRef(new Map<string, PlanFrontsResponse>());

  const pv = usePoll(async () => {
    const r = await (import.meta.env.DEV && MOCK
      ? (await import("../../../lib/mock/day")).mockPvToday(dates)
      : Promise.all(dates.map((d) => cachedPast(pvPast, d, isPastUtcDate(d, nowMs), () => getPvToday(d)))));
    pvStore.current.set(dkey, r);
    return null;
  }, 120_000, [dkey]);
  const exec = usePoll(async () => {
    if (!isToday) return null;
    const r = await (import.meta.env.DEV && MOCK
      ? (await import("../../../lib/mock/day")).mockExecToday(dates)
      : Promise.all(dates.map((d) => cachedPast(execPast, d, isPastUtcDate(d, nowMs), () => getExecutionToday(d)))));
    execStore.current.set(dkey, r);
    return null;
  }, 120_000, [dkey, isToday]);
  const timeline = usePoll(async () => {
    if (import.meta.env.DEV && MOCK) return (await import("../../../lib/mock/day")).mockTimeline();
    return getSchedulerTimeline();
  }, 120_000);
  const tomorrowFronts = usePoll(async () => {
    if (isToday) return null;
    const r = await (import.meta.env.DEV && MOCK
      ? (await import("../../../lib/mock/day")).mockFrontsTomorrow()
      : getPlanFronts(ymd));
    frontsStore.current.set(ymd, r);
    return null;
  }, 60_000, [ymd, isToday]);

  const pvData = pvStore.current.get(dkey) ?? null;
  const execData = isToday ? (execStore.current.get(dkey) ?? null) : [];
  const dayFronts = isToday ? fronts : (frontsStore.current.get(ymd) ?? null);
  const dataLoading = pvData == null && !pv.error;
  const frontsLoading = !isToday && dayFronts == null && !tomorrowFronts.error;
  // Re-render marker only: the stores above hold the data; `pv.data` etc. are null on purpose.
  void tomorrowFronts.lastFetchAt; void exec.lastFetchAt;

  const st = now.state;
  const capacityKwh = (fronts?.battery && !fronts.battery.error ? fronts.battery.capacity_kwh : null)
    ?? (st.soc_pct > 5 && st.soc_kwh > 0 ? (st.soc_kwh / st.soc_pct) * 100 : null);

  const series = useMemo(() => buildDaySeries({
    bounds, nowMs, isToday, pv: pvData ?? [], exec: execData ?? [], timeline: timeline.data,
    capacityKwh, socNowPct: Number.isFinite(st.soc_pct) ? st.soc_pct : null,
  }), [bounds, nowMs, isToday, pvData, execData, timeline.data, capacityKwh, st.soc_pct]);

  const tariff = dayFronts?.tariff && !dayFronts.tariff.error ? dayFronts.tariff : null;
  const axis: Axis = { startMs: bounds.startMs, endMs: bounds.endMs };
  const peakWin = upcomingPeakSpan(tariff, nowMs) ?? peakSpans(tariff?.windows)[0] ?? null;
  const peak: PeakInfo | null = peakWin
    ? { label: span(peakWin.start_local, peakWin.end_local), coverage: peakCoverage(dayFronts?.battery, tariff, nowMs) }
    : null;
  const summary = daySummary({ isToday, series, peak, endMs: bounds.endMs, tz });

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
  const fcValue = series.pvForecastKwh != null ? fmt(series.pvForecastKwh, 1) : "—";
  const metrics: Array<{ label: string; value: string; unit?: string }> = isToday
    ? [
      { label: "Sol gerado até agora", value: series.pvSoFarKwh != null ? fmt(series.pvSoFarKwh, 1) : "—", unit: "kWh" },
      { label: "Previsto no dia", value: fcValue, unit: "kWh" },
    ]
    : [
      { label: "Sol previsto amanhã", value: fcValue, unit: "kWh" },
      ...(fullAt(series) != null ? [{ label: "Bateria cheia às", value: hhmm(fullAt(series) as number, tz) }] : []),
    ];
  const ariaLabel = `${isToday ? "Hoje" : "Amanhã"}: ${metrics.map((m) => `${m.label.toLowerCase()} ${m.value}${m.unit ? ` ${m.unit}` : ""}`).join("; ")}.${series.pvForecastMissing ? " Previsão do sol indisponível." : ""}${summary ? ` ${summary}` : ""}`;

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
    .map(({ w, a, b }) => ({ a, b, cell: heatingCell(w, dayFronts?.heating, holiday.climate, tariff?.structure) }))
    .filter((x) => x.cell != null);

  // ---- bands: one selectable column per tariff window, full text as real text below
  const bands = wins.map(({ w, a, b }) => {
    const mid = (Date.parse(w.start_utc) + Date.parse(w.end_utc)) / 2;
    const ws = water.find((s) => mid >= s.startMs && mid < s.endMs);
    const heat = heatingCell(w, dayFronts?.heating, holiday.climate, tariff?.structure);
    const isCur = nowMs >= Date.parse(w.start_utc) && nowMs < Date.parse(w.end_utc);
    const info = {
      startLocal: w.start_local, endLocal: w.end_local, tier: tierOf(w),
      price: w.price_p != null ? pp(w.price_p) : null,
      heat, water: holiday.tank ? "desligado" : (ws ? ws.text : null),
    };
    return { key: w.start_utc, a, b, w, text: bandDetail(info, isToday && isCur), range: bandRange(info), tier: info.tier };
  });
  const curIdx = bands.findIndex((x) => nowMs >= Date.parse(x.w.start_utc) && nowMs < Date.parse(x.w.end_utc));
  const defaultKey = bands.length ? bands[isToday && curIdx >= 0 ? curIdx : 0].key : null;
  const selKey = picked && picked.ymd === ymd && bands.some((x) => x.key === picked.start) ? picked.start : defaultKey;
  const selected = bands.find((x) => x.key === selKey) ?? null;
  const pick = (key: string) => setPicked({ ymd, start: key });

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

  const emptyNote = dataLoading || frontsLoading || timeline.loading
    ? "Carregando o gráfico…"
    : isToday ? "Sem dados do dia ainda." : "O plano ainda não cobre amanhã.";

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
      {series.pvForecastMissing && <p class="hs-muted">Previsão do sol indisponível.</p>}

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

          {/* Pointer convenience only: click/tap a column to select its band. The accessible
              controls are the band buttons below. */}
          {bands.length > 0 && (
            <div class="dc-cols" aria-hidden="true">
              {bands.map((x) => (
                <div key={x.key} class={`dc-col${x.key === selKey ? " is-sel" : ""}`}
                     style={{ left: pct(x.a), width: pct(x.b - x.a) }} onClick={() => pick(x.key)} />
              ))}
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

      {bands.length > 0 && (
        <div class="dc-bands" role="group" aria-label="Faixas do dia">
          {bands.map((x) => (
            <button key={x.key} type="button" class={`dc-band dc-band--${x.tier}`} aria-pressed={x.key === selKey}
                    aria-label={x.text} onClick={() => pick(x.key)}>
              {x.range}
            </button>
          ))}
        </div>
      )}
      {selected && <p class="dc-band-detail" aria-live="polite">{selected.text}</p>}

      {!hasAny && <p class="hs-muted">{emptyNote}</p>}
      {summary && <p class="dc-summary">{summary}</p>}
      <p class="hs-caption">
        <span class="dc-legend">
          Linha cheia = medido · tracejada = previsão e plano{isToday ? " · linha vertical = agora" : ""} · laranja nas faixas = aquecendo.{" "}
        </span>
        <Link href="/insights" class="hs-link">Detalhes e histórico →</Link>
      </p>
    </section>
  );
}
