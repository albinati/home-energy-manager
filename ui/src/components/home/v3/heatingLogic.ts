// Pure helpers for the Home v3 "Aquecimento · Daikin" card (#900). No DOM, no
// Preact: tested with `node --test` (type stripping). All copy is pt-BR; no
// solver jargon (offset, LP, slot).
import type {
  HeatingPlanWindow, HeatingPredictedIndoor, PlanFrontsTariff,
} from "../../../lib/types";
import { num, tierOf } from "../simple/logic.ts";

export const SETPOINT_KEY = "INDOOR_SETPOINT_C";
export const CEILING_KEY = "LP_W3_CEILING_C";
export const SETPOINT_STEP = 0.5;

const isPeak = (w: { key?: string | null; label?: string | null }): boolean => tierOf(w) === "peak";

// ------------------------------------------------------------------ headline
export type Tone = "ok" | "warn" | "bad" | "dim";
/** The comfort word is the first part of casaTile's meaning ("Confortável · meta
 *  21" -> "confortável"); we read it, we do not recompute the judgement. Only the
 *  three real comfort words pass: holiday/sensor texts are said elsewhere. */
const COMFORT_WORDS = new Set(["confortável", "frio", "quente"]);
export function comfortWord(meaning: string, tone: Tone): { word: string; tone: Tone } {
  const head = meaning.split(" · ")[0].trim().toLowerCase();
  return COMFORT_WORDS.has(head) ? { word: head, tone } : { word: "", tone };
}

// --------------------------------------------------------------------- chips
export interface RoomChip { label: string; value: string }
export function roomLabel(key: string): string {
  const s = key.replace(/_/g, " ").trim();
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : key;
}
/** Room chips from the same snapshot as the headline (`state.indoor.rooms`,
 *  fresh only), falling back to the plan's rooms. Fewer than 2 rooms: no room
 *  chips (one chip would repeat the headline); "Lá fora" is always kept. */
export function roomChips(
  stateRooms: Array<{ room: string; temp_c: number | null; stale?: boolean }> | null | undefined,
  planRooms: Record<string, number> | null | undefined,
  outdoor: number | null | undefined,
): RoomChip[] {
  const fromState: Array<[string, number]> = (stateRooms ?? [])
    .filter((r) => r.temp_c != null && !r.stale).map((r) => [r.room, r.temp_c as number]);
  const entries = fromState.length ? fromState : Object.entries(planRooms ?? {});
  const out: RoomChip[] = [];
  if (entries.length >= 2) {
    for (const [k, v] of [...entries].sort((a, b) => a[0].localeCompare(b[0], "pt-BR"))) {
      if (Number.isFinite(v)) out.push({ label: roomLabel(k), value: num(Math.round(v * 10) / 10) });
    }
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
/** Honest pump line. `passive` and holiday are stated here, once, never dressed up. */
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

/** The stepper is hidden (silently: the Bomba row already says why) when the
 *  device is not driven by the plan: passive, Daikin holiday or HEM vacation. */
export function stepperHidden(controlMode: string | undefined, holidayClimate: boolean, away: boolean): boolean {
  return holidayClimate || away || controlMode === "passive";
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
  else if (over.some((w) => w.kind === "setback" || (w.offset_c ?? 0) < 0)) text = "Água mais fria no pico (a casa segura o calor)";
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
/** Highest day target the backend accepts: spec max, and ceiling - 0.5 (server rule).
 *  `ceiling` is the live LP_W3_CEILING_C setting; the plan's ceiling is the fallback. */
export function setpointBounds(
  specMin: number | null | undefined, specMax: number | null | undefined,
  ceilingSetting: number | null | undefined, ceilingPlan?: number | null,
): { min: number; max: number } {
  const min = specMin ?? 16;
  let max = specMax ?? 26;
  const ceiling = ceilingSetting ?? ceilingPlan ?? null;
  if (ceiling != null) max = Math.min(max, ceiling - 0.5);
  return { min, max: Math.max(min, max) };
}
export function stepSetpoint(cur: number, dir: 1 | -1, min: number, max: number): number {
  return Math.min(max, Math.max(min, half(cur + dir * SETPOINT_STEP)));
}

/** Preview copy. /settings/batch/simulate returns only before/after of the key
 *  (no cost, no indoor delta), so we say that and nothing more. Applying always
 *  replans, and the new plan goes straight to the battery and the pump. */
export function previewText(live: number, draft: number): string {
  return `A meta de dia vai de ${num(live)} para ${num(draft)} °C. Ao aplicar, o plano é refeito e a bateria e a bomba já recebem o novo plano. O efeito no custo só aparece depois, nos números do dia.`;
}

/** pt-BR message for an API failure; never raw JSON or server text. */
export function errorMessage(status: number | null | undefined, body: string | null | undefined): string {
  const b = body || "";
  if (/BatchPartialFailure/.test(b)) return "Não foi possível salvar a meta; nada foi alterado.";
  if (/Simulation(Expired|IdRequired|IdMismatch)/.test(b)) return "A simulação expirou. Revise e confirme de novo.";
  if (status === 401 || status === 403) return "Sem permissão. Entre como administrador.";
  if (status === 400 || status === 422) return "Valor fora do limite permitido.";
  if (status === 503) return "Serviço indisponível no momento.";
  if (status != null && status >= 500) return "Erro no servidor. Tente de novo.";
  return "Algo deu errado. Tente de novo.";
}

export interface ApplyDeps {
  apply: () => Promise<unknown>;                  // save the setting (throws on failure)
  replan: () => Promise<unknown>;                 // propose; always applies
  fetchSetpoint: () => Promise<number | null>;    // FRESH plan read (throws if it failed)
}
export interface ApplyOutcome {
  replanError: unknown | null;
  verified: "yes" | "no" | "unknown";
}
/** apply -> replan -> verify. `apply` errors propagate (nothing saved). A replan
 *  failure is reported, not thrown (the setting IS saved). Verification counts
 *  only when the fresh read succeeded, so a failed refresh never false-alarms. */
export async function runApply(deps: ApplyDeps, want: number): Promise<ApplyOutcome> {
  await deps.apply();
  let replanError: unknown | null = null;
  try { await deps.replan(); } catch (e) { replanError = e ?? new Error("replan failed"); }
  let verified: ApplyOutcome["verified"] = "unknown";
  try {
    const got = await deps.fetchSetpoint();
    if (got != null) verified = Math.abs(got - want) < 1e-6 ? "yes" : "no";
  } catch { /* stale or failed read: say nothing */ }
  return { replanError, verified };
}

// ------------------------------------------------------------------ feedback
export type Verdict = "cold" | "ok" | "hot";
export const VERDICTS: Array<{ v: Verdict; label: string }> = [
  { v: "cold", label: "Com frio" }, { v: "ok", label: "Bom" }, { v: "hot", label: "Com calor" },
];
/** What the backend really does: stores the verdict with the house context; the
 *  Sunday summary and weekly review read it. Nothing changes right now. */
export function feedbackMessage(v: Verdict): string {
  if (v === "ok") return "Anotado: conforto bom.";
  return "Anotado. Não muda o aquecimento agora; entra no resumo de domingo. Se repetir, pode sugerir ajustar o mínimo da noite ou do pico.";
}
