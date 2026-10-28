// Pure helpers for the simplified Home (#879). No DOM, no Preact, no value
// imports: this file is unit-tested with `node --test` (type stripping), so
// keep it dependency-free. All copy is pt-BR.
import type {
  CockpitNow,
  CockpitVacation,
  PlanFrontsBattery,
  PlanFrontsBilling,
  PlanFrontsHeating,
  PlanFrontsTank,
  PlanFrontsTariff,
  PlanFrontsTariffWindow,
  PlanFrontsSpend,
  PlanFrontsResponse,
} from "../../../lib/types";

// ---------------------------------------------------------------- formatting
/** pt-BR decimal: 6.2 -> "6,2". */
export function fmt(n: number, d = 1): string {
  return n.toFixed(d).replace(".", ",");
}
/** Integer when whole, else one decimal: 21 -> "21", 20.5 -> "20,5". */
export function num(n: number): string {
  return Number.isInteger(n) ? String(n) : fmt(n, 1);
}
/** Pence per kWh, ONE format everywhere: 12.49 -> "12,5p". */
export function pp(n: number): string {
  return `${fmt(Math.round(n * 10 + 1e-6) / 10, 1)}p`;   // 25.45 -> "25,5p" (toFixed gives 25,4)
}
export function gbp(n: number): string {
  return `£${fmt(Math.abs(n), 2)}`;
}
export function signedGbp(n: number): string {
  return `${n < 0 ? "−" : "+"}${gbp(n)}`;
}
/** "HH:MM" -> minutes since local midnight ("24:00" -> 1440). */
export function minutesOf(hhmm: string): number {
  const [h, m] = hhmm.split(":").map(Number);
  return (h || 0) * 60 + (m || 0);
}
/** "16:00" -> "16"; "04:30" -> "04:30". */
export function hourWord(hhmm: string): string {
  return hhmm.endsWith(":00") ? hhmm.slice(0, 2) : hhmm;
}
export function span(startLocal: string, endLocal: string): string {
  return `${hourWord(startLocal)}–${hourWord(endLocal)}`;
}
/** Minutes since local midnight of an instant, in the house timezone. */
export function localMinutes(iso: string, tz?: string): number {
  const d = new Date(iso);
  try {
    const parts = new Intl.DateTimeFormat("en-GB", {
      hour: "2-digit", minute: "2-digit", hourCycle: "h23", timeZone: tz,
    }).formatToParts(d);
    const h = Number(parts.find((p) => p.type === "hour")?.value ?? 0);
    const m = Number(parts.find((p) => p.type === "minute")?.value ?? 0);
    return h * 60 + m;
  } catch {
    return d.getHours() * 60 + d.getMinutes();
  }
}
export function localHHMM(iso: string, tz?: string): string {
  const mm = localMinutes(iso, tz);
  return `${String(Math.floor(mm / 60)).padStart(2, "0")}:${String(mm % 60).padStart(2, "0")}`;
}

// --------------------------------------------------------------------- tiers
export type Tier = "cheap" | "standard" | "peak";
export const TIER_WORD: Record<Tier, string> = { cheap: "barato", standard: "dia", peak: "pico" };

/** Band tier from the backend key/label (works for banded and for the dynamic
 *  cheap/standard/peak tiers; a negative-price tier reads as cheap). */
export function tierOf(w: { key?: string | null; label?: string | null }): Tier {
  const s = `${w.key ?? ""} ${w.label ?? ""}`.toLowerCase();
  if (s.includes("peak") || s.includes("pico")) return "peak";
  if (s.includes("cheap") || s.includes("neg") || s.includes("barato")) return "cheap";
  return "standard";
}

/** Is a local minute-of-day inside the LP night window (wraps midnight)? */
export function inNightWindow(minute: number, startHour = 22, endHour = 7): boolean {
  const s = startHour * 60, e = endHour * 60;
  return s > e ? minute >= s || minute < e : minute >= s && minute < e;
}
export function isNightStart(startLocal: string, h?: PlanFrontsHeating | null): boolean {
  return inNightWindow(minutesOf(startLocal), h?.night_start_hour ?? 22, h?.night_end_hour ?? 7);
}

// ----------------------------------------------------------------- day axis
export interface Axis { startMs: number; endMs: number }
/** The day axis = the span covered by the tariff windows (they tile the day;
 *  23 h / 25 h on a DST change day). */
export function dayAxis(tariff: PlanFrontsTariff | null | undefined): Axis | null {
  const ws = tariff?.windows ?? [];
  if (ws.length === 0) return null;
  const startMs = Math.min(...ws.map((w) => Date.parse(w.start_utc)));
  const endMs = Math.max(...ws.map((w) => Date.parse(w.end_utc)));
  return endMs > startMs ? { startMs, endMs } : null;
}
export function frac(axis: Axis, ms: number): number {
  return Math.min(1, Math.max(0, (ms - axis.startMs) / (axis.endMs - axis.startMs)));
}
/** Axis fraction of each local wall-clock hour (scans the real axis in 15-min
 *  steps, so a DST day is placed correctly). Hours not on the axis are skipped. */
export function hourTicks(axis: Axis, tz: string | undefined, hours: number[]): Array<{ f: number; hour: number }> {
  const out: Array<{ f: number; hour: number }> = [];
  const step = 15 * 60000;
  for (const h of hours) {
    for (let ms = axis.startMs; ms < axis.endMs; ms += step) {
      if (localMinutes(new Date(ms).toISOString(), tz) === h * 60) {
        out.push({ f: frac(axis, ms), hour: h });
        break;
      }
    }
  }
  return out;
}

/** Merge adjacent items (already time-sorted) when `same` says so. */
export function mergeRuns<T extends { start: number; end: number }>(
  items: T[],
  same: (a: T, b: T) => boolean,
): T[] {
  const out: T[] = [];
  for (const it of items) {
    const last = out[out.length - 1];
    if (last && same(last, it) && it.start <= last.end + 1e-6) {
      last.end = Math.max(last.end, it.end);
    } else {
      out.push({ ...it });
    }
  }
  return out;
}

// -------------------------------------------------------------- lane: tarifa
export interface TariffSeg {
  tier: Tier; start: number; end: number; startLocal: string; endLocal: string;
  price: number | null; label: string;
}
export function tariffSegments(tariff: PlanFrontsTariff | null | undefined, axis: Axis | null): TariffSeg[] {
  if (!tariff || !axis) return [];
  const sorted = [...tariff.windows].sort((a, b) => Date.parse(a.start_utc) - Date.parse(b.start_utc));
  const segs: TariffSeg[] = sorted.map((w) => ({
    tier: tierOf(w),
    start: frac(axis, Date.parse(w.start_utc)),
    end: frac(axis, Date.parse(w.end_utc)),
    startLocal: w.start_local, endLocal: w.end_local,
    price: w.price_p,
    label: w.price_p != null ? pp(w.price_p) : "",
  }));
  // Dynamic tariffs can hand back consecutive windows of one tier: show a run.
  return mergeRuns(segs, (a, b) => a.tier === b.tier && a.price != null && b.price != null
    && Math.abs(a.price - b.price) < 0.5);
}

// ---------------------------------------------------------------- lane: rede
export function gridLabel(
  w: PlanFrontsTariffWindow,
  battery: PlanFrontsBattery | null | undefined,
): string | null {
  if (!battery || battery.error) return null;
  const row = battery.by_band?.find((r) => r.start_local === w.start_local && r.end_local === w.end_local);
  if (!row) return null;
  const planned = row.planned_import_kwh;
  const real = row.realised_import_kwh;
  const peak = tierOf(w) === "peak";
  if (peak && (planned ?? 0) <= 0.05) {
    return (real ?? 0) > 0.05 ? `${fmt(real!)} (meta 0)` : "0 (meta)";
  }
  if (w.status === "upcoming") {
    return planned == null ? null : planned <= 0.05 ? "sem compra" : `plano ${fmt(planned)}`;
  }
  if (planned == null) return real == null ? null : `${fmt(real)} kWh`;
  return `${fmt(real ?? 0)} de ${fmt(planned)} kWh`;
}

// --------------------------------------------------------- lane: aquecimento
export type HeatMode = "boost" | "hold" | "off";
export interface HeatCell { mode: HeatMode; text: string }
const MIN_BOOST_OVERLAP_MS = 30 * 60000;

/** Heating word for one tariff band from the WRITTEN plan. `off` = climate
 *  holiday on the Daikin (not "vacation preset"). */
export function heatingCell(
  w: PlanFrontsTariffWindow,
  heating: PlanFrontsHeating | null | undefined,
  off: boolean,
): HeatCell | null {
  if (off) return { mode: "off", text: "desligado" };
  if (!heating || heating.error) return null;
  const a = Date.parse(w.start_utc), b = Date.parse(w.end_utc);
  let boost: number | null = null;   // best positive offset overlapping >= 30 min
  let coastMs = 0;
  for (const hw of heating.windows ?? []) {
    const ov = Math.min(b, Date.parse(hw.end_utc)) - Math.max(a, Date.parse(hw.start_utc));
    if (ov <= 0) continue;
    const off2 = hw.offset_c ?? 0;
    if (off2 > 0 && ov >= MIN_BOOST_OVERLAP_MS) boost = Math.max(boost ?? 0, off2);
    else if (off2 < 0 || hw.kind === "setback") coastMs += ov;
  }
  if (boost != null) return { mode: "boost", text: `aquece +${num(Math.round(boost))}` };
  const row = heating.by_band?.find((r) => r.start_local === w.start_local && r.end_local === w.end_local);
  if (coastMs >= (b - a) * 0.5 || row?.offset_mode === "setback") return { mode: "off", text: "desligado" };
  if (row?.offset_mode === "boost") return { mode: "boost", text: "aquece" };
  if (row && row.indoor_min_c != null && row.indoor_max_c != null) {
    return { mode: "hold", text: `mantém ~${num(Math.round((row.indoor_min_c + row.indoor_max_c) / 2))}` };
  }
  return { mode: "hold", text: "mantém" };
}

// ------------------------------------------------------------- lane: água
export interface WaterSeg { start: number; end: number; startMs: number; endMs: number; kind: string; target: number | null; text: string; warm: boolean }
export function waterLabel(kind: string, target: number | null): string {
  const tt = target != null ? `${num(Math.round(target))} °C` : "";
  switch (kind) {
    case "legionella": return `${tt || "60 °C"} legionella`;
    case "setback": return `${tt} (economia)`.trim();
    case "boost": return `${tt} (carga barata)`.trim();
    case "warmup": return `${tt} (aquece)`.trim();
    default: return tt;
  }
}
export function waterSegments(tank: PlanFrontsTank | null | undefined, axis: Axis | null): WaterSeg[] {
  if (!tank || tank.error || !axis) return [];
  const raw = [...(tank.windows ?? [])]
    .sort((a, b) => Date.parse(a.start_utc) - Date.parse(b.start_utc))
    .map((w) => {
      const startMs = Date.parse(w.start_utc), endMs = Date.parse(w.end_utc);
      return {
        start: frac(axis, startMs), end: frac(axis, endMs), startMs, endMs,
        kind: w.kind, target: w.tank_target_c, text: waterLabel(w.kind, w.tank_target_c), warm: w.kind !== "setback",
      };
    })
    .filter((s) => s.end - s.start > 0.004);
  const merged = mergeRuns(raw, (a, b) => a.kind === b.kind && a.target === b.target);
  return merged.map((m) => ({ ...m, startMs: axisMs(axis, m.start), endMs: axisMs(axis, m.end) }));
}
function axisMs(axis: Axis, f: number): number {
  return axis.startMs + f * (axis.endMs - axis.startMs);
}

// ------------------------------------------------------------- now / bands
export function currentWindow(tariff: PlanFrontsTariff | null | undefined, nowMs: number): PlanFrontsTariffWindow | null {
  return tariff?.windows?.find((w) => nowMs >= Date.parse(w.start_utc) && nowMs < Date.parse(w.end_utc)) ?? null;
}
/** First window after `nowMs` whose tier differs from the current one. */
export function nextTransition(tariff: PlanFrontsTariff | null | undefined, nowMs: number): PlanFrontsTariffWindow | null {
  const cur = currentWindow(tariff, nowMs);
  const curTier = cur ? tierOf(cur) : null;
  const ws = [...(tariff?.windows ?? [])].sort((a, b) => Date.parse(a.start_utc) - Date.parse(b.start_utc));
  return ws.find((w) => Date.parse(w.start_utc) > nowMs && tierOf(w) !== curTier) ?? null;
}

export type PowerSource = "bateria" | "sol" | "rede";
/** null = unknown (no Fox state) or nothing moving: say nothing rather than guess. */
export function powerSource(s: { grid_kw?: number; battery_kw?: number; solar_kw?: number } | undefined | null): PowerSource | null {
  if (!s || (s.grid_kw == null && s.battery_kw == null && s.solar_kw == null)) return null;
  if ((s.grid_kw ?? 0) > 0.3) return "rede";
  if ((s.battery_kw ?? 0) < -0.1) return "bateria";
  if ((s.solar_kw ?? 0) > 0.1) return "sol";
  return null;
}

export interface PeakCoverage {
  kind: "covered" | "buys" | "bought";
  until: string;
  kwh: number;
}
/** Does the battery carry the house through the peak? Only claimed with a real
 *  basis (non-null planned import, SoC at entry and at exit above the reserve). */
export function peakCoverage(
  battery: PlanFrontsBattery | null | undefined,
  tariff: PlanFrontsTariff | null | undefined,
  nowMs: number,
): PeakCoverage | null {
  const peak = [...(tariff?.windows ?? [])]
    .sort((a, b) => Date.parse(a.start_utc) - Date.parse(b.start_utc))
    .find((w) => tierOf(w) === "peak" && Date.parse(w.end_utc) > nowMs);
  if (!peak || !battery || battery.error) return null;
  const rows = [...(battery.by_band ?? [])].sort((a, b) => minutesOf(a.start_local) - minutesOf(b.start_local));
  const i = rows.findIndex((r) => r.start_local === peak.start_local && r.end_local === peak.end_local);
  if (i < 0) return null;
  const row = rows[i];
  const planned = row.planned_import_kwh;
  if (planned == null) return null;
  const inside = nowMs >= Date.parse(peak.start_utc);
  const real = row.realised_import_kwh ?? 0;
  if (inside && real > 0.1) return { kind: "bought", until: peak.end_local, kwh: real };
  if (planned > 0.1) return { kind: "buys", until: peak.end_local, kwh: planned };
  const reserve = battery.reserve_pct;
  if (reserve == null || row.soc_entry_pct == null || row.soc_entry_pct <= reserve) return null;
  const after = rows[i + 1];
  if (after && (after.soc_entry_pct == null || after.soc_entry_pct <= reserve)) return null;
  return { kind: "covered", until: peak.end_local, kwh: 0 };
}

// ---------------------------------------------------------------- holidays
export interface HolidayFlags { climate: boolean; tank: boolean }
/** Onecta holiday per management point (#872). The vacation PRESET alone does
 *  not mean the Daikin is off (holiday may be disabled or not yet sent). */
export function holidayFlags(now: CockpitNow): HolidayFlags {
  const h = now.daikin?.holiday;
  return { climate: h?.climate === true, tank: h?.tank === true };
}
export const isAway = (now: CockpitNow): boolean => !!now.vacation?.active;

export function weekdayTime(iso: string, tz?: string): string {
  try {
    const d = new Date(iso);
    const wd = d.toLocaleDateString("pt-BR", { weekday: "short", timeZone: tz }).replace(".", "");
    return `${wd} ${localHHMM(iso, tz)}`;
  } catch { return iso; }
}

function hasGridCharge(b: PlanFrontsBattery | null | undefined, nowMs: number): boolean {
  return !!b && !b.error && (b.windows ?? []).some((w) => w.kind === "grid_charge" && Date.parse(w.end_utc) > nowMs);
}

// ---------------------------------------------------------- status sentence
export interface StatusCtx { now: CockpitNow; fronts: PlanFrontsResponse | null }

export function buildStatus({ now, fronts }: StatusCtx): string {
  const tz = now.planner_tz;
  const nowMs = Date.parse(now.now_utc);
  const vac = now.vacation;
  const hol = holidayFlags(now);
  const battery = fronts?.battery && !fronts.battery.error ? fronts.battery : null;
  const tariff = fronts?.tariff && !fronts.tariff.error ? fronts.tariff : null;
  const holWord = hol.climate && hol.tank ? "Daikin em holiday"
    : hol.climate ? "Daikin em holiday (aquecimento)"
    : hol.tank ? "Daikin em holiday (água quente)" : null;
  if (vac?.active) {
    const parts = [vac.return_at ? `Fora até ${weekdayTime(vac.return_at, tz)}` : "Fora de casa"];
    if (holWord) parts.push(holWord);
    if (hasGridCharge(battery, nowMs)) parts.push("bateria carrega nas faixas baratas");
    else if (battery) parts.push("bateria só com sol");
    return parts.join(" · ");
  }
  if (holWord) {
    const what = hol.climate && hol.tank ? "sem aquecimento nem água quente"
      : hol.climate ? "sem aquecimento" : "sem água quente";
    return `${holWord} · ${what}`;
  }
  const cur = currentWindow(tariff, nowMs);
  const parts: string[] = [];
  if (cur) {
    parts.push(`faixa ${TIER_WORD[tierOf(cur)]}${cur.price_p != null ? ` (${pp(cur.price_p)})` : ""}`);
  }
  const src = powerSource(now.state);
  if (src) parts.push(`casa na ${src}`);
  const nxt = nextTransition(tariff, nowMs);
  if (nxt) {
    let s = `próximo evento ${nxt.start_local} ${TIER_WORD[tierOf(nxt)]}`;
    const cov = peakCoverage(battery, tariff, nowMs);
    if (cov) {
      if (cov.kind === "covered") s += `, bateria segura a casa até ${cov.until}`;
      else if (cov.kind === "buys") s += `, vai comprar ${fmt(cov.kwh)} kWh no pico`;
      else s += `, já comprou ${fmt(cov.kwh)} kWh no pico`;
    }
    parts.push(s);
  }
  return parts.length ? `Tudo normal. Agora: ${parts.join(" · ")}.` : "Tudo normal.";
}

// ------------------------------------------------------------------ tiles
export interface TileText { value: string | null; unit: string; meaning: string; next: string; tone: "ok" | "warn" | "bad" | "dim" }

export function casaTile(now: CockpitNow, fronts: PlanFrontsResponse | null): TileText {
  const h = fronts?.heating && !fronts.heating.error ? fronts.heating : null;
  const tariff = fronts?.tariff && !fronts.tariff.error ? fronts.tariff : null;
  const t = now.state.indoor_c ?? h?.indoor_now_c ?? null;
  const tz = now.planner_tz;
  const nowMs = Date.parse(now.now_utc);
  const off = holidayFlags(now).climate;
  let meaning = "Sem leitura dos sensores";
  let tone: TileText["tone"] = "dim";
  if (t != null && h) {
    const night = inNightWindow(localMinutes(now.now_utc, tz), h.night_start_hour ?? 22, h.night_end_hour ?? 7);
    const cur = currentWindow(tariff, nowMs);
    const inPeak = !!cur && tierOf(cur) === "peak";
    const setp = h.setpoint_c;
    // Same three-level floor the planner uses: night / peak coast / setpoint.
    const floor = night ? h.night_floor_c : inPeak && setp != null ? setp - (h.peak_coast_delta_c ?? 0) : setp;
    const ceil = h.ceiling_c;
    if (off) { meaning = "Aquecimento desligado (holiday)"; tone = "dim"; }
    else if (floor != null && t < floor - 0.3) { meaning = `Frio · abaixo de ${num(floor)}`; tone = "warn"; }
    else if (ceil != null && t > ceil + 0.3) { meaning = `Quente · acima de ${num(ceil)}`; tone = "warn"; }
    else {
      tone = "ok";
      meaning = `Confortável · meta ${setp != null ? num(setp) : "—"}${h.night_floor_c != null ? `, mínimo ${num(h.night_floor_c)} à noite` : ""}`;
    }
  } else if (t != null) {
    meaning = off ? "Aquecimento desligado (holiday)" : "Temperatura média da casa";
  }
  return { value: t != null ? fmt(t) : null, unit: "°C", meaning, next: nextHeating(h, tariff, nowMs, off), tone };
}

export function nextHeating(
  h: PlanFrontsHeating | null | undefined,
  tariff: PlanFrontsTariff | null | undefined,
  nowMs: number,
  off: boolean,
): string {
  if (off) return "Próximo: aquecimento religa antes da volta";
  if (!h) return "";
  const ws = [...(h.windows ?? [])].sort((a, b) => Date.parse(a.start_utc) - Date.parse(b.start_utc))
    .filter((w) => Date.parse(w.end_utc) > nowMs && ((w.offset_c ?? 0) !== 0 || w.kind === "setback"));
  const first = ws[0];
  if (!first) return "Próximo: mantém a temperatura";
  const isCoast = (w: { offset_c: number | null; kind: string }) => (w.offset_c ?? 0) < 0 || w.kind === "setback";
  const bandTag = (w: { start_utc: string }) => {
    const bw = currentWindow(tariff, Date.parse(w.start_utc));
    return bw && tierOf(bw) === "peak" ? " (pico)" : "";
  };
  const ongoing = Date.parse(first.start_utc) <= nowMs;
  if (isCoast(first)) {
    const back = ws.find((w) => !isCoast(w) && Date.parse(w.start_utc) >= Date.parse(first.end_utc));
    const head = ongoing
      ? `Agora: sem aquecer até ${first.end_local}${bandTag(first)}`
      : `Próximo: ${span(first.start_local, first.end_local)} sem aquecer${bandTag(first)}`;
    return `${head}${back ? ` · volta a aquecer ${back.start_local}` : ""}`;
  }
  const coast = ws.find((w) => isCoast(w) && Date.parse(w.start_utc) >= Date.parse(first.end_utc));
  const head = ongoing
    ? `Agora: aquecendo até ${first.end_local}`
    : `Próximo: aquece ${span(first.start_local, first.end_local)}`;
  return `${head}${coast ? ` · depois ${span(coast.start_local, coast.end_local)} sem aquecer${bandTag(coast)}` : ""}`;
}

export function aguaTile(now: CockpitNow, fronts: PlanFrontsResponse | null): TileText {
  const tk = fronts?.tank && !fronts.tank.error ? fronts.tank : null;
  const t = now.state.tank_c ?? tk?.tank_now_c ?? null;
  const tz = now.planner_tz;
  const nowMin = localMinutes(now.now_utc, tz);
  const nowMs = Date.parse(now.now_utc);
  const off = holidayFlags(now).tank;
  let meaning = "Água quente";
  let tone: TileText["tone"] = "dim";
  if (off) {
    meaning = "Tanque desligado (holiday)";
  } else if (tk) {
    const upcoming = (tk.showers ?? []).filter((s) => minutesOf(s.end_local) > nowMin)
      .sort((a, b) => minutesOf(a.start_local) - minutesOf(b.start_local));
    if (upcoming.length === 0) {
      meaning = "Sem banho previsto hoje";
    } else {
      // Each shower is judged on the tank PREDICTED at its start (the plan's
      // coast), the first one falls back to the live reading if there is none.
      const ok = upcoming.map((s, i) => {
        const pred = s.predicted_tank_c ?? (i === 0 ? t : null);
        return pred != null ? pred >= s.floor_c : null;
      });
      let lastOk = -1;
      while (lastOk + 1 < upcoming.length && ok[lastOk + 1] === true) lastOk++;
      const bad = ok.findIndex((v) => v === false);
      if (lastOk >= 0) {
        meaning = `Pronta para banho (≥ ${num(upcoming[0].floor_c)} até as ${upcoming[lastOk].end_local})`;
        tone = "ok";
        if (bad > lastOk) { meaning += ` · pode faltar às ${upcoming[bad].start_local}`; tone = "warn"; }
      } else if (bad === 0) {
        const s = upcoming[0];
        meaning = `Pode faltar para o banho das ${s.start_local} (prevista ${s.predicted_tank_c != null ? num(Math.round(s.predicted_tank_c)) : "—"} °C, ≥ ${num(s.floor_c)})`;
        tone = "warn";
      }
    }
  }
  return { value: t != null ? fmt(t, 0) : null, unit: "°C", meaning, next: nextTank(tk, nowMs, off), tone };
}

export function nextTank(tk: PlanFrontsTank | null | undefined, nowMs: number, off: boolean): string {
  if (off) return "Próximo: tanque religa antes da volta";
  if (!tk) return "";
  const ws = [...(tk.windows ?? [])].sort((a, b) => Date.parse(a.start_utc) - Date.parse(b.start_utc));
  const nxt = ws.find((w) => Date.parse(w.start_utc) > nowMs);
  if (!nxt) return "Próximo: sem mudança hoje";
  const tgt = nxt.tank_target_c != null ? num(Math.round(nxt.tank_target_c)) : null;
  const first = nxt.kind === "setback"
    ? `${nxt.start_local} baixa${tgt ? ` para ${tgt}` : ""}`
    : `${nxt.start_local} aquece${tgt ? ` para ${tgt}` : ""}`;
  const again = nxt.kind === "setback"
    ? ws.find((w) => w.kind !== "setback" && Date.parse(w.start_utc) > Date.parse(nxt.start_utc))
    : null;
  return `Próximo: ${first}${again ? ` · aquece de novo ${again.start_local}` : ""}`;
}

/** "Chega até HH:MM": earliest of (planned SoC reaching the reserve) and the
 *  next charge (grid window, else the next cheap band). */
export interface Runway { kind: "empty" | "recharge" | "unknown"; at: string | null; now?: boolean }
export function batteryRunway(
  battery: PlanFrontsBattery | null | undefined,
  tariff: PlanFrontsTariff | null | undefined,
  nowMs: number,
  tz?: string,
): Runway {
  if (!battery || battery.error) return { kind: "unknown", at: null };
  const reserve = battery.reserve_pct ?? 10;
  const cands: Array<{ ms: number; at: string; kind: "empty" | "recharge" }> = [];
  const ws = [...(battery.windows ?? [])].sort((a, b) => Date.parse(a.start_utc) - Date.parse(b.start_utc));
  for (const w of ws) {
    const s = Date.parse(w.start_utc), e = Date.parse(w.end_utc);
    if (e <= nowMs) continue;
    if (w.kind === "grid_charge" && s > nowMs) {
      cands.push({ ms: s, at: w.start_local, kind: "recharge" });
      continue;
    }
    const a = w.soc_start_pct, b = w.soc_end_pct;
    if (a != null && b != null && b <= reserve + 0.5 && a > b) {
      const f = a <= reserve ? 0 : (a - reserve) / (a - b);
      const ms = s + f * (e - s);
      if (ms <= nowMs) return { kind: "empty", at: null, now: true };   // already at the floor
      cands.push({ ms, at: localHHMM(new Date(ms).toISOString(), tz), kind: "empty" });
    }
  }
  // Fallback: the next cheap band (today's, else the first of the day = tomorrow's).
  const cheap = [...(tariff?.windows ?? [])].filter((w) => tierOf(w) === "cheap")
    .sort((a, b) => Date.parse(a.start_utc) - Date.parse(b.start_utc));
  const nextCheap = cheap.find((w) => Date.parse(w.start_utc) > nowMs) ?? cheap[0];
  if (nextCheap) {
    const isTomorrow = Date.parse(nextCheap.start_utc) <= nowMs;
    cands.push({ ms: isTomorrow ? Infinity : Date.parse(nextCheap.start_utc), at: nextCheap.start_local, kind: "recharge" });
  }
  if (cands.length === 0) return { kind: "unknown", at: null };
  cands.sort((a, b) => a.ms - b.ms);
  return { kind: cands[0].kind, at: cands[0].at };
}

export function bateriaTile(now: CockpitNow, fronts: PlanFrontsResponse | null): TileText {
  const b = fronts?.battery && !fronts.battery.error ? fronts.battery : null;
  const tariff = fronts?.tariff && !fronts.tariff.error ? fronts.tariff : null;
  const soc = now.state.soc_pct ?? b?.soc_now_pct ?? null;
  const nowMs = Date.parse(now.now_utc);
  const away = isAway(now);
  let meaning = "Bateria";
  let tone: TileText["tone"] = "dim";
  if (away && b && !hasGridCharge(b, nowMs)) {
    meaning = `Só com sol${b.reserve_pct != null ? ` · piso ${num(b.reserve_pct)} %` : ""}`;
  } else if (b) {
    const r = batteryRunway(b, tariff, nowMs, now.planner_tz);
    if (r.now) { meaning = "No piso agora"; tone = "warn"; }
    else if (r.kind === "empty") { meaning = `Chega até as ${r.at}`; tone = "warn"; }
    else if (r.at) { meaning = `Chega até as ${r.at}`; tone = "ok"; }
  }
  return { value: soc != null ? fmt(soc, 0) : null, unit: "%", meaning, next: nextCharge(b, tariff, nowMs, away), tone };
}

export function nextCharge(
  b: PlanFrontsBattery | null | undefined,
  tariff: PlanFrontsTariff | null | undefined,
  nowMs: number,
  away: boolean,
): string {
  if (!b) return "";
  const ws = [...(b.windows ?? [])].sort((a, b2) => Date.parse(a.start_utc) - Date.parse(b2.start_utc));
  const g = ws.find((w) => w.kind === "grid_charge" && Date.parse(w.end_utc) > nowMs);
  if (g) {
    const win = tariff?.windows?.find((w) => Date.parse(w.start_utc) <= Date.parse(g.start_utc) && Date.parse(w.end_utc) > Date.parse(g.start_utc));
    const kwh = g.charge_kwh ?? g.grid_kwh;   // energy INTO the battery
    const price = win?.price_p != null ? ` a ${pp(win.price_p)}` : "";
    const q = kwh != null ? ` (${fmt(kwh)} kWh${price})` : "";
    const ongoing = Date.parse(g.start_utc) <= nowMs;
    return ongoing
      ? `Próximo: carregando da rede até ${g.end_local}${q}`
      : `Próximo: ${span(g.start_local, g.end_local)} carrega da rede${q}`;
  }
  if (away) return "Próximo: carrega com o sol";
  const cheap = [...(tariff?.windows ?? [])].filter((w) => tierOf(w) === "cheap")
    .sort((a, b2) => Date.parse(a.start_utc) - Date.parse(b2.start_utc));
  const nc = cheap.find((w) => Date.parse(w.start_utc) > nowMs) ?? cheap[0];
  return nc ? `Próximo: faixa barata ${span(nc.start_local, nc.end_local)}${nc.price_p != null ? ` (${pp(nc.price_p)})` : ""}` : "";
}

/** Help line under the mode switch, built from the live plan (never hard-coded). */
export function vacationHelp(vac: CockpitVacation | null | undefined, battery: PlanFrontsBattery | null | undefined,
                             nowMs: number, tz?: string): string {
  const parts: string[] = [];
  if (vac?.holiday_enabled !== false) parts.push("Daikin em holiday até a véspera da volta");
  const reserve = battery && !battery.error && battery.reserve_pct != null ? `, piso ${num(battery.reserve_pct)} %` : "";
  parts.push(hasGridCharge(battery, nowMs)
    ? `bateria carrega nas faixas baratas${reserve}`
    : `bateria só com sol${reserve}`);
  parts.push(vac?.recovery_at ? `aquecimento religado ${weekdayTime(vac.recovery_at, tz)}` : "aquecimento religado antes da chegada");
  return `Em férias: ${parts.join(", ")}.`;
}

// ------------------------------------------------------------------- money
export type SpendTone = "ok" | "neutral" | "warn" | "dim";
/** Words describe the actual test: the day's AVERAGE import price vs the band prices. */
export function spendVerdict(spend: PlanFrontsSpend | null | undefined): { word: string; tone: SpendTone } {
  switch (spend?.score) {
    case "ideal": return { word: "preço médio ótimo", tone: "ok" };
    case "below": return { word: "preço médio ok", tone: "neutral" };
    case "above": return { word: "preço médio alto", tone: "warn" };
    default: return { word: "", tone: "dim" };
  }
}
/** The peak figure lives in the strip's Rede lane (once); this line has no peak. */
export function spendLine(spend: PlanFrontsSpend | null | undefined): string {
  if (!spend) return "";
  const parts: string[] = ["líquido (com taxa fixa, menos exportação)"];
  if (spend.realised_import_kwh != null) {
    const avg = spend.realised_avg_import_p != null ? ` a ${pp(spend.realised_avg_import_p)}` : "";
    parts.push(`${fmt(spend.realised_import_kwh)} kWh da rede${avg}`);
  }
  if (spend.pv_kwh != null) parts.push(`${fmt(spend.pv_kwh)} kWh de sol`);
  return parts.join(" · ");
}
export function todayNet(spend: PlanFrontsSpend | null | undefined): number | null {
  return spend?.realised_net_cost_gbp ?? null;
}

export function cycleLabel(b: PlanFrontsBilling): string {
  const f = (iso: string) => {
    const d = new Date(`${iso}T12:00:00Z`);
    const m = d.toLocaleDateString("pt-BR", { month: "short", timeZone: "UTC" }).replace(".", "");
    return `${d.getUTCDate()} ${m}`;
  };
  return `${f(b.cycle_start)} → ${f(b.cycle_end)}${b.since ? ` · desde ${f(b.since)}` : ""}`;
}
/** null when the comparison has no winner (never invent "tarifa certa"). */
export function billingVerdict(b: PlanFrontsBilling): { lead: string; others: string[]; good: boolean } | null {
  if (!b.winner) return null;
  const others = b.compare
    .filter((c) => c.tariff !== b.winner!.tariff)
    .map((c) => (c.tie ? `${c.short_label} ≈ igual` : `${c.short_label} ${signedGbp(c.delta_gbp)}`));
  if (b.winner.is_current) {
    return { lead: `${b.current_label ?? b.winner.label} é a tarifa certa`, others, good: true };
  }
  const w = b.compare.find((c) => c.tariff === b.winner!.tariff);
  const save = w ? Math.abs(w.delta_gbp) : 0;
  return { lead: `${b.winner.label} custaria −${gbp(save)}`, others, good: false };
}

// ------------------------------------------------------- band list (phone)
export interface BandRow {
  startLocal: string; endLocal: string; tier: Tier; price: number | null; current: boolean;
  grid: string | null; heat: HeatCell | null; water: string | null;
}
/** One row per tariff band for the narrow layout; each tank window is mapped to
 *  the band it overlaps most. */
export function bandRows(fronts: PlanFrontsResponse, nowMs: number, holiday: HolidayFlags): BandRow[] {
  const tariff = fronts.tariff && !fronts.tariff.error ? fronts.tariff : null;
  if (!tariff) return [];
  const wins = [...tariff.windows].sort((a, b) => Date.parse(a.start_utc) - Date.parse(b.start_utc));
  // Every band names the tank window that covers most of it.
  const tk = fronts.tank && !fronts.tank.error ? fronts.tank : null;
  const water: Array<string | null> = wins.map((w) => {
    let best: string | null = null, bestOv = 0;
    for (const tw of tk?.windows ?? []) {
      const ov = Math.min(Date.parse(tw.end_utc), Date.parse(w.end_utc)) - Math.max(Date.parse(tw.start_utc), Date.parse(w.start_utc));
      if (ov > bestOv) { bestOv = ov; best = holiday.tank ? "desligado" : waterLabel(tw.kind, tw.tank_target_c); }
    }
    return best;
  });
  return wins.map((w, i) => ({
    startLocal: w.start_local, endLocal: w.end_local, tier: tierOf(w), price: w.price_p,
    current: nowMs >= Date.parse(w.start_utc) && nowMs < Date.parse(w.end_utc),
    grid: gridLabel(w, fronts.battery),
    heat: heatingCell(w, fronts.heating, holiday.climate),
    water: water[i],
  }));
}

// ----------------------------------------------------------------- footer
export function footerLine(now: CockpitNow, planRunAt: string | null | undefined, sha: string): string {
  const tz = now.planner_tz;
  const parts: string[] = [];
  const dq = now.quota?.daikin;
  if (dq && dq.used != null && dq.budget) parts.push(`Daikin ${dq.used}/${dq.budget} chamadas`);
  const fq = now.quota?.fox;
  if (fq) parts.push(fq.blocked ? "Fox bloqueado" : "Fox ok");
  if (now.plan?.pushed_at_utc) parts.push(`plano das ${localHHMM(now.plan.pushed_at_utc, tz)}`);
  if (planRunAt) parts.push(`replanejado ${localHHMM(planRunAt, tz)}`);
  parts.push(`build ${sha}`);
  return parts.join(" · ");
}

// ------------------------------------------------------------ misc (form)
/** "2026-11-07T18:00:00+00:00" -> "2026-11-07T18:00" (datetime-local value). */
export function toDatetimeLocal(v: unknown): string {
  const s = String(v ?? "").trim();
  return s ? s.replace(/t/i, "T").slice(0, 16) : "";
}
/** Presets that mean "away" (vacation and its legacy aliases). */
export function isAwayPreset(preset: string | undefined | null): boolean {
  return ["vacation", "travel", "away"].includes(String(preset ?? "").toLowerCase());
}
