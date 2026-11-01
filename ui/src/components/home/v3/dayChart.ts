// Pure logic for the Home v3 "O dia" chart and the "Agora" flow pills (#898).
// No DOM, no Preact, no value imports: unit-tested with `node --test`.
// Every value comes from the API responses typed below; nothing is invented.

// ------------------------------------------------------------ response types
export interface PvSlot {
  slot_utc: string;
  pv_forecast_kwh: number | null;     // live forecast (forward-only: 0 for elapsed slots)
  pv_planned_kwh: number | null;      // committed plan, stitched across the day's solves
  pv_actual_kwh: number | null;       // measured, null for future / no-telemetry slots
  load_forecast_kwh: number | null;   // committed total household load
}
export interface PvTodayResp { date: string; now_utc: string; slots: PvSlot[] }
export interface ExecSlot { slot_utc: string; consumption_kwh: number | null; soc_percent: number | null }
export interface ExecTodayResp { date: string; slots: ExecSlot[] }
export interface TimelineSlot { slot_time_utc: string; soc_kwh: number | null }
export interface TimelineResp {
  ok?: boolean; executed?: TimelineSlot[]; ongoing?: TimelineSlot | null; planned?: TimelineSlot[];
}

// ------------------------------------------------------------------- format (shared pt-BR formatter)
import { fmt } from "../simple/logic.ts";
import type { PeakCoverage } from "../simple/logic.ts";
export { fmt };

// -------------------------------------------------------------- local days
const SLOT_MS = 30 * 60000;

function zoneParts(ms: number, tz?: string): { y: number; m: number; d: number; h: number; mi: number } {
  const parts = new Intl.DateTimeFormat("en-GB", {
    year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
    hourCycle: "h23", timeZone: tz,
  }).formatToParts(new Date(ms));
  const g = (t: string) => Number(parts.find((p) => p.type === t)?.value ?? 0);
  return { y: g("year"), m: g("month"), d: g("day"), h: g("hour"), mi: g("minute") };
}
function zoneOffsetMs(ms: number, tz?: string): number {
  const p = zoneParts(ms, tz);
  return Date.UTC(p.y, p.m - 1, p.d, p.h, p.mi) - Math.floor(ms / 60000) * 60000;
}
const pad2 = (n: number) => String(n).padStart(2, "0");

/** YYYY-MM-DD of an instant in the house timezone. */
export function localYmd(ms: number, tz?: string): string {
  const p = zoneParts(ms, tz);
  return `${p.y}-${pad2(p.m)}-${pad2(p.d)}`;
}
export function addDaysYmd(ymd: string, n: number): string {
  const [y, m, d] = ymd.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, d + n)).toISOString().slice(0, 10);
}
function localMidnightMs(ymd: string, tz?: string): number {
  const [y, m, d] = ymd.split("-").map(Number);
  const guess = Date.UTC(y, m - 1, d);
  const t1 = guess - zoneOffsetMs(guess, tz);
  return guess - zoneOffsetMs(t1, tz);
}
/** [local midnight, next local midnight) in UTC ms (23 h / 25 h on a DST change day). */
export function localDayBounds(ymd: string, tz?: string): { startMs: number; endMs: number } {
  return { startMs: localMidnightMs(ymd, tz), endMs: localMidnightMs(addDaysYmd(ymd, 1), tz) };
}
/** The UTC calendar dates a local day touches (the "today" endpoints serve UTC days). */
export function utcDatesOf(b: { startMs: number; endMs: number }): string[] {
  const a = new Date(b.startMs).toISOString().slice(0, 10);
  const z = new Date(b.endMs - 1).toISOString().slice(0, 10);
  return a === z ? [a] : [a, z];
}
export function hhmm(ms: number, tz?: string): string {
  const p = zoneParts(ms, tz);
  return `${pad2(p.h)}:${pad2(p.mi)}`;
}

// ------------------------------------------------------------------- series
export interface Pt { t: number; v: number }
export interface DaySeries {
  pvFc: Pt[];          // kW, forecast / committed plan
  pvAct: Pt[];         // kW, measured
  socAct: Pt[];        // %, measured
  socPlan: Pt[];       // %, planned
  loadAct: Pt[];       // kW, measured
  loadFc: Pt[];        // kW, planned
  pvSoFarKwh: number | null;     // measured generation so far
  pvForecastKwh: number | null;  // whole-day forecast / plan
  pvForecastMissing: boolean;    // provider failed (backend fills 0.0): never shown as "0 sun"
  pvCmp: { actual: number; expected: number } | null;   // elapsed slots with both values
}
export interface SeriesInput {
  bounds: { startMs: number; endMs: number };
  nowMs: number;
  isToday: boolean;
  pv: PvTodayResp[];
  exec: ExecTodayResp[];
  timeline: TimelineResp | null;
  capacityKwh: number | null;
  socNowPct: number | null;
}
const MIN_DAY_FORECAST_KWH = 0.05;
const isNum = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);

export function buildDaySeries(inp: SeriesInput): DaySeries {
  const { bounds, nowMs, isToday } = inp;
  const inDay = (t: number) => t >= bounds.startMs && t < bounds.endMs;
  const out: DaySeries = {
    pvFc: [], pvAct: [], socAct: [], socPlan: [], loadAct: [], loadFc: [],
    pvSoFarKwh: null, pvForecastKwh: null, pvForecastMissing: false, pvCmp: null,
  };

  const seen = new Set<number>();
  const pvSlots = inp.pv.flatMap((r) => r.slots ?? [])
    .map((s) => ({ s, t: Date.parse(s.slot_utc) }))
    .filter((x) => Number.isFinite(x.t) && inDay(x.t) && !seen.has(x.t) && seen.add(x.t))
    .sort((a, b) => a.t - b.t);

  let fcSum = 0, fcN = 0, actSum = 0, actN = 0, cmpA = 0, cmpE = 0, cmpN = 0;
  for (const { s, t } of pvSlots) {
    const elapsed = t + SLOT_MS <= nowMs;
    const mid = t + SLOT_MS / 2;
    // The committed plan wins when present. The live forecast is forward-only (0 for elapsed
    // slots) and the backend fills 0.0 when the provider fails, so it is only the fallback.
    const fc = s.pv_planned_kwh ?? (elapsed ? null : s.pv_forecast_kwh);
    if (isNum(fc)) { out.pvFc.push({ t: mid, v: fc * 2 }); fcSum += fc; fcN++; }
    if (elapsed && isNum(s.pv_actual_kwh)) {
      out.pvAct.push({ t: mid, v: s.pv_actual_kwh * 2 });
      actSum += s.pv_actual_kwh; actN++;
      if (isNum(s.pv_planned_kwh)) { cmpA += s.pv_actual_kwh; cmpE += s.pv_planned_kwh; cmpN++; }
    }
    const lf = s.load_forecast_kwh;
    if (isNum(lf) && (!isToday || mid > nowMs)) out.loadFc.push({ t: mid, v: lf * 2 });
  }
  if (fcN > 0 && fcSum < MIN_DAY_FORECAST_KWH) {
    out.pvFc = [];                       // an all-zero day forecast is a failed provider, not a dark day
    out.pvForecastMissing = true;
  } else if (fcN > 0) out.pvForecastKwh = fcSum;
  if (isToday && actN > 0) out.pvSoFarKwh = actSum;
  if (cmpN > 0) out.pvCmp = { actual: cmpA, expected: cmpE };

  if (isToday) {
    const seenE = new Set<number>();
    const ex = inp.exec.flatMap((r) => r.slots ?? [])
      .map((s) => ({ s, t: Date.parse(s.slot_utc) }))
      .filter((x) => Number.isFinite(x.t) && inDay(x.t) && x.t + SLOT_MS <= nowMs && !seenE.has(x.t) && seenE.add(x.t))
      .sort((a, b) => a.t - b.t);
    for (const { s, t } of ex) {
      if (isNum(s.soc_percent)) out.socAct.push({ t: t + SLOT_MS, v: s.soc_percent });
      if (isNum(s.consumption_kwh) && s.consumption_kwh > 0) out.loadAct.push({ t: t + SLOT_MS / 2, v: s.consumption_kwh * 2 });
    }
    if (out.loadAct.length && out.loadFc.length) out.loadFc.unshift({ t: nowMs, v: out.loadAct[out.loadAct.length - 1].v });
  }

  // Planned battery: the plan's soc_kwh (end of slot) as % of the pack. Only what exists.
  const cap = inp.capacityKwh;
  const tl = inp.timeline;
  if (tl && tl.ok !== false && isNum(cap) && cap > 0) {
    const all = [...(tl.executed ?? []), ...(tl.ongoing ? [tl.ongoing] : []), ...(tl.planned ?? [])];
    const seenT = new Set<number>();
    const pts = all
      .map((s) => ({ t: Date.parse(s.slot_time_utc) + SLOT_MS, soc: s.soc_kwh }))
      .filter((x) => Number.isFinite(x.t) && isNum(x.soc) && x.t > bounds.startMs && x.t <= bounds.endMs
        && (!isToday || x.t > nowMs) && !seenT.has(x.t) && seenT.add(x.t))
      .sort((a, b) => a.t - b.t);
    for (const p of pts) out.socPlan.push({ t: p.t, v: Math.min(100, Math.max(0, (p.soc as number) / cap * 100)) });
    if (isToday && out.socPlan.length && isNum(inp.socNowPct)) out.socPlan.unshift({ t: nowMs, v: inp.socNowPct });
  }
  return out;
}

// -------------------------------------------------------------------- paths
export interface Frame { w: number; h: number; pad: number; startMs: number; endMs: number }
export const xOf = (f: Frame, t: number) => ((t - f.startMs) / (f.endMs - f.startMs)) * f.w;
export const yOf = (f: Frame, v: number, max: number) => f.h - f.pad - (Math.min(v, max) / max) * (f.h - 2 * f.pad);

/** SVG path; a gap longer than `maxGapMs` between points starts a new sub-path. */
export function linePath(pts: Pt[], f: Frame, max: number, maxGapMs = 90 * 60000): string {
  let d = "";
  let prev: number | null = null;
  for (const p of pts) {
    const cmd = prev == null || p.t - prev > maxGapMs ? "M" : "L";
    d += `${d ? " " : ""}${cmd}${xOf(f, p.t).toFixed(1)},${yOf(f, p.v, max).toFixed(1)}`;
    prev = p.t;
  }
  return d;
}
export function areaPath(pts: Pt[], f: Frame, max: number): string {
  if (pts.length < 2) return "";
  const base = (f.h - f.pad).toFixed(1);
  const first = xOf(f, pts[0].t).toFixed(1);
  const last = xOf(f, pts[pts.length - 1].t).toFixed(1);
  return `${linePath(pts, f, max, Infinity)} L${last},${base} L${first},${base} Z`;
}
/** Rounded-up whole-kW ceiling of the visible kW series (min 1 kW). */
export function kwCeil(series: Pt[][]): number {
  const m = Math.max(0, ...series.flat().map((p) => p.v));
  return Math.max(1, Math.ceil(m));
}
export function valueAt(pts: Pt[], t: number): number | null {
  if (pts.length === 0 || t < pts[0].t || t > pts[pts.length - 1].t) return null;
  for (let i = 1; i < pts.length; i++) {
    if (t <= pts[i].t) {
      const a = pts[i - 1], b = pts[i];
      return b.t === a.t ? b.v : a.v + ((b.v - a.v) * (t - a.t)) / (b.t - a.t);
    }
  }
  return null;
}

// ------------------------------------------------------------------ summary
export interface PeakInfo { label: string; coverage: PeakCoverage | null }
const FULL_PCT = 98;
const COVER_SLACK_MS = 60 * 60000;

/** Does the plan trajectory reach (about) the end of the day? */
export function planCoversDay(plan: Pt[], endMs: number): boolean {
  return plan.length > 0 && plan[plan.length - 1].t >= endMs - COVER_SLACK_MS;
}

/** One plain-language sentence group from real data. Missing facts are skipped.
 *  The peak clause uses `peakCoverage()` from logic.ts (same source as the status line),
 *  carries no kWh (the status line owns it) and never states the plan as fact for the past. */
export function daySummary(i: { isToday: boolean; series: DaySeries; peak: PeakInfo | null; endMs: number; tz?: string }): string {
  const out: string[] = [];
  const s = i.series;
  if (i.isToday && s.pvCmp && s.pvCmp.expected >= 0.5) {
    const r = s.pvCmp.actual / s.pvCmp.expected;
    out.push(r >= 1.1 ? "O sol rendeu mais que o previsto." : r >= 0.9 ? "O sol rendeu quase o previsto." : "O sol rendeu menos que o previsto.");
  }
  const plan = s.socPlan;
  if (plan.length > 0) {
    const full = plan.find((p) => p.v >= FULL_PCT);
    if (i.isToday && plan[0].v >= FULL_PCT) {
      out.push("A bateria já está cheia.");
    } else if (full) {
      out.push(`A bateria ${i.isToday ? "completa" : "deve completar"} às ${hhmm(full.t, i.tz)}.`);
    } else if (planCoversDay(plan, i.endMs)) {
      const top = plan.reduce((a, b) => (b.v > a.v ? b : a));
      out.push(`A bateria ${i.isToday ? "chega" : "deve chegar"} a ${Math.round(top.v)} % às ${hhmm(top.t, i.tz)}.`);
    } else {
      out.push(`O plano da bateria vai até ${hhmm(plan[plan.length - 1].t, i.tz)}.`);
    }
  }
  const c = i.peak?.coverage;
  if (i.peak && c) {
    const lead = `No pico (${i.peak.label})`;
    if (c.kind === "bought") out.push(`${lead} a casa já comprou da rede.`);
    else if (c.kind === "buys") out.push(`${lead} o plano compra da rede.`);
    else out.push(`${lead} ${i.isToday ? "a bateria segura" : "a bateria deve segurar"} a casa sem comprar da rede.`);
  }
  return out.join(" ");
}

// ------------------------------------------------------- bands (tap to read)
export interface BandInfo {
  startLocal: string; endLocal: string; tier: "cheap" | "standard" | "peak"; price: string | null;
  heat: { mode: "boost" | "hold" | "off"; text: string } | null; water: string | null;
}
const TIER_WORD = { cheap: "barata", standard: "dia", peak: "pico" } as const;
const hourWord2 = (h: string) => (h.endsWith(":00") ? h.slice(0, 2) : h);
export function bandRange(b: { startLocal: string; endLocal: string }): string {
  return `${hourWord2(b.startLocal)}–${hourWord2(b.endLocal)}`;
}
/** "16–19 · pico 38,2p · aquecimento desligado · água 37 °C (economia)". Real text for
 *  screen readers and touch (a segment's `title` does neither). */
export function bandDetail(b: BandInfo): string {
  const out = [bandRange(b), `${TIER_WORD[b.tier]}${b.price ? ` ${b.price}` : ""}`];
  if (b.heat) {
    const t = b.heat.text;
    out.push(b.heat.mode === "off" ? "aquecimento desligado"
      : b.heat.mode === "boost" ? `aquecimento ${t.replace(/^aquece\s*/, "") || "ligado"}`
      : `aquecimento ${t}`);
  }
  if (b.water) out.push(b.water === "desligado" ? "água desligada" : `água ${b.water}`);
  return out.join(" · ");
}

/** Past UTC days never change: fetch once, keep for the session. The current UTC day always refetches. */
export async function cachedPast<T>(cache: Map<string, T>, key: string, isPast: boolean, fetcher: () => Promise<T>): Promise<T> {
  if (isPast && cache.has(key)) return cache.get(key) as T;
  const r = await fetcher();
  if (isPast) cache.set(key, r);
  return r;
}

/** First plan point at/over the "full" mark, for the Amanhã header. */
export function fullAt(series: DaySeries): number | null {
  return series.socPlan.find((p) => p.v >= FULL_PCT)?.t ?? null;
}

// ---------------------------------------------------------------- flow pills
export type FlowKey = "sol" | "rede" | "casa" | "bateria";
export interface FlowPill { key: FlowKey; label: string; kw: number }
export interface FlowModel { sources: FlowPill[]; sinks: FlowPill[] }
export interface FlowState {
  solar_kw?: number | null; grid_kw?: number | null; load_kw?: number | null; battery_kw?: number | null;
}
const DEAD_KW = 0.05;

/** Sources "Entra" (sol, rede importando, bateria descarregando) and destinations
 *  "Vai para" (casa, bateria carregando, rede exportando) from the live balance.
 *  grid_kw > 0 = import; battery_kw > 0 = charging. A null reading drops that pill. */
export function flowModel(s: FlowState | null | undefined): FlowModel {
  const m: FlowModel = { sources: [], sinks: [] };
  if (!s) return m;
  if (isNum(s.solar_kw)) m.sources.push({ key: "sol", label: "Sol", kw: Math.max(0, s.solar_kw) });
  if (isNum(s.grid_kw) && s.grid_kw > DEAD_KW) m.sources.push({ key: "rede", label: "Rede importa", kw: s.grid_kw });
  if (isNum(s.battery_kw) && s.battery_kw < -DEAD_KW) m.sources.push({ key: "bateria", label: "Bateria descarrega", kw: -s.battery_kw });
  if (isNum(s.load_kw)) m.sinks.push({ key: "casa", label: "Casa", kw: Math.max(0, s.load_kw) });
  if (isNum(s.battery_kw) && s.battery_kw > DEAD_KW) m.sinks.push({ key: "bateria", label: "Bateria carrega", kw: s.battery_kw });
  if (isNum(s.grid_kw) && s.grid_kw < -DEAD_KW) m.sinks.push({ key: "rede", label: "Rede exporta", kw: -s.grid_kw });
  return m;
}
const words = (ps: FlowPill[]) => ps.map((p) => p.key).join(" + ");
/** "Entra: sol + rede · vai para: casa + bateria." (no figures: they are in the pills). */
export function flowCaption(m: FlowModel): string {
  if (m.sources.length === 0 && m.sinks.length === 0) return "";
  return `Entra: ${words(m.sources) || "—"} · vai para: ${words(m.sinks) || "—"}.`;
}
