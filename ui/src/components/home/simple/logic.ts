// Pure helpers for the simplified Home (#879). No DOM, no Preact, no value
// imports: this file is unit-tested with `node --test` (type stripping), so
// keep it dependency-free. All copy is pt-BR.
import type {
  CockpitNow,
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
  return `${hourWord(startLocal)}–${hourWord(endLocal === "24:00" ? "24:00" : endLocal)}`;
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

export function isNightStart(startLocal: string): boolean {
  const m = minutesOf(startLocal);
  return m >= 22 * 60 || m < 7 * 60;
}

// ----------------------------------------------------------------- day axis
export interface Axis { startMs: number; endMs: number }
/** The day axis = the span covered by the tariff windows (they tile the day). */
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
    label: w.price_p != null ? `${Math.round(w.price_p)}p` : "",
  }));
  // Dynamic tariffs can hand back consecutive windows of one tier: show a run.
  return mergeRuns(segs, (a, b) => a.tier === b.tier && a.price != null && b.price != null
    && Math.abs(a.price - b.price) < 0.5).map((s) => ({ ...s }));
}

// ---------------------------------------------------------------- lane: rede
export function gridLabel(
  w: PlanFrontsTariffWindow,
  battery: PlanFrontsBattery | null | undefined,
): string | null {
  const row = battery?.by_band?.find((r) => r.start_local === w.start_local && r.end_local === w.end_local);
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
export function heatingCell(
  w: PlanFrontsTariffWindow,
  heating: PlanFrontsHeating | null | undefined,
  holiday: boolean,
): HeatCell | null {
  if (holiday) return { mode: "off", text: "desligado" };
  if (!heating || heating.error) return null;
  const a = Date.parse(w.start_utc), b = Date.parse(w.end_utc);
  let best: { off: number | null; kind: string; ov: number } | null = null;
  for (const hw of heating.windows ?? []) {
    const ov = Math.min(b, Date.parse(hw.end_utc)) - Math.max(a, Date.parse(hw.start_utc));
    if (ov > 0 && (!best || ov > best.ov)) best = { off: hw.offset_c, kind: hw.kind, ov };
  }
  const target = isNightStart(w.start_local) ? heating.night_floor_c : heating.setpoint_c;
  const hold: HeatCell = { mode: "hold", text: target != null ? `mantém ${num(target)}` : "mantém" };
  if (!best || best.ov < (b - a) * 0.5) return hold;
  const off = best.off ?? 0;
  if (off > 0) return { mode: "boost", text: `aquece +${num(Math.round(off))}` };
  if (off < 0 || best.kind === "setback") return { mode: "off", text: "desligado" };
  return hold;
}

// ------------------------------------------------------------- lane: água
export interface WaterSeg { start: number; end: number; kind: string; target: number | null; text: string; warm: boolean }
export function waterSegments(tank: PlanFrontsTank | null | undefined, axis: Axis | null): WaterSeg[] {
  if (!tank || tank.error || !axis) return [];
  const raw = [...(tank.windows ?? [])]
    .sort((a, b) => Date.parse(a.start_utc) - Date.parse(b.start_utc))
    .map((w) => {
      const start = frac(axis, Date.parse(w.start_utc));
      const end = frac(axis, Date.parse(w.end_utc));
      const t = w.tank_target_c;
      const tt = t != null ? `${num(Math.round(t))} °C` : "";
      let text = tt;
      if (w.kind === "legionella") text = `${t != null ? tt : "60 °C"} legionella`;
      else if (w.kind === "setback") {
        const night = w.end_local === "00:00" || w.end_local === "24:00" || minutesOf(w.start_local) >= 22 * 60;
        text = `${tt} (${night ? "madrugada" : "economia"})`;
      }
      else text = `${tt} (banhos)`;
      return { start, end, kind: w.kind, target: t, text: text.trim(), warm: w.kind !== "setback" };
    })
    .filter((s) => s.end - s.start > 0.004);
  return mergeRuns(raw, (a, b) => a.kind === b.kind && a.target === b.target);
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
export function powerSource(s: { grid_kw?: number; battery_kw?: number; solar_kw?: number } | undefined): PowerSource {
  if (!s) return "bateria";
  if ((s.grid_kw ?? 0) > 0.3) return "rede";
  if ((s.battery_kw ?? 0) < -0.1) return "bateria";
  if ((s.solar_kw ?? 0) > 0.1) return "sol";
  return "bateria";
}

export function peakCoverage(battery: PlanFrontsBattery | null | undefined, tariff: PlanFrontsTariff | null | undefined,
                             nowMs: number): { covered: boolean; until: string; importKwh: number } | null {
  const peak = tariff?.windows?.find((w) => tierOf(w) === "peak" && Date.parse(w.end_utc) > nowMs);
  if (!peak || !battery || battery.error) return null;
  const row = battery.by_band?.find((r) => r.start_local === peak.start_local && r.end_local === peak.end_local);
  const planned = row?.planned_import_kwh ?? battery.peak_import_planned_kwh;
  if (planned == null) return null;
  return { covered: planned <= 0.1, until: peak.end_local, importKwh: planned };
}

// ---------------------------------------------------------- status sentence
export function weekdayTime(iso: string, tz?: string): string {
  try {
    const d = new Date(iso);
    const wd = d.toLocaleDateString("pt-BR", { weekday: "short", timeZone: tz }).replace(".", "");
    return `${wd} ${localHHMM(iso, tz)}`;
  } catch { return iso; }
}

export interface StatusCtx { now: CockpitNow; fronts: PlanFrontsResponse | null }

export function isHoliday(now: CockpitNow): boolean {
  const h = now.daikin?.holiday;
  return !!(h && (h.climate === true || h.tank === true));
}

export function buildStatus({ now, fronts }: StatusCtx): string {
  const tz = now.planner_tz;
  const vac = now.vacation;
  const hol = isHoliday(now);
  if (vac?.active) {
    const parts = [vac.return_at ? `Fora até ${weekdayTime(vac.return_at, tz)}` : "Fora de casa"];
    if (hol) parts.push("Daikin em holiday");
    parts.push("bateria só com sol");
    return parts.join(" · ");
  }
  if (hol) return "Daikin em holiday · sem aquecimento nem água quente · bateria segue o plano";
  const nowMs = Date.parse(now.now_utc);
  const tariff = fronts?.tariff && !fronts.tariff.error ? fronts.tariff : null;
  const cur = currentWindow(tariff, nowMs);
  const parts: string[] = [];
  if (cur) {
    const p = cur.price_p != null ? ` (${Math.round(cur.price_p)}p)` : "";
    parts.push(`faixa ${TIER_WORD[tierOf(cur)]}${p}`);
  }
  parts.push(`casa na ${powerSource(now.state)}`);
  const nxt = nextTransition(tariff, nowMs);
  if (nxt) {
    let s = `próximo evento ${nxt.start_local} ${TIER_WORD[tierOf(nxt)]}`;
    const cov = peakCoverage(fronts?.battery, tariff, nowMs);
    if (cov && (tierOf(nxt) === "peak" || (cur && tierOf(cur) === "peak"))) {
      s += cov.covered ? `, bateria segura a casa até ${cov.until}` : `, vai comprar ${fmt(cov.importKwh)} kWh no pico`;
    }
    parts.push(s);
  }
  return `Tudo normal. Agora: ${parts.join(" · ")}.`;
}

// ------------------------------------------------------------------ tiles
export interface TileText { value: string | null; unit: string; meaning: string; next: string; tone: "ok" | "warn" | "bad" | "dim" }

export function casaTile(now: CockpitNow, fronts: PlanFrontsResponse | null): TileText {
  const h = fronts?.heating && !fronts.heating.error ? fronts.heating : null;
  const t = now.state.indoor_c ?? h?.indoor_now_c ?? null;
  const tz = now.planner_tz;
  const nowMs = Date.parse(now.now_utc);
  const hol = isHoliday(now) || !!now.vacation?.active;
  let meaning = "Sem leitura dos sensores";
  let tone: TileText["tone"] = "dim";
  if (t != null && h) {
    const night = (() => { const m = localMinutes(now.now_utc, tz); return m >= 22 * 60 || m < 7 * 60; })();
    const floor = night ? h.night_floor_c : h.setpoint_c;
    const ceil = h.ceiling_c;
    const setp = h.setpoint_c;
    if (hol) { meaning = "Em férias · sem aquecimento"; tone = "dim"; }
    else if (floor != null && t < floor - 0.3) { meaning = `Frio · abaixo de ${num(floor)}`; tone = "warn"; }
    else if (ceil != null && t > ceil + 0.3) { meaning = `Quente · acima de ${num(ceil)}`; tone = "warn"; }
    else {
      tone = "ok";
      meaning = `Confortável · meta ${setp != null ? num(setp) : "—"}${h.night_floor_c != null ? `, mínimo ${num(h.night_floor_c)} à noite` : ""}`;
    }
  } else if (t != null) {
    meaning = hol ? "Em férias · sem aquecimento" : "Temperatura média da casa";
  }
  return { value: t != null ? fmt(t) : null, unit: "°C", meaning, next: nextHeating(h, nowMs, hol), tone };
}

export function nextHeating(h: PlanFrontsHeating | null | undefined, nowMs: number, holiday: boolean): string {
  if (holiday) return "Próximo: aquecimento religa antes da volta";
  if (!h) return "";
  const ws = [...(h.windows ?? [])].sort((a, b) => Date.parse(a.start_utc) - Date.parse(b.start_utc))
    .filter((w) => Date.parse(w.end_utc) > nowMs);
  const coast = ws.find((w) => (w.offset_c ?? 0) < 0 || w.kind === "setback");
  const bandName = (w: { start_local: string }) => {
    const hr = minutesOf(w.start_local);
    return hr >= 16 * 60 && hr < 19 * 60 ? " (pico)" : "";
  };
  if (coast) {
    const back = ws.find((w) => (w.offset_c ?? 0) > 0 && Date.parse(w.start_utc) >= Date.parse(coast.end_utc));
    return `Próximo: ${span(coast.start_local, coast.end_local)} sem aquecer${bandName(coast)}${back ? ` · volta a aquecer ${back.start_local}` : ""}`;
  }
  const boost = ws.find((w) => (w.offset_c ?? 0) > 0);
  if (boost) return `Próximo: aquece ${span(boost.start_local, boost.end_local)}`;
  return "Próximo: mantém a temperatura";
}

export function aguaTile(now: CockpitNow, fronts: PlanFrontsResponse | null): TileText {
  const tk = fronts?.tank && !fronts.tank.error ? fronts.tank : null;
  const t = now.state.tank_c ?? tk?.tank_now_c ?? null;
  const tz = now.planner_tz;
  const nowMin = localMinutes(now.now_utc, tz);
  const nowMs = Date.parse(now.now_utc);
  const hol = isHoliday(now) || !!now.vacation?.active;
  let meaning = "Água quente";
  let tone: TileText["tone"] = "dim";
  if (hol) {
    meaning = "Em férias · tanque desligado";
  } else if (tk && t != null) {
    const upcoming = (tk.showers ?? []).filter((s) => minutesOf(s.end_local) > nowMin)
      .sort((a, b) => minutesOf(a.start_local) - minutesOf(b.start_local));
    const cur = upcoming[0];
    const last = upcoming.length ? upcoming[upcoming.length - 1] : undefined;
    if (cur && last) {
      if (t >= cur.floor_c) { meaning = `Pronta para banho (≥ ${num(cur.floor_c)} até as ${last.end_local})`; tone = "ok"; }
      else { meaning = `Aquecendo para o banho das ${cur.start_local} (≥ ${num(cur.floor_c)})`; tone = "warn"; }
    } else {
      meaning = "Pronta para banho"; tone = "ok";
    }
  }
  return { value: t != null ? fmt(t, 0) : null, unit: "°C", meaning, next: nextTank(tk, nowMs, hol), tone };
}

export function nextTank(tk: PlanFrontsTank | null | undefined, nowMs: number, holiday: boolean): string {
  if (holiday) return "Próximo: tanque religa antes da volta";
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

/** "Chega até HH:MM": earliest of (planned SoC reaching the reserve) and
 *  (the next cheap/charging window). */
export interface Runway { kind: "empty" | "recharge" | "unknown"; at: string | null }
export function batteryRunway(
  battery: PlanFrontsBattery | null | undefined,
  tariff: PlanFrontsTariff | null | undefined,
  nowMs: number,
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
      const ms = Math.max(nowMs, s + f * (e - s));
      // local clock label from the window's own local bounds
      const spanMin = minutesOf(w.end_local === "00:00" ? "24:00" : w.end_local) - minutesOf(w.start_local);
      const mm = Math.round(minutesOf(w.start_local) + f * (spanMin > 0 ? spanMin : (e - s) / 60000));
      const hh = String(Math.floor(mm / 60) % 24).padStart(2, "0");
      cands.push({ ms, at: `${hh}:${String(mm % 60).padStart(2, "0")}`, kind: "empty" });
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
  const vac = !!now.vacation?.active;
  let meaning = "Bateria";
  let tone: TileText["tone"] = "dim";
  if (vac) {
    meaning = `Só com sol${b?.reserve_pct != null ? ` · piso ${num(b.reserve_pct)} %` : ""}`;
  } else if (b) {
    const r = batteryRunway(b, tariff, nowMs);
    if (r.kind === "empty") { meaning = `Chega até as ${r.at}`; tone = "warn"; }
    else if (r.at) { meaning = `Chega até as ${r.at}`; tone = "ok"; }
  }
  return { value: soc != null ? fmt(soc, 0) : null, unit: "%", meaning, next: nextCharge(b, tariff, nowMs, vac), tone };
}

export function nextCharge(
  b: PlanFrontsBattery | null | undefined,
  tariff: PlanFrontsTariff | null | undefined,
  nowMs: number,
  vacation: boolean,
): string {
  if (vacation) return "Próximo: carrega só com sol";
  if (!b) return "";
  const ws = [...(b.windows ?? [])].sort((a, b2) => Date.parse(a.start_utc) - Date.parse(b2.start_utc));
  const g = ws.find((w) => w.kind === "grid_charge" && Date.parse(w.end_utc) > nowMs);
  if (g) {
    const win = tariff?.windows?.find((w) => Date.parse(w.start_utc) <= Date.parse(g.start_utc) && Date.parse(w.end_utc) > Date.parse(g.start_utc));
    const kwh = g.grid_kwh ?? g.charge_kwh;
    const price = win?.price_p != null ? ` a ${fmt(win.price_p)}p` : "";
    const q = kwh != null ? ` (${fmt(kwh)} kWh${price})` : "";
    const ongoing = Date.parse(g.start_utc) <= nowMs;
    return ongoing
      ? `Próximo: carregando da rede até ${g.end_local}${q}`
      : `Próximo: ${span(g.start_local, g.end_local)} carrega da rede${q}`;
  }
  const cheap = [...(tariff?.windows ?? [])].filter((w) => tierOf(w) === "cheap")
    .sort((a, b2) => Date.parse(a.start_utc) - Date.parse(b2.start_utc));
  const nc = cheap.find((w) => Date.parse(w.start_utc) > nowMs) ?? cheap[0];
  return nc ? `Próximo: faixa barata ${span(nc.start_local, nc.end_local)}${nc.price_p != null ? ` (${fmt(nc.price_p)}p)` : ""}` : "";
}

// ------------------------------------------------------------------- money
export type SpendTone = "ok" | "warn" | "dim";
export function spendVerdict(spend: PlanFrontsSpend | null | undefined): { word: string; tone: SpendTone } {
  switch (spend?.score) {
    case "ideal": return { word: "Igual ao ideal", tone: "ok" };
    case "below": return { word: "Abaixo do usual", tone: "ok" };
    case "above": return { word: "Acima do usual", tone: "warn" };
    default: return { word: "", tone: "dim" };
  }
}
export function spendLine(spend: PlanFrontsSpend | null | undefined): string {
  if (!spend) return "";
  const parts: string[] = [];
  if (spend.realised_import_kwh != null) {
    const avg = spend.realised_avg_import_p != null ? ` a ${fmt(spend.realised_avg_import_p)}p` : "";
    parts.push(`${fmt(spend.realised_import_kwh)} kWh da rede${avg}`);
  }
  if (spend.pv_kwh != null) parts.push(`${fmt(spend.pv_kwh)} kWh de sol`);
  if (spend.peak_import_kwh != null) parts.push(`${spend.peak_import_kwh < 0.05 ? "0" : fmt(spend.peak_import_kwh)} no pico`);
  return parts.join(" · ");
}

export function shortTariff(label: string): string {
  if (/fix/i.test(label)) return "fixo";
  if (/svt|vari/i.test(label)) return "variável";
  return label.split(/\s+/)[0].replace(/[()]/g, "");
}
export function cycleLabel(b: PlanFrontsBilling): string {
  const f = (iso: string) => {
    const d = new Date(`${iso}T12:00:00Z`);
    const m = d.toLocaleDateString("pt-BR", { month: "short", timeZone: "UTC" }).replace(".", "");
    return `${d.getUTCDate()} ${m}`;
  };
  return `${f(b.cycle_start)} → ${f(b.cycle_end)}`;
}
export function billingVerdict(b: PlanFrontsBilling): { lead: string; others: string; good: boolean } {
  const others = b.compare.map((c) => `${shortTariff(c.label)} ${signedGbp(c.delta_gbp)}`).join(" · ");
  if (!b.winner || b.winner.is_current) {
    return { lead: `${b.current_label ?? "Sua tarifa"} é a tarifa certa`, others, good: true };
  }
  const w = b.compare.find((c) => c.tariff === b.winner!.tariff);
  const save = w ? Math.abs(w.delta_gbp) : 0;
  return { lead: `${shortTariff(b.winner.label)} custaria −${gbp(save)}`, others, good: false };
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
