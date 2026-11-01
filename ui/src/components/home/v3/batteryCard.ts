// Pure logic for the Home v3 Bateria card (#899). Every value comes from
// /cockpit/now or /plan/fronts; a missing source hides its row, never invents.
import type { CockpitNow, PlanFrontsBattery, PlanFrontsResponse, StatusFoxDriftBlock } from "../../../lib/types";

// Local copies of three tiny helpers: this module is loaded by node's type
// stripping in tests, where a bare "../simple/logic" cannot resolve and a
// ".ts" specifier fails typecheck.
const fmt = (n: number, d = 1) => n.toFixed(d).replace(".", ",");
const num = (n: number) => (Number.isInteger(n) ? String(n) : fmt(n, 1));
function localHHMM(iso: string, tz?: string): string {
  const parts = new Intl.DateTimeFormat("en-GB", { hour: "2-digit", minute: "2-digit", hourCycle: "h23", timeZone: tz }).formatToParts(new Date(iso));
  const g = (t: string) => parts.find((p) => p.type === t)?.value ?? "00";
  return `${g("hour")}:${g("minute")}`;
}

export type CardTone = "ok" | "warn" | "dim";
export interface BatteryRow { key: "agora" | "pico" | "noite" | "inversor"; label: string; text: string; tone: CardTone }
export interface BatteryCardModel {
  socPct: number | null;
  stateWord: "carregando" | "descarregando" | "parada" | null;
  stateTone: "charge" | "discharge" | "idle";
  reservePct: number | null;
  /** Next planned charge target, e.g. { pct: 100, at: "16:00" }. */
  target: { pct: number; at: string } | null;
  rows: BatteryRow[];
}

const DEAD_BAND_KW = 0.05;
const FOX_STALE_S = 600;

export const FOX_MODE_PHRASE: Record<string, string> = {
  ForceCharge: "Carregando da rede na faixa barata",
  SelfUse: "A casa usa sol e bateria",
  Backup: "Segurando a carga para mais tarde",
  ForceDischarge: "Descarregando para a rede",
};

export function stateWord(kw: number | null | undefined): { word: BatteryCardModel["stateWord"]; tone: BatteryCardModel["stateTone"] } {
  if (kw == null || !Number.isFinite(kw)) return { word: null, tone: "idle" };
  if (Math.abs(kw) < DEAD_BAND_KW) return { word: "parada", tone: "idle" };
  return kw > 0 ? { word: "carregando", tone: "charge" } : { word: "descarregando", tone: "discharge" };
}

function usable(f: PlanFrontsResponse | null | undefined): PlanFrontsBattery | null {
  return f?.battery && !f.battery.error ? f.battery : null;
}

/** First charge window still running or ahead: the "meta" of the bar. */
export function nextTarget(b: PlanFrontsBattery | null, nowMs: number, tz?: string): BatteryCardModel["target"] {
  if (!b) return null;
  for (const w of b.windows ?? []) {
    if (w.kind !== "grid_charge" && w.kind !== "pv_charge") continue;
    if (w.soc_end_pct == null || !(Date.parse(w.end_utc) > nowMs)) continue;
    return { pct: Math.round(w.soc_end_pct), at: w.end_local || localHHMM(w.end_utc, tz) };
  }
  return null;
}

const isPeak = (r: { key: string; label: string }) => r.key === "band_peak" || r.label === "peak";
const isCheap = (r: { key: string; label: string }) => r.key === "band_cheap" || r.label === "cheap";
const hourOf = (hhmm: string) => String(Number(hhmm.slice(0, 2)));

export function peakRow(b: PlanFrontsBattery | null): BatteryRow | null {
  const p = b?.by_band?.find(isPeak);
  if (!p || p.planned_import_kwh == null) return null;
  const imp = p.planned_import_kwh;
  const label = `Pico ${hourOf(p.start_local)}–${hourOf(p.end_local)}`;
  return imp < 0.05
    ? { key: "pico", label, text: "Segura a casa sozinha, sem comprar da rede", tone: "ok" }
    : { key: "pico", label, text: `Precisa comprar ~${fmt(imp, 1)} kWh da rede`, tone: "warn" };
}

/** Expected charge on entering the next cheap band after the peak. */
export function nightRow(b: PlanFrontsBattery | null): BatteryRow | null {
  const rows = b?.by_band ?? [];
  const pi = rows.findIndex(isPeak);
  if (pi < 0) return null;
  const n = rows.slice(pi + 1).find(isCheap);
  if (!n || n.soc_entry_pct == null) return null;
  return { key: "noite", label: "Noite", text: `Chega a ~${num(Math.round(n.soc_entry_pct))}% às ${n.start_local} e volta a carregar`, tone: "dim" };
}

export function agoraRow(now: CockpitNow): BatteryRow | null {
  const m = now.current_slot?.fox_mode;
  const text = m ? FOX_MODE_PHRASE[m] : undefined;
  return text ? { key: "agora", label: "Agora", text, tone: "dim" } : null;
}

export function inversorRow(now: CockpitNow, drift?: Pick<StatusFoxDriftBlock, "in_sync"> | null, tz?: string): BatteryRow | null {
  const fx = now.freshness?.fox;
  if (!fx) return null;   // no freshness reading: hide rather than guess
  const pushed = now.plan?.pushed_at_utc;
  const last = pushed ? ` · último plano enviado ${localHHMM(pushed, tz)}` : "";
  if (fx.stale || fx.age_s > FOX_STALE_S) {
    return { key: "inversor", label: "Inversor", text: `Fox sem resposta há ${Math.max(1, Math.floor(fx.age_s / 60))} min${last}`, tone: "warn" };
  }
  if (drift && drift.in_sync === false) {
    return { key: "inversor", label: "Inversor", text: `Fox respondendo, mas o agendamento difere do plano${last}`, tone: "warn" };
  }
  return { key: "inversor", label: "Inversor", text: `Fox respondendo${last}`, tone: "ok" };
}

export function batteryCardModel(
  now: CockpitNow, fronts: PlanFrontsResponse | null | undefined, drift?: Pick<StatusFoxDriftBlock, "in_sync"> | null,
): BatteryCardModel {
  const b = usable(fronts);
  const tz = now.planner_tz;
  const nowMs = Date.parse(now.now_utc);
  const sw = stateWord(now.state?.battery_kw);
  const rows = [agoraRow(now), peakRow(b), nightRow(b), inversorRow(now, drift, tz)]
    .filter((r): r is BatteryRow => r != null);
  return {
    socPct: now.state?.soc_pct ?? b?.soc_now_pct ?? null,
    stateWord: sw.word, stateTone: sw.tone,
    reservePct: b?.reserve_pct ?? null,
    target: nextTarget(b, nowMs, tz),
    rows,
  };
}
