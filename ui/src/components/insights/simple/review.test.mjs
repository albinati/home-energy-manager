// Review-round tests (#879 PR 2): signed money, week range alignment, tank/solar honesty.
import test from "node:test";
import assert from "node:assert/strict";
import { sgbp, weekParams, weekHeading, billCaption, houseTiles } from "./logic.ts";

test("signed £: negatives keep their sign (arbitrage and bill can be negative)", () => {
  assert.equal(sgbp(-0.29), "−£0,29");
  assert.equal(sgbp(3.9), "£3,90");
  assert.equal(sgbp(0), "£0,00");
});

test("the endpoint range is exactly the navigator week (Mon..Sun clamped to today)", () => {
  assert.deepEqual(weekParams({ start: "2026-10-26", end: "2026-10-28" }), { start: "2026-10-26", end: "2026-10-28" });
  assert.equal(weekHeading({ start: "2026-10-26", end: "2026-10-28", complete_days: 2 }), "semana 26/10–28/10 · 2 dias completos");
  assert.equal(weekHeading({ start: "2026-10-19", end: "2026-10-25", complete_days: 7 }), "semana 19/10–25/10 · 7 dias completos");
  assert.equal(weekHeading({ start: "2026-10-27", end: "2026-10-27", complete_days: 1 }), "semana 27/10–27/10 · 1 dia completo");
});

test("usual is labelled with the number of days it covers", () => {
  assert.equal(
    billCaption({ current_label: "Cosy", compare: [], usual_gbp: 9, complete_days: 6 }),
    "usual £9,00 para 6 dias",
  );
});

test("tank: 'bate' only inside 0.8..1.25, single episode says last night", () => {
  const cap = (c) => houseTiles({ tank: c })[0].caption;
  assert.match(cap({ c_per_h_night: 0.35, ratio_median_recent: 1.02, n_recent: 5 }), /o modelo bate/);
  assert.match(cap({ c_per_h_night: 0.35, ratio_median_recent: 1.4, n_recent: 5 }), /o modelo erra: o medido é 1,40× o previsto/);
  assert.doesNotMatch(cap({ c_per_h_night: 0.35, ratio_median_recent: 0.6, n_recent: 5 }), /bate/);
  assert.match(cap({ c_per_h_night: 0.35, ratio_median_recent: 1.0, n_recent: 1 }), /perdeu na última noite/);
});

test("solar gain caption makes the 3 kW x 3 h assumption visible", () => {
  const t = houseTiles({ solar_gain: { coefficient: 0.2, value_c_per_sunny_afternoon: 0.4, applied: false, assumed_pv_kw: 3, assumed_hours: 3 } })[0];
  assert.match(t.caption, /\(3 kW de sol por 3 h\)/);
});

test("no solver jargon on the surface", () => {
  const all = houseTiles({
    cooling_c_per_h: { value: 0.06, outdoor_c: 9, tau_h: 70 },
    heating_c_per_h: { value: 0.16, offset_c: 3, lag_min: 35 },
  }).map((t) => t.caption).join(" ");
  assert.doesNotMatch(all, /LWT|τ/);
});
