// Pure helpers for the Home v3 "Água quente" card (#901). No DOM, no Preact
// (unit-tested with `node --test` type stripping; logic.ts is imported by source). The shared
// judgement ("pronta para banho", "próximo passo") stays in simple/logic.ts
// (aguaTile) and is passed in already computed; nothing is re-derived here.
import type { PlanFrontsTank } from "../../../lib/types";
import { localHHMM } from "../simple/logic.ts";

export const BAR_MIN_C = 30;
export const BAR_MAX_C = 60;

const c0 = (n: number): string => String(Math.round(n));

/** Position on the 30-60 C bar, 0..100 (clamped); null when unknown. */
export function barPct(c: number | null | undefined): number | null {
  if (c == null || !Number.isFinite(c)) return null;
  return Math.max(0, Math.min(100, ((c - BAR_MIN_C) / (BAR_MAX_C - BAR_MIN_C)) * 100));
}

export type WaterState = "heating" | "holding" | "eco" | "off" | "boost" | "unknown";
export interface WaterHead {
  tank: number | null;
  state: WaterState;
  text: string;
  target: number | null;
}

/** Headline state. "aquecendo" only when a warm-up/boost window is open AND the
 *  tank is clearly below its target (the firmware only reheats outside its dead
 *  band, so a tank a degree under target is "mantendo"). */
export function waterHead(
  tk: PlanFrontsTank | null | undefined,
  tankNow: number | null | undefined,
  nowMs: number,
  opts: { holidayOff: boolean; boostUntil?: string | null; boostTarget?: number | null; differential?: number | null } = { holidayOff: false },
): WaterHead {
  const tank = tankNow ?? tk?.tank_now_c ?? null;
  if (opts.holidayOff) return { tank, state: "off", text: "desligada (modo férias)", target: null };
  if (opts.boostUntil && opts.boostTarget != null) {
    return { tank, state: "boost", text: `banho extra, meta ${c0(opts.boostTarget)} °C`, target: opts.boostTarget };
  }
  if (!tk) return { tank, state: "unknown", text: "", target: null };
  if (tk.power_on === false) return { tank, state: "off", text: "desligada", target: tk.target_now_c };
  const target = tk.target_now_c;
  if (target == null) return { tank, state: "unknown", text: "", target: null };
  const win = (tk.windows ?? []).find(
    (w) => Date.parse(w.start_utc) <= nowMs && nowMs < Date.parse(w.end_utc),
  );
  if (win?.kind === "setback") return { tank, state: "eco", text: `economia (meta ${c0(target)} °C)`, target };
  // "aquecendo" is only claimed when the firmware really reheats: a warm-up/boost
  // window is open AND the tank is further under target than the calibrated
  // reheat differential. Otherwise just state the target.
  const diff = opts.differential;
  const heating = diff != null && (win?.kind === "warmup" || win?.kind === "boost")
    && tank != null && tank <= target - diff;
  return heating
    ? { tank, state: "heating", text: `aquecendo até ${c0(target)} °C`, target }
    : { tank, state: "holding", text: `meta ${c0(target)} °C`, target };
}

/** "Depois" row. aguaTile().next is "Próximo: 16:00 baixa para 37 ..."; the row
 *  label already says what it is, so the prefix goes. While a boost holds the
 *  tank the planned step is NOT what happens next, so say that instead. */
export function afterRow(
  next: string | undefined,
  boost: { until: string | null; target: number | null } | null,
): string | null {
  if (boost?.until) {
    return "Volta ao plano quando o banho extra acabar";
  }
  if (!next) return null;
  const s = next.replace(/^Próximo:\s*/, "").trim();
  return s ? s[0].toUpperCase() + s.slice(1) : null;
}

/** "Próxima dom 8/11 às 12:00" in the planner's timezone. */
export function legionellaRow(
  l: { start_utc: string; end_utc: string } | null | undefined,
  nowMs: number,
  tz?: string,
): string | null {
  if (!l) return null;
  const start = Date.parse(l.start_utc);
  const end = Date.parse(l.end_utc);
  if (!Number.isFinite(start) || !Number.isFinite(end) || end <= nowMs) return null;
  try {
    const d = new Date(start);
    const wd = d.toLocaleDateString("pt-BR", { weekday: "short", timeZone: tz }).replace(".", "");
    const md = d.toLocaleDateString("pt-BR", { day: "numeric", month: "numeric", timeZone: tz });
    const hm = d.toLocaleTimeString("pt-BR", { hour: "2-digit", minute: "2-digit", hourCycle: "h23", timeZone: tz });
    const when = `${wd} ${md} às ${hm}`;
    return start <= nowMs ? `Em andamento desde ${hm}` : `Próxima ${when}`;
  } catch { return null; }
}

/** What the boost control shows. Hidden = no button at all. */
export type BoostView =
  | { kind: "hidden" }
  | { kind: "idle"; hint?: string }
  | { kind: "active"; until: string | null };

// Transient gates worth saying out loud; every other block (passive, holiday,
// vacation, tank already hot, tank off, read-only, no reading) hides the button.
const HINTS: Record<string, string> = {
  QuotaExhausted: "Banho extra indisponível: cota do Daikin esgotada hoje.",
  LegionellaStandoff: "Banho extra indisponível durante o ciclo de legionela.",
  RestorePending: "O tanque ainda está voltando ao plano; o banho extra volta a ficar disponível em instantes.",
};

export function boostView(
  st: { active: boolean; available: boolean; until_utc: string | null; target_c: number; blocked: { error: string } | null } | null | undefined,
  tankNow: number | null | undefined,
  tz?: string,
): BoostView {
  if (!st) return { kind: "hidden" };
  if (st.active) return { kind: "active", until: st.until_utc ? hhmm(st.until_utc, tz) : null };
  if (tankNow != null && tankNow >= st.target_c) return { kind: "hidden" };
  if (st.available) return { kind: "idle" };
  const hint = st.blocked ? HINTS[st.blocked.error] : undefined;
  return hint ? { kind: "idle", hint } : { kind: "hidden" };
}

export const hhmm = (iso: string, tz?: string): string => localHHMM(iso, tz);

/** Explicit band wording; unknown kinds say nothing. */
export function bandText(kind: string | undefined): string {
  switch (kind) {
    case "negative": return "Agora o preço é negativo.";
    case "solar_charge": return "Agora há sobra de sol.";
    case "cheap": return "Agora é faixa barata.";
    case "standard": return "Agora é faixa dia: custa mais que na barata.";
    case "peak":
    case "peak_export": return "Agora é pico: custa mais.";
    default: return "";
  }
}

/** Confirm copy: says exactly what the backend does (target now, Powerful when
 *  the gap is inside the firmware deadband, plan hands off for `hold_hours`,
 *  then resumes). No money figure: none is computed. */
export function confirmCopy(
  st: { target_c: number; hold_hours: number; uses_powerful?: boolean },
  until: string | null,
  bandKind: string | undefined,
): string {
  const hold = `${String(st.hold_hours).replace(".", ",")} h`;
  const heat = st.uses_powerful
    ? `Liga o modo Powerful e aquece até ${c0(st.target_c)} °C agora (pode usar a resistência, mais cara).`
    : `Aquece o tanque até ${c0(st.target_c)} °C agora.`;
  const band = bandText(bandKind);
  return `${heat} O plano não mexe no tanque por ${hold}${until ? ` (até ${until})` : ""} e depois volta ao normal.${band ? ` ${band}` : ""}`;
}

/** Confirm copy for the simulate -> confirm step. Whether Powerful is switched on comes from
 *  the SIMULATE diff's `after.powerful` (what the backend will really do now), never from the
 *  60 s status poll's `uses_powerful`, which may be a minute stale. */
export function confirmCopyFromDiff(
  st: { target_c: number; hold_hours: number; uses_powerful?: boolean },
  diff: { after?: { powerful?: boolean; hold_until_utc?: string } | null } | null | undefined,
  until: string | null,
  bandKind: string | undefined,
  tz?: string,
): string {
  // The backend's own hold end wins over the client-side now + hold_hours estimate.
  const holdUntil = diff?.after?.hold_until_utc;
  const untilText = holdUntil && Number.isFinite(Date.parse(holdUntil)) ? hhmm(holdUntil, tz) : until;
  return confirmCopy({ ...st, uses_powerful: diff?.after?.powerful === true }, untilText, bandKind);
}
