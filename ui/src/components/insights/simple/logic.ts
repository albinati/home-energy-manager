// Pure helpers for the simplified Insights (#879 PR 2). No DOM, no Preact, no
// value imports: unit-tested with `node --test` (type stripping). All copy pt-BR.
import type { RecHouse, RecItem, RecWeekSummary } from "../../../lib/recTypes";

// ---------------------------------------------------------------- formatting
export function fmt(n: number, d = 1): string {
  return n.toFixed(d).replace(".", ",");
}
export function gbp(n: number): string {
  return `£${fmt(Math.abs(n), 2)}`;
}
/** £ with its sign kept: -0.29 -> "−£0,29" (arbitrage and the bill can be negative). */
export function sgbp(n: number): string {
  return `${n < 0 ? "−" : ""}${gbp(n)}`;
}
export function pct(share: number | null | undefined): string {
  return share == null ? "—" : `${Math.round(share * 100)} %`;
}

/** Money cell of a row: label as sent ("−35p/sem"), "ok" for status, nothing otherwise. */
export function moneyText(it: Pick<RecItem, "kind" | "money_label" | "money_pence">): string | null {
  if (it.money_label) return it.money_label;
  if (it.kind === "status") return "ok";
  return null;
}
/** Tone of the money cell: green when it is a saving (or status ok), dim otherwise. */
export function moneyTone(it: Pick<RecItem, "kind" | "money_pence">): "save" | "ok" | "cost" | "none" {
  if (it.kind === "status") return "ok";
  if (it.money_pence == null) return "none";
  return it.money_pence < 0 ? "save" : "cost";
}

/** "Automática — o HEM liga …" -> bold "Automática", rest "— o HEM liga …". */
export function splitHeadline(h: string): { bold: string; rest: string } {
  const i = h.indexOf(" — ");
  if (i < 0) return { bold: h, rest: "" };
  return { bold: h.slice(0, i), rest: h.slice(i) };
}

/** Admin-only Aplicar button: only rows that change a setting. */
export function showApply(it: Pick<RecItem, "kind" | "action">, isAdmin: boolean): boolean {
  return isAdmin && it.kind === "setting" && it.action != null;
}

export function sortItems(items: RecItem[]): RecItem[] {
  // Server order is already intentional (status/suggestions first, settings last);
  // keep it stable and just guarantee settings never precede people suggestions.
  const rank = (k: RecItem["kind"]) => (k === "setting" ? 1 : 0);
  return items.map((it, i) => ({ it, i })).sort((a, b) => rank(a.it.kind) - rank(b.it.kind) || a.i - b.i).map((x) => x.it);
}

// ------------------------------------------------------------------ week tiles
export interface Seg { key: "cheap" | "day" | "peak" | "solar"; label: string; kwh: number; pct: number }
const SEG_LABEL = { cheap: "barata", day: "dia", peak: "pico", solar: "sol" } as const;

/** Stacked-bar segments; widths sum to 100 and follow the kWh (shares are re-derived,
 *  so a missing `shares` still draws). Zero segments are dropped. */
export function bandSegments(e: NonNullable<RecWeekSummary["energy"]>): Seg[] {
  const keys = ["cheap", "day", "peak", "solar"] as const;
  const total = keys.reduce((s, k) => s + (e.by_band[k] ?? 0), 0);
  if (total <= 0) return [];
  return keys
    .map((k) => ({ key: k, label: SEG_LABEL[k], kwh: e.by_band[k] ?? 0, pct: ((e.by_band[k] ?? 0) / total) * 100 }))
    .filter((s) => s.kwh > 0);
}

export function billCaption(
  ws: Pick<RecWeekSummary, "compare" | "current_label" | "usual_gbp"> & { complete_days?: number },
): string {
  const parts: string[] = [];
  const cmp = ws.compare ?? [];
  if (cmp.length) {
    const cur = ws.current_label || "Cosy";
    const allWorse = cmp.every((c) => c.delta_gbp >= 0);
    const list = cmp.map((c) => `${c.short_label} ${c.delta_gbp < 0 ? "−" : "+"}${gbp(c.delta_gbp)}`).join(", ");
    parts.push(allWorse ? `${cur} ganha: ${list}` : list);
  }
  if (ws.usual_gbp != null) {
    const n = ws.complete_days;
    parts.push(`usual ${gbp(ws.usual_gbp)}${n ? ` para ${n} ${n === 1 ? "dia" : "dias"}` : ""}`);
  }
  return parts.join(" · ");
}

/** Query range for the endpoint: EXACTLY the navigator's week (Mon..Sun clamped to today). */
export function weekParams(range: { start: string; end: string }): { start: string; end: string } {
  return { start: range.start, end: range.end };
}

/** "semana 26/10–28/10 · 2 dias completos" (dd/mm from ISO dates; today's partial day is left out). */
export function weekHeading(week: { start: string; end: string; complete_days?: number }): string {
  const dm = (iso: string) => iso.slice(5).split("-").reverse().join("/");
  const n = week.complete_days;
  const tail = n != null ? ` · ${n} ${n === 1 ? "dia completo" : "dias completos"}` : "";
  return `semana ${dm(week.start)}–${dm(week.end)}${tail}`;
}

export function energyCaption(e: NonNullable<RecWeekSummary["energy"]>): string {
  const bits: string[] = [];
  if (e.shares.cheap != null) bits.push(`${pct(e.shares.cheap)} na faixa barata`);
  if (e.peak_kwh != null) bits.push(e.peak_kwh <= 0.05 ? "0 kWh no pico" : `${fmt(e.peak_kwh)} kWh no pico`);
  return bits.join(" · ");
}

export function solarCaption(s: NonNullable<RecWeekSummary["solar"]>): string {
  const bits: string[] = [];
  if (s.self_use_share != null) {
    const used = Math.round(s.self_use_share * 100);
    let t = `${used} % usado em casa, ${100 - used} % exportado`;
    if (s.export_kwh && s.export_gbp != null && s.export_kwh > 0.05) {
      t += ` a ${Math.round((s.export_gbp / s.export_kwh) * 100)}p (${gbp(s.export_gbp)})`;
    }
    bits.push(t);
  }
  if (s.forecast_mape != null) bits.push(`Previsão errou em média ${Math.round(s.forecast_mape * 100)} %`);
  return bits.join(". ");
}

export function batteryCaption(b: NonNullable<RecWeekSummary["battery"]>): string {
  const bits: string[] = [];
  if (b.cheap_bought_kwh != null && b.cheap_bought_kwh > 0.05) {
    const p = b.cheap_p != null ? ` a ${fmt(b.cheap_p)}p` : "";
    bits.push(`${Math.round(b.cheap_bought_kwh)} kWh comprados${p} cobriram dia/pico`);
  }
  if (b.cycles_per_day != null) bits.push(`${fmt(b.cycles_per_day)} ciclo/dia`);
  // "pico coberto N de M" lives in the dinner/peak row of "O que fazer" (said once).
  return bits.join(" · ");
}

// ----------------------------------------------------------------------- house
export interface HouseTile { key: string; title: string; value: string; unit: string; caption: string }

export function houseTiles(h: RecHouse): HouseTile[] {
  const out: HouseTile[] = [];
  if (h.cooling_c_per_h) {
    const c = h.cooling_c_per_h;
    out.push({ key: "cooling", title: "Esfria", value: fmt(c.value, 2), unit: "°C/h",
      caption: `sem aquecer, a ${Math.round(c.outdoor_c)} °C fora (constante de tempo ≈ ${Math.round(c.tau_h)} h)` });
  }
  if (h.heating_c_per_h) {
    const c = h.heating_c_per_h;
    const sign = c.offset_c > 0 ? "+" : c.offset_c < 0 ? "−" : "";
    out.push({ key: "heating", title: "Aquece", value: fmt(c.value, 2), unit: "°C/h",
      caption: `com a água dos radiadores ${sign}${fmt(Math.abs(c.offset_c), 0)} °C acima da curva.${c.lag_min != null ? ` O calor chega ~${c.lag_min} min depois` : ""}`.trim() });
  }
  if (h.solar_gain) {
    const c = h.solar_gain;
    const v = c.value_c_per_sunny_afternoon;
    const assume = c.assumed_pv_kw != null && c.assumed_hours != null
      ? ` (${fmt(c.assumed_pv_kw, 0)} kW de sol por ${fmt(c.assumed_hours, 0)} h)` : "";
    out.push({ key: "solar", title: "Sol na casa", value: v != null ? `+${fmt(v, 1)}` : "—", unit: v != null ? "°C" : "",
      caption: `${v != null ? `numa tarde ensolarada${assume}` : "ganho medido"}${c.applied ? "; já entra no plano" : " — ainda não entra no plano (ganho solar = 0)"}` });
  }
  if (h.tank) {
    const c = h.tank;
    const ratio = c.ratio_median_recent;
    const single = c.n_recent != null && c.n_recent <= 1;
    let model = "";
    if (ratio != null) {
      model = ratio >= 0.8 && ratio <= 1.25
        ? ` — o modelo bate com o medido (razão ${fmt(ratio, 2)})`
        : ` — o modelo erra: o medido é ${fmt(ratio, 2)}× o previsto`;
    }
    out.push({ key: "tank", title: "Tanque", value: fmt(c.c_per_h_night, 2), unit: "°C/h",
      caption: `${single ? "perdeu na última noite" : "perde de noite"}${model}` });
  }
  return out;
}

// ----------------------------------------------------------------- week range
/** The `end` query for /insights/recommendations from the navigator's range:
 *  week -> its (today-clamped) last day; any other granularity -> its end too
 *  (the server always returns the 7 days ending there). */
export function weekEnd(range: { start: string; end: string }): string {
  return range.end;
}
