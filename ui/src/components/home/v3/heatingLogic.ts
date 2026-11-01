// Pure helpers for the Home v3 "Aquecimento · Daikin" card (#900). No DOM, no
// Preact: tested with `node --test` (type stripping). All copy is pt-BR; no
// solver jargon (offset, LP, slot).
import type {
  HeatingPlanWindow, HeatingPredictedIndoor, PlanFrontsTariff,
} from "../../../lib/types";
import { num, tierOf } from "../simple/logic.ts";

export const SETPOINT_KEY = "INDOOR_SETPOINT_C";
export const SETPOINT_STEP = 0.5;

const isPeak = (w: { key?: string | null; label?: string | null }): boolean => tierOf(w) === "peak";

// ------------------------------------------------------------------ headline
export type Tone = "ok" | "warn" | "bad" | "dim";
/** The comfort word is the first part of casaTile's meaning ("Confortável · meta
 *  21" -> "confortável"); we read it, we do not recompute the judgement. */
export function comfortWord(meaning: string, tone: Tone): { word: string; tone: Tone } {
  const head = meaning.split(" · ")[0].trim();
  const word = head ? head.charAt(0).toLowerCase() + head.slice(1) : "";
  return { word, tone };
}

// --------------------------------------------------------------------- chips
export interface RoomChip { label: string; value: string }
export function roomLabel(key: string): string {
  const s = key.replace(/_/g, " ").trim();
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : key;
}
export function roomChips(
  rooms: Record<string, number> | null | undefined,
  fallback: Array<{ room: string; temp_c: number | null; stale?: boolean }> | null | undefined,
  outdoor: number | null | undefined,
): RoomChip[] {
  const out: RoomChip[] = [];
  const entries: Array<[string, number]> = rooms && Object.keys(rooms).length
    ? Object.entries(rooms)
    : (fallback ?? []).filter((r) => r.temp_c != null && !r.stale).map((r) => [r.room, r.temp_c as number]);
  for (const [k, v] of entries.sort((a, b) => a[0].localeCompare(b[0], "pt-BR"))) {
    if (Number.isFinite(v)) out.push({ label: roomLabel(k), value: num(Math.round(v * 10) / 10) });
  }
  if (outdoor != null && Number.isFinite(outdoor)) out.push({ label: "Lá fora", value: num(Math.round(outdoor * 10) / 10) });
  return out;
}

// ---------------------------------------------------------------------- pump
/** Plain words for the radiator-water adjustment active at `nowMs`. */
export function lwtWord(windows: HeatingPlanWindow[] | null | undefined, nowMs: number, inPeak: boolean): string {
  const w = (windows ?? []).find((x) => nowMs >= Date.parse(x.start_utc) && nowMs < Date.parse(x.end_utc));
  const off = w?.offset_c ?? 0;
  if (w && (off < 0 || w.kind === "setback")) return inPeak ? "mais fria (pico)" : "mais fria, economizando";
  if (w && off > 0) return "um pouco mais quente, guardando calor";
  return "normal";
}

export interface PumpInput {
  controlMode: string | undefined;
  holidayClimate: boolean;
  away: boolean;
  daikinMode: string | null | undefined;
  lwtC: number | null | undefined;
  windows: HeatingPlanWindow[] | null | undefined;
  nowMs: number;
  inPeak: boolean;
}
const MODE_WORD: Record<string, string> = {
  heating: "Modo aquecimento", cooling: "Modo resfriamento", auto: "Modo automático",
};
/** Honest pump line. `passive` and holiday are stated, never dressed up. */
export function pumpLine(p: PumpInput): string {
  if (p.holidayClimate) return "Aquecimento desligado (modo férias da Daikin)";
  const mode = p.daikinMode ? (MODE_WORD[p.daikinMode] ?? null) : null;
  const water = p.lwtC != null ? `água do radiador ${num(Math.round(p.lwtC))} °C` : null;
  const base = [mode, water].filter(Boolean).join(" · ") || "Sem leitura da bomba";
  if (p.controlMode === "passive") return `${base} · o HEM só observa, quem manda é a Daikin`;
  if (p.away) return `${base} · modo férias do HEM`;
  if (water == null) return base;
  return `${base} (${lwtWord(p.windows, p.nowMs, p.inPeak)})`;
}

/** Why the day-target stepper is hidden (null = show it). */
export function controlsDisabledReason(
  controlMode: string | undefined, holidayClimate: boolean, away: boolean,
): string | null {
  if (holidayClimate) return "Daikin em modo férias: a meta de dia só volta a valer quando ele terminar.";
  if (away) return "Casa em modo férias: a meta de dia volta a valer ao fim das férias.";
  if (controlMode === "passive") return "O HEM está só observando a Daikin: mudar a meta não aciona o aquecimento.";
  return null;
}

// ---------------------------------------------------------------------- peak
export interface PeakRow { label: string; text: string }
export function peakRow(
  tariff: PlanFrontsTariff | null | undefined,
  windows: HeatingPlanWindow[] | null | undefined,
  predicted: HeatingPredictedIndoor | null | undefined,
  nowMs: number,
): PeakRow | null {
  const pk = (tariff?.windows ?? []).filter(isPeak).sort((a, b) => Date.parse(a.start_utc) - Date.parse(b.start_utc))
    .find((w) => Date.parse(w.end_utc) > nowMs);
  if (!pk) return null;
  const s = Date.parse(pk.start_utc), e = Date.parse(pk.end_utc);
  const over = (windows ?? []).filter((w) => Date.parse(w.start_utc) < e && Date.parse(w.end_utc) > s);
  let text: string;
  if (!windows || windows.length === 0) text = "";
  else if (over.some((w) => w.kind === "setback" || (w.offset_c ?? 0) < 0)) text = "Pausa o aquecimento";
  else if (over.some((w) => (w.offset_c ?? 0) > 0)) text = "Aquece também no pico";
  else text = "Mantém o aquecimento normal";
  const at19 = pk.end_local === "19:00" ? predicted?.at_19_c : null;
  if (at19 != null) {
    text = `${text || "Sem plano de aquecimento"}; previsto chegar às ${pk.end_local} com ${num(Math.round(at19 * 10) / 10)} °C`;
  }
  if (!text) return null;
  const hh = (s2: string) => s2.replace(/:00$/, "");
  return { label: `Pico ${hh(pk.start_local)}–${hh(pk.end_local)}`, text };
}

// ---------------------------------------------------------------------- meta
export function targetsLine(
  setpoint: number | null | undefined, nightFloor: number | null | undefined, hideDay = false,
): string | null {
  const parts: string[] = [];
  if (setpoint != null && !hideDay) parts.push(`${num(setpoint)} °C de dia`);
  if (nightFloor != null) parts.push(`${num(nightFloor)} °C à noite`);
  return parts.length ? parts.join(" · ") : null;
}

// ------------------------------------------------------------------- stepper
const half = (n: number): number => Math.round(n * 2) / 2;
/** Highest day target the backend accepts: spec max, and ceiling - 0.5 (server rule). */
export function setpointBounds(
  specMin: number | null | undefined, specMax: number | null | undefined, ceiling: number | null | undefined,
): { min: number; max: number } {
  const min = specMin ?? 16;
  let max = specMax ?? 26;
  if (ceiling != null) max = Math.min(max, ceiling - 0.5);
  return { min, max: Math.max(min, max) };
}
export function stepSetpoint(cur: number, dir: 1 | -1, min: number, max: number): number {
  return Math.min(max, Math.max(min, half(cur + dir * SETPOINT_STEP)));
}

/** Preview copy. /settings/batch/simulate returns only before/after of the key
 *  (no cost, no indoor delta), so we say exactly that and nothing more. */
export function previewText(
  liveSetpoint: number, draft: number, simBefore: unknown, simAfter: unknown,
): { text: string; warn: string | null } {
  const b = typeof simBefore === "number" ? simBefore : Number(simBefore);
  const a = typeof simAfter === "number" ? simAfter : Number(simAfter);
  const to = Number.isFinite(a) ? a : draft;
  const text = `A meta de dia vai de ${num(liveSetpoint)} para ${num(to)} °C e o plano é refeito agora. O efeito no custo só aparece depois, nos números do dia.`;
  // The plan reads the in-memory value; the simulator reads the saved one. If
  // they disagree, a temporary override is pinning the plan (Config._overrides).
  const warn = Number.isFinite(b) && Math.abs(b - liveSetpoint) > 1e-6
    ? `Atenção: o plano usa ${num(liveSetpoint)} °C, mas o valor salvo é ${num(b)} °C. Há um valor temporário preso; a mudança só pega depois de reiniciar o serviço.`
    : null;
  return { text, warn };
}

// ------------------------------------------------------------------ feedback
export type Verdict = "cold" | "ok" | "hot";
export const VERDICTS: Array<{ v: Verdict; label: string }> = [
  { v: "cold", label: "Com frio" }, { v: "ok", label: "Bom" }, { v: "hot", label: "Com calor" },
];
/** What the backend really does: stores the verdict with the house context; the
 *  Sunday review (and the weekly tuning) read it. Nothing changes right now. */
export function feedbackMessage(v: Verdict): string {
  const w = v === "cold" ? "com frio" : v === "hot" ? "com calor" : "bem";
  return `Anotado: você está ${w}. Não muda o aquecimento agora; entra na revisão de domingo, que ajusta a meta aos poucos.`;
}
