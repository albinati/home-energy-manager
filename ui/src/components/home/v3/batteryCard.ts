// Pure logic for the Home v3 Bateria card (#899). Every value comes from
// /cockpit/now, /plan/fronts or /status/alerts; a missing source hides its row,
// never invents. Reuses the Home helpers (tierOf, peakCoverage, ...).
import type {
  BatteryPlanWindow, CockpitNow, PlanFrontsBattery, PlanFrontsResponse, PlanFrontsTariffWindow,
} from "../../../lib/types";
import {
  FAIXA_WORD, batteryRunway, chargeBasis, currentWindow, inNightWindow, isAway, localHHMM, minutesOf, num, peakCoverage, span, tierOf,
  peakSpans, upcomingPeakSpan,
} from "../simple/logic.ts";

export type CardTone = "ok" | "warn" | "dim";
export interface BatteryRow {
  key: "agora" | "pico" | "noite" | "autonomia" | "inversor"; label: string; text: string; tone: CardTone;
}
export type StateWord = "carregando" | "descarregando" | "parada" | "na reserva";
export interface BatteryCardModel {
  socPct: number | null;
  stateWord: StateWord | null;
  stateTone: "charge" | "discharge" | "idle" | "warn";
  reservePct: number | null;
  /** Next planned charge target, e.g. { pct: 100, at: "16:00" }. */
  target: { pct: number; at: string } | null;
  rows: BatteryRow[];
}
/** Extra health signals from /status/alerts (both optional). */
export interface FoxHealth { in_sync?: boolean | null; actuation_stale?: boolean | null }

const DEAD_BAND_KW = 0.05;
const FOX_STALE_S = 300;   // same threshold as agoraStale
const RESERVE_TOL = 0.5;
const SOON_MS = 2 * 3600_000;

export function foxModePhrase(mode: string | null | undefined, band?: PlanFrontsTariffWindow | null): string | null {
  switch (mode) {
    case "ForceCharge": return band ? `Carregando da rede na faixa ${FAIXA_WORD[tierOf(band)]}` : "Carregando da rede";
    case "SelfUse": return "A casa usa sol e bateria";
    case "Backup": return "Segurando a carga para mais tarde";
    case "ForceDischarge": return "Descarregando para a rede";
    default: return null;
  }
}

export function stateWord(
  kw: number | null | undefined, soc?: number | null, reserve?: number | null,
): { word: StateWord | null; tone: BatteryCardModel["stateTone"] } {
  if (soc != null && reserve != null && soc <= reserve + RESERVE_TOL && (kw == null || kw < DEAD_BAND_KW)) return { word: "na reserva", tone: "warn" };
  if (kw == null || !Number.isFinite(kw)) return { word: null, tone: "idle" };
  if (Math.abs(kw) < DEAD_BAND_KW) return { word: "parada", tone: "idle" };
  return kw > 0 ? { word: "carregando", tone: "charge" } : { word: "descarregando", tone: "discharge" };
}

function usable(f: PlanFrontsResponse | null | undefined): PlanFrontsBattery | null {
  return f?.battery && !f.battery.error ? f.battery : null;
}
const isCharge = (w: BatteryPlanWindow) => w.kind === "grid_charge" || w.kind === "pv_charge";
const discharges = (w: BatteryPlanWindow) => (w.discharge_kwh ?? 0) > 0.05 || w.kind === "export";

/** The "meta": the highest planned SoC of the coming charge run, i.e. the
 *  charge windows from now up to the next discharge or peak. An idle gap
 *  inside the run does not end it, and a PV intermediate is not the target. */
export function nextTarget(
  b: PlanFrontsBattery | null, tariff: PlanFrontsResponse["tariff"], nowMs: number, tz?: string,
): BatteryCardModel["target"] {
  if (!b) return null;
  const ws = [...(b.windows ?? [])]
    .filter((w) => Date.parse(w.end_utc) > nowMs)
    .sort((a, c) => Date.parse(a.start_utc) - Date.parse(c.start_utc));
  const first = ws.find(isCharge);
  if (!first) return null;
  const firstMs = Date.parse(first.start_utc);
  const peakStart = (tariff?.windows ?? [])
    .filter((w) => tierOf(w) === "peak" && Date.parse(w.start_utc) >= firstMs)
    .map((w) => Date.parse(w.start_utc)).sort((a, c) => a - c)[0] ?? Infinity;
  const nextDischarge = ws.find((w) => Date.parse(w.start_utc) > firstMs && discharges(w));
  const stop = Math.min(peakStart, nextDischarge ? Date.parse(nextDischarge.start_utc) : Infinity);
  let best: BatteryPlanWindow | null = null;
  for (const w of ws) {
    if (!isCharge(w) || w.soc_end_pct == null || Date.parse(w.start_utc) >= stop) continue;
    if (!best || w.soc_end_pct > (best.soc_end_pct as number)) best = w;
  }
  return best ? { pct: Math.round(best.soc_end_pct as number), at: best.end_local || localHHMM(best.end_utc, tz) } : null;
}

export function peakRow(b: PlanFrontsBattery | null, tariff: PlanFrontsResponse["tariff"], nowMs: number): BatteryRow | null {
  const peak = upcomingPeakSpan(tariff, nowMs);
  const cov = peakCoverage(b, tariff, nowMs);
  if (!peak || !cov) return null;
  const label = `Pico ${span(peak.start_local, peak.end_local)}`;
  if (cov.kind === "covered") return { key: "pico", label, text: "Segura a casa sozinha, sem comprar da rede", tone: "ok" };
  if (cov.kind === "buys") return { key: "pico", label, text: "Precisa comprar da rede", tone: "warn" };
  return { key: "pico", label, text: "Já comprou da rede neste pico", tone: "warn" };
}

/** Does a local band [start, end) overlap the (midnight-wrapping) night window? */
function overlapsNight(startLocal: string, endLocal: string, startHour: number, endHour: number): boolean {
  const a = minutesOf(startLocal);
  let b = minutesOf(endLocal);
  if (b <= a) b += 1440;   // "00:00" / "24:00" ends
  const ns = startHour * 60;
  return inNightWindow(a, startHour, endHour) || inNightWindow((b - 1) % 1440, startHour, endHour) || (ns >= a && ns < b);
}

/** Expected charge on entering the first cheap band that follows the peak (the
 *  latest span that has started, else the next one) and overlaps the night
 *  window, so a midday dip is never "Noite". Hidden once that band has started. */
export function nightRow(
  b: PlanFrontsBattery | null, tariff: PlanFrontsResponse["tariff"], nowMs: number, away: boolean,
  nightStartHour = 22, nightEndHour = 7,
): BatteryRow | null {
  if (!b) return null;
  // Anchor on the most recent peak that has started (it stays the anchor after
  // it ends, until the cheap band starts), else on the upcoming one.
  const spans = peakSpans(tariff?.windows);
  const peak = [...spans].reverse().find((p) => Date.parse(p.start_utc) <= nowMs) ?? spans[0];
  if (!peak) return null;
  const peakEnd = Date.parse(peak.end_utc);
  const tw = [...(tariff?.windows ?? [])]
    .sort((x, y) => Date.parse(x.start_utc) - Date.parse(y.start_utc))
    .find((w) => Date.parse(w.start_utc) >= peakEnd && tierOf(w) === "cheap"
      && overlapsNight(w.start_local, w.end_local, nightStartHour, nightEndHour));
  if (!tw) return null;
  const n = (b.by_band ?? []).find((r) => r.start_local === tw.start_local && r.end_local === tw.end_local);
  if (!n || n.soc_entry_pct == null) return null;
  const startMs = Date.parse(tw.start_utc);
  if (nowMs >= startMs) return null;
  let tail = "";
  if (away && chargeBasis(b, tariff, nowMs) === "sun") tail = " e carrega só com sol";
  else if ((b.windows ?? []).some((w) => w.kind === "grid_charge" && Date.parse(w.start_utc) >= startMs)) tail = " e volta a carregar";
  return { key: "noite", label: "Noite", text: `Chega a ~${num(Math.round(n.soc_entry_pct))}% às ${n.start_local}${tail}`, tone: "dim" };
}

/** Planned to hit the reserve before the next charge (not "already there": the state word covers that). */
export function runwayRow(b: PlanFrontsBattery | null, tariff: PlanFrontsResponse["tariff"], nowMs: number, tz?: string): BatteryRow | null {
  if (!b || b.reserve_pct == null) return null;
  const r = batteryRunway(b, tariff, nowMs, tz);
  if (r.kind !== "empty" || r.now || !r.at) return null;
  // Next grid charge after the crossing: within 2 h it is the planned drain before a cheap band.
  const cross = runwayCrossingMs(b, nowMs);
  const nextCharge = (b.windows ?? []).filter((w) => w.kind === "grid_charge" && Date.parse(w.start_utc) >= (cross ?? nowMs))
    .map((w) => Date.parse(w.start_utc)).sort((a, c) => a - c)[0];
  const soon = cross != null && nextCharge != null && nextCharge - cross <= SOON_MS;
  return { key: "autonomia", label: "Autonomia", text: `Pode chegar à reserva às ${r.at}, antes da próxima carga`, tone: soon ? "dim" : "warn" };
}

/** When the plan crosses the reserve (same interpolation as batteryRunway). */
function runwayCrossingMs(b: PlanFrontsBattery, nowMs: number): number | null {
  const reserve = b.reserve_pct ?? 10;
  const ws = [...(b.windows ?? [])].sort((a, c) => Date.parse(a.start_utc) - Date.parse(c.start_utc));
  for (const w of ws) {
    const st = Date.parse(w.start_utc), e = Date.parse(w.end_utc);
    if (e <= nowMs) continue;
    const a = w.soc_start_pct, c = w.soc_end_pct;
    if (a != null && c != null && c <= reserve + RESERVE_TOL && a > c) {
      const f = a <= reserve ? 0 : (a - reserve) / (a - c);
      return st + f * (e - st);
    }
  }
  return null;
}

export function agoraRow(now: CockpitNow, tariff: PlanFrontsResponse["tariff"], nowMs: number): BatteryRow | null {
  const text = foxModePhrase(now.current_slot?.fox_mode, currentWindow(tariff, nowMs));
  return text ? { key: "agora", label: "Agora", text, tone: "dim" } : null;
}

export function inversorRow(now: CockpitNow, health?: FoxHealth | null): BatteryRow | null {
  const fx = now.freshness?.fox;
  if (!fx) return null;   // no entry at all: hide rather than guess
  const row = (text: string, tone: CardTone): BatteryRow => ({ key: "inversor", label: "Inversor", text, tone });
  if (fx.age_s == null) return row("Fox sem leitura", "warn");   // real backend default before the first poll
  if (fx.stale || fx.age_s > FOX_STALE_S) return row(`Fox sem resposta há ${Math.max(1, Math.floor(fx.age_s / 60))} min`, "warn");
  if (now.freshness?.plan?.stale || health?.actuation_stale) return row("Fox respondendo, mas o plano não chega ao inversor", "warn");
  if (health && health.in_sync === false) return row("Fox respondendo, mas o agendamento difere do plano", "warn");
  return row("Fox respondendo", "ok");
}

export function batteryCardModel(
  now: CockpitNow, fronts: PlanFrontsResponse | null | undefined, health?: FoxHealth | null,
): BatteryCardModel {
  const b = usable(fronts);
  const tariff = fronts?.tariff && !fronts.tariff.error ? fronts.tariff : null;
  const tz = now.planner_tz;
  const nowMs = Date.parse(now.now_utc);
  const soc = now.state?.soc_pct ?? b?.soc_now_pct ?? null;
  const reserve = b?.reserve_pct ?? null;
  const sw = stateWord(now.state?.battery_kw, soc, reserve);
  const rows = [
    agoraRow(now, tariff, nowMs), peakRow(b, tariff, nowMs), nightRow(b, tariff, nowMs, isAway(now), fronts?.heating?.night_start_hour ?? 22, fronts?.heating?.night_end_hour ?? 7),
    runwayRow(b, tariff, nowMs, tz), inversorRow(now, health),
  ].filter((r): r is BatteryRow => r != null);
  return { socPct: soc, stateWord: sw.word, stateTone: sw.tone, reservePct: reserve, target: nextTarget(b, tariff, nowMs, tz), rows };
}

