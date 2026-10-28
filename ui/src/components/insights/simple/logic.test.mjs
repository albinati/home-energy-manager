// Unit tests for the pure Insights helpers. Run: `npm test` (node --test).
import test from "node:test";
import assert from "node:assert/strict";
import {
  fmt, gbp, pct, moneyText, moneyTone, splitHeadline, showApply, sortItems, bandSegments,
  billCaption, energyCaption, solarCaption, batteryCaption, houseTiles,
} from "./logic.ts";

test("money formatting uses decimal comma", () => {
  assert.equal(fmt(0.06, 2), "0,06");
  assert.equal(gbp(8.1), "£8,10");
  assert.equal(gbp(-2), "£2,00");
  assert.equal(pct(0.613), "61 %");
  assert.equal(pct(null), "—");
});

test("row money: label as sent, ok for status, none for unpriced suggestion", () => {
  assert.equal(moneyText({ kind: "suggestion", money_label: "−35p/sem", money_pence: -35 }), "−35p/sem");
  assert.equal(moneyText({ kind: "status", money_label: null, money_pence: null }), "ok");
  assert.equal(moneyText({ kind: "suggestion", money_label: null, money_pence: null }), null);
  assert.equal(moneyTone({ kind: "suggestion", money_pence: -35 }), "save");
  assert.equal(moneyTone({ kind: "suggestion", money_pence: 12 }), "cost");
  assert.equal(moneyTone({ kind: "status", money_pence: null }), "ok");
  assert.equal(moneyTone({ kind: "setting", money_pence: null }), "none");
});

test("headline splits on the first em dash", () => {
  assert.deepEqual(splitHeadline("Automática — o HEM liga no melhor horário"), { bold: "Automática", rest: " — o HEM liga no melhor horário" });
  assert.deepEqual(splitHeadline("Água a 45 °C das 13:00 às 22:00"), { bold: "Água a 45 °C das 13:00 às 22:00", rest: "" });
});

test("Aplicar only for admin and only for setting rows with an action", () => {
  const setting = { kind: "setting", action: { setting_key: "K", from: 1, to: 2 } };
  assert.equal(showApply(setting, true), true);
  assert.equal(showApply(setting, false), false);
  assert.equal(showApply({ kind: "suggestion", action: null }, true), false);
  assert.equal(showApply({ kind: "setting", action: null }, true), false);
});

test("sortItems keeps people rows before setting rows, stable otherwise", () => {
  const mk = (key, kind) => ({ key, kind });
  const out = sortItems([mk("a", "setting"), mk("b", "status"), mk("c", "suggestion"), mk("d", "setting")]);
  assert.deepEqual(out.map((i) => i.key), ["b", "c", "a", "d"]);
});

test("bar segments sum to 100 and drop zero bands", () => {
  const e = { total_kwh: 100, by_band: { cheap: 40, day: 40, peak: 0, solar: 20 }, shares: { cheap: 0.4, day: 0.4, peak: 0, solar: 0.2 }, peak_kwh: 0 };
  const segs = bandSegments(e);
  assert.deepEqual(segs.map((s) => s.key), ["cheap", "day", "solar"]);
  assert.equal(Math.round(segs.reduce((s, x) => s + x.pct, 0)), 100);
  assert.equal(segs[0].pct, 40);
  assert.deepEqual(bandSegments({ ...e, by_band: { cheap: 0, day: 0, peak: 0, solar: 0 } }), []);
});

test("bill caption: winner phrasing and usual", () => {
  assert.equal(
    billCaption({ current_label: "Cosy", compare: [{ short_label: "Agile", delta_gbp: 2 }, { short_label: "fixo", delta_gbp: 3.5 }], usual_gbp: 9 }),
    "Cosy ganha: Agile +£2,00, fixo +£3,50 · usual £9,00",
  );
  assert.equal(billCaption({ current_label: "Cosy", compare: [{ short_label: "Flux", delta_gbp: -1.2 }], usual_gbp: null }), "Flux −£1,20");
  assert.equal(billCaption({ current_label: null, compare: [], usual_gbp: null }), "");
});

test("week captions", () => {
  assert.equal(energyCaption({ total_kwh: 112, by_band: {}, shares: { cheap: 0.5 }, peak_kwh: 0 }), "50 % na faixa barata · 0 kWh no pico");
  assert.equal(
    solarCaption({ kwh: 23, self_use_share: 0.61, export_kwh: 9, export_gbp: 1.35, forecast_mape: 0.12 }),
    "61 % usado em casa, 39 % exportado a 15p (£1,35). Previsão acertou ±12 %",
  );
  assert.equal(
    batteryCaption({ arbitrage_gbp: 3.9, cheap_bought_kwh: 58, cheap_p: 12.49, cycles_per_day: 1, peak_covered_days: 6, days: 7 }),
    "58 kWh comprados a 12,5p cobriram dia/pico · 1,0 ciclo/dia · pico coberto 6 de 7 dias",
  );
});

test("house tiles: only identifiable estimators produce a tile", () => {
  assert.deepEqual(houseTiles({}), []);
  const t = houseTiles({
    cooling_c_per_h: { value: 0.06, outdoor_c: 9.4, tau_h: 70 },
    heating_c_per_h: { value: 0.16, offset_c: 3, lag_min: 35 },
    solar_gain: { coefficient: 0.2, value_c_per_sunny_afternoon: 0.4, applied: false },
    tank: { c_per_h_night: 0.35, ratio_median_recent: 1.02 },
  });
  assert.deepEqual(t.map((x) => x.key), ["cooling", "heating", "solar", "tank"]);
  assert.equal(t[0].value, "0,06");
  assert.equal(t[0].caption, "sem aquecer, a 9 °C fora (τ ≈ 70 h)");
  assert.equal(t[1].caption, "com +3 de LWT. Calor chega ~35 min depois");
  assert.equal(t[2].value, "+0,4");
  assert.match(t[2].caption, /ainda não entra no plano \(ganho solar = 0\)/);
  assert.match(t[3].caption, /ratio 1,02/);
  // heating without a lag estimate drops the sentence instead of faking one
  const h = houseTiles({ heating_c_per_h: { value: 0.1, offset_c: -2, lag_min: null } });
  assert.equal(h[0].caption, "com −2 de LWT.");
});
