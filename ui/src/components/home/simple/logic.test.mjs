// Unit tests for the pure Home helpers. Run: `npm test` (node --test, no deps;
// logic.ts is loaded through Node's built-in type stripping, Node >= 22.6).
import test from "node:test";
import assert from "node:assert/strict";
import {
  fmt, pp, mergeRuns, dayAxis, frac, hourTicks, waterSegments, tariffSegments, gridLabel, heatingCell,
  batteryRunway, buildStatus, tierOf, billingVerdict, cycleLabel, nextHeating, aguaTile, casaTile, bateriaTile,
  peakCoverage, powerSource, spendVerdict, spendLine, bandRows, nextCharge, vacationHelp, holidayFlags,
  isAwayPreset, localHHMM,
} from "./logic.ts";

const TZ = "Europe/London";
// BST (UTC+1) on 2026-10-07.
const utc = (hhmm) => { const [h, m] = hhmm.split(":").map(Number); return new Date(Date.UTC(2026, 9, 7, h - 1, m)).toISOString(); };
const win = (s, e, label, p, status = "upcoming") => ({
  key: `band_${label}`, label, start_utc: utc(s), end_utc: e === "24:00" ? "2026-10-07T23:00:00.000Z" : utc(e),
  start_local: s, end_local: e, price_p: p, status,
});
const tariff = {
  display_name: "Cosy", structure: "banded",
  windows: [
    win("00:00", "04:00", "day", 25.45, "done"), win("04:00", "07:00", "cheap", 12.49, "done"),
    win("07:00", "13:00", "day", 25.45, "done"), win("13:00", "16:00", "cheap", 12.49, "ongoing"),
    win("16:00", "19:00", "peak", 38.17), win("19:00", "22:00", "day", 25.45), win("22:00", "24:00", "cheap", 12.49),
  ],
};
const NOW = utc("14:30");
const NOWMS = Date.parse(NOW);

test("number formats: decimal comma, one price format", () => {
  assert.equal(fmt(6.2), "6,2");
  assert.equal(pp(12.49), "12,5p");
  assert.equal(pp(25.45), "25,5p");
});

test("mergeRuns joins adjacent equal items only", () => {
  const out = mergeRuns(
    [{ start: 0, end: 0.2, k: "a" }, { start: 0.2, end: 0.4, k: "a" }, { start: 0.4, end: 0.5, k: "b" }, { start: 0.6, end: 0.7, k: "b" }],
    (a, b) => a.k === b.k,
  );
  assert.deepEqual(out.map((o) => [o.start, o.end, o.k]), [[0, 0.4, "a"], [0.4, 0.5, "b"], [0.6, 0.7, "b"]]);
});

test("tier classification, cosy and dynamic tiers", () => {
  assert.equal(tierOf({ key: "band_peak", label: "peak" }), "peak");
  assert.equal(tierOf({ key: "negative", label: "negative" }), "cheap");
  assert.equal(tierOf({ key: "band_day", label: "day" }), "standard");
});

test("day axis spans the tariff and tariff segments tile it", () => {
  const axis = dayAxis(tariff);
  const segs = tariffSegments(tariff, axis);
  assert.equal(segs.length, 7);
  assert.ok(Math.abs(segs[0].start) < 1e-9 && Math.abs(segs[6].end - 1) < 1e-9);
  assert.equal(segs[1].label, "12,5p");
  assert.ok(frac(axis, NOWMS) > 0.5);
});

test("hour ticks follow the real axis on a 25 h DST day", () => {
  // 2026-10-25: BST -> GMT, local midnight = 23:00Z the day before, day lasts 25 h.
  const axis = { startMs: Date.UTC(2026, 9, 24, 23), endMs: Date.UTC(2026, 9, 26, 0) };
  const t = Object.fromEntries(hourTicks(axis, TZ, [0, 6, 12, 18]).map((x) => [x.hour, x.f]));
  assert.ok(Math.abs(t[0]) < 1e-9);
  assert.ok(Math.abs(t[12] - 13 / 25) < 1e-6, `12:00 GMT is 13 h after the start, got ${t[12]}`);
  // 23 h spring-forward day (2026-03-29): 06:00 BST = 05:00Z, 6 h of wall clock = 5 h elapsed.
  const spring = { startMs: Date.UTC(2026, 2, 29, 0), endMs: Date.UTC(2026, 2, 29, 23) };
  const s = Object.fromEntries(hourTicks(spring, TZ, [0, 6]).map((x) => [x.hour, x.f]));
  assert.ok(Math.abs(s[6] - 5 / 23) < 1e-6, `got ${s[6]}`);
});

test("water lane merges equal adjacent windows and names them by kind", () => {
  const axis = dayAxis(tariff);
  const tank = {
    windows: [
      { kind: "warmup", start_utc: utc("13:00"), end_utc: utc("15:00"), start_local: "13:00", end_local: "15:00", tank_target_c: 47 },
      { kind: "warmup", start_utc: utc("15:00"), end_utc: utc("16:00"), start_local: "15:00", end_local: "16:00", tank_target_c: 47 },
      { kind: "setback", start_utc: utc("16:00"), end_utc: utc("24:00"), start_local: "16:00", end_local: "24:00", tank_target_c: 37 },
      { kind: "boost", start_utc: utc("04:00"), end_utc: utc("05:00"), start_local: "04:00", end_local: "05:00", tank_target_c: 60 },
      { kind: "legionella", start_utc: utc("11:00"), end_utc: utc("12:00"), start_local: "11:00", end_local: "12:00", tank_target_c: 60 },
    ],
  };
  const segs = waterSegments(tank, axis);
  assert.deepEqual(segs.map((s) => s.text), [
    "60 °C (carga barata)", "60 °C legionella", "47 °C (aquece)", "37 °C (economia)",
  ]);
  assert.ok(!segs.some((s) => /madrugada|banhos/.test(s.text)));
});

test("grid lane wording: realised of planned, goal on peak, plan for future, error section", () => {
  const battery = { by_band: [
    { start_local: "04:00", end_local: "07:00", planned_import_kwh: 6.2, realised_import_kwh: 6.0 },
    { start_local: "16:00", end_local: "19:00", planned_import_kwh: 0, realised_import_kwh: null },
    { start_local: "22:00", end_local: "24:00", planned_import_kwh: 2.1, realised_import_kwh: null },
  ] };
  const [, cheap, , , peak, , night] = tariff.windows;
  assert.equal(gridLabel(cheap, battery), "6,0 de 6,2 kWh");
  assert.equal(gridLabel(peak, battery), "0 (meta)");
  assert.equal(gridLabel(night, battery), "plano 2,1");
  assert.equal(gridLabel(cheap, { error: "boom" }), null);
});

test("heating lane: pre-heat >= 30 min counts, coast, hold from the plan, holiday, error", () => {
  const heating = {
    setpoint_c: 21, night_floor_c: 20,
    windows: [
      { kind: "boost", offset_c: 3, start_utc: utc("15:00"), end_utc: utc("16:00") },       // only the last hour of 13-16
      { kind: "setback", offset_c: -2, start_utc: utc("16:00"), end_utc: utc("19:00") },
    ],
    by_band: [
      { start_local: "19:00", end_local: "22:00", offset_mode: "neutral", indoor_min_c: 20.4, indoor_max_c: 20.9 },
      { start_local: "07:00", end_local: "13:00", offset_mode: "neutral", indoor_min_c: null, indoor_max_c: null },
    ],
  };
  const w = tariff.windows;
  assert.deepEqual(heatingCell(w[3], heating, false), { mode: "boost", text: "aquece +3" });
  assert.deepEqual(heatingCell(w[4], heating, false), { mode: "off", text: "desligado" });
  assert.deepEqual(heatingCell(w[5], heating, false), { mode: "hold", text: "mantém ~21" });   // predicted, not config
  assert.deepEqual(heatingCell(w[2], heating, false), { mode: "hold", text: "mantém" });         // no prediction -> no number
  assert.deepEqual(heatingCell(w[3], heating, true), { mode: "off", text: "desligado" });
  assert.equal(heatingCell(w[3], { error: "x" }, false), null);
});

test("battery runway: crossing mid-window, already at reserve, recharge, midnight wrap", () => {
  const draining = { reserve_pct: 10, windows: [
    { kind: "self_use", start_utc: utc("16:00"), end_utc: utc("19:00"), start_local: "16:00", end_local: "19:00", soc_start_pct: 50, soc_end_pct: 10 },
  ] };
  assert.deepEqual(batteryRunway(draining, tariff, NOWMS, TZ), { kind: "empty", at: "19:00" });
  const mid = { reserve_pct: 10, windows: [
    { kind: "self_use", start_utc: utc("16:00"), end_utc: utc("19:00"), start_local: "16:00", end_local: "19:00", soc_start_pct: 60, soc_end_pct: 0 },
  ] };
  // (60-10)/(60-0) of 3 h = 2 h 30 -> 18:30
  assert.deepEqual(batteryRunway(mid, tariff, NOWMS, TZ), { kind: "empty", at: "18:30" });
  const holds = { reserve_pct: 10, windows: [
    { kind: "self_use", start_utc: utc("16:00"), end_utc: utc("19:00"), start_local: "16:00", end_local: "19:00", soc_start_pct: 94, soc_end_pct: 50 },
  ] };
  assert.deepEqual(batteryRunway(holds, tariff, NOWMS, TZ), { kind: "recharge", at: "22:00" });
  assert.deepEqual(batteryRunway(holds, tariff, Date.parse(utc("23:00")), TZ), { kind: "recharge", at: "04:00" });
  // an ongoing window that already sits at the reserve is "now", never a past time
  const floor = { reserve_pct: 10, windows: [
    { kind: "self_use", start_utc: utc("13:00"), end_utc: utc("16:00"), start_local: "13:00", end_local: "16:00", soc_start_pct: 10, soc_end_pct: 8 },
  ] };
  assert.deepEqual(batteryRunway(floor, tariff, NOWMS, TZ), { kind: "empty", at: null, now: true });
  // crossing inside a window that wraps midnight is labelled in LOCAL time
  const wrap = { reserve_pct: 10, windows: [
    { kind: "self_use", start_utc: utc("23:00"), end_utc: utc("25:00"), start_local: "23:00", end_local: "01:00", soc_start_pct: 30, soc_end_pct: 10 },
  ] };
  assert.equal(batteryRunway(wrap, tariff, Date.parse(utc("22:00")), TZ).at, "01:00");
  const charge = { reserve_pct: 10, windows: [
    { kind: "grid_charge", start_utc: utc("15:00"), end_utc: utc("16:00"), start_local: "15:00", end_local: "16:00", soc_start_pct: 60, soc_end_pct: 90 },
    { kind: "grid_charge", start_utc: utc("20:00"), end_utc: utc("21:00"), start_local: "20:00", end_local: "21:00", soc_start_pct: 60, soc_end_pct: 90 },
  ] };
  assert.equal(batteryRunway(charge, tariff, NOWMS, TZ).at, "15:00");
  assert.equal(batteryRunway(charge, tariff, Date.parse(utc("15:30")), TZ).at, "20:00");
  assert.deepEqual(batteryRunway({ error: "x" }, tariff, NOWMS, TZ), { kind: "unknown", at: null });
});

const cockpit = (extra = {}) => ({
  now_utc: NOW, planner_tz: TZ,
  state: { soc_pct: 72, solar_kw: 0, load_kw: 0.5, grid_kw: 0, battery_kw: -0.5, tank_c: 44, indoor_c: 21.1 },
  ...extra,
});
const peakRows = (over = {}) => [
  { start_local: "13:00", end_local: "16:00", planned_import_kwh: 2.4, realised_import_kwh: 0.6, soc_entry_pct: 62 },
  { start_local: "16:00", end_local: "19:00", planned_import_kwh: 0, realised_import_kwh: null, soc_entry_pct: 94, ...over },
  { start_local: "19:00", end_local: "22:00", planned_import_kwh: 1.1, realised_import_kwh: null, soc_entry_pct: 50 },
];
const fronts = (rowOver = {}) => ({
  tariff,
  battery: { reserve_pct: 10, peak_import_planned_kwh: 0, windows: [], by_band: peakRows(rowOver) },
});

test("status sentence: normal day with a real peak-coverage basis", () => {
  const s = buildStatus({ now: cockpit(), fronts: fronts() });
  assert.equal(s, "Tudo normal. Agora: faixa barato (12,5p) · casa na bateria · próximo evento 16:00 pico, bateria segura a casa até 19:00.");
});

test("peak coverage is never invented", () => {
  // planned import unknown -> no claim at all
  assert.equal(peakCoverage(fronts({ planned_import_kwh: null }).battery, tariff, NOWMS), null);
  assert.doesNotMatch(buildStatus({ now: cockpit(), fronts: fronts({ planned_import_kwh: null }) }), /segura/);
  // no SoC at entry -> no claim
  assert.equal(peakCoverage(fronts({ soc_entry_pct: null }).battery, tariff, NOWMS), null);
  // SoC at entry at/under the reserve -> no claim
  assert.equal(peakCoverage(fronts({ soc_entry_pct: 10 }).battery, tariff, NOWMS), null);
  // planned purchase -> says so
  const buys = peakCoverage(fronts({ planned_import_kwh: 2.5 }).battery, tariff, NOWMS);
  assert.deepEqual(buys, { kind: "buys", until: "19:00", kwh: 2.5 });
  assert.match(buildStatus({ now: cockpit(), fronts: fronts({ planned_import_kwh: 2.5 }) }), /vai comprar 2,5 kWh no pico/);
  // inside the peak, realised import beats the plan
  const inPeak = Date.parse(utc("17:00"));
  assert.equal(peakCoverage(fronts({ realised_import_kwh: 0.8 }).battery, tariff, inPeak).kind, "bought");
  // section error -> no claim, no crash
  assert.equal(peakCoverage({ error: "x" }, tariff, NOWMS), null);
});

test("status sentence: vacation derives the battery wording from the plan", () => {
  const vac = { active: true, return_at: "2026-11-01T12:00:00+00:00" };
  const solarOnly = buildStatus({ now: cockpit({ vacation: vac, daikin: { holiday: { climate: true, tank: true } } }), fronts: fronts() });
  assert.equal(solarOnly, "Fora até dom 12:00 · Daikin em holiday · bateria só com sol");
  const withGrid = { ...fronts(), battery: { ...fronts().battery, windows: [
    { kind: "grid_charge", start_utc: utc("22:00"), end_utc: utc("24:00"), start_local: "22:00", end_local: "24:00", charge_kwh: 3 },
  ] } };
  const s = buildStatus({ now: cockpit({ vacation: vac }), fronts: withGrid });
  assert.equal(s, "Fora até dom 12:00 · bateria carrega nas faixas baratas");
  assert.doesNotMatch(s, /só com sol/);
});

test("holiday is per management point; vacation preset alone never blanks heating/water", () => {
  const climateOnly = cockpit({ daikin: { holiday: { climate: true, tank: false } } });
  assert.deepEqual(holidayFlags(climateOnly), { climate: true, tank: false });
  assert.equal(buildStatus({ now: climateOnly, fronts: fronts() }), "Daikin em holiday (aquecimento) · sem aquecimento");
  const tankOnly = cockpit({ daikin: { holiday: { climate: false, tank: true } } });
  assert.equal(buildStatus({ now: tankOnly, fronts: fronts() }), "Daikin em holiday (água quente) · sem água quente");
  assert.equal(aguaTile(tankOnly, { tank: { tank_now_c: 40, windows: [], showers: [] } }).meaning, "Tanque desligado (holiday)");
  assert.notEqual(casaTile(tankOnly, { heating: { setpoint_c: 21, night_floor_c: 20 } }).meaning, "Aquecimento desligado (holiday)");
  // away preset without any Daikin holiday flag: heating keeps its plan
  const away = cockpit({ vacation: { active: true, holiday_enabled: false } });
  assert.deepEqual(holidayFlags(away), { climate: false, tank: false });
  assert.deepEqual(heatingCell(tariff.windows[3], { windows: [{ kind: "boost", offset_c: 3, start_utc: utc("13:00"), end_utc: utc("16:00") }] }, holidayFlags(away).climate),
    { mode: "boost", text: "aquece +3" });
  assert.ok(isAwayPreset("vacation") && isAwayPreset("Travel") && !isAwayPreset("normal"));
});

test("power source is unknown (omitted) without Fox state", () => {
  assert.equal(powerSource(undefined), null);
  assert.equal(powerSource({}), null);
  assert.equal(powerSource({ grid_kw: 0, battery_kw: 0, solar_kw: 0 }), null);
  assert.equal(powerSource({ grid_kw: 1.2, battery_kw: 0, solar_kw: 0 }), "rede");
  const s = buildStatus({ now: cockpit({ state: {} }), fronts: fronts() });
  assert.doesNotMatch(s, /casa na/);
});

test("billing verdict: current wins, cheaper tariff, tie, no winner", () => {
  const b = { cycle_start: "2026-10-21", cycle_end: "2026-11-20", since: null, current_label: "Cosy",
    compare: [
      { tariff: "FIX", label: "British Gas Fixed", short_label: "fixo", delta_gbp: 6.3, tie: false, approximate: false },
      { tariff: "AG", label: "Agile", short_label: "Agile", delta_gbp: 0.4, tie: true, approximate: false },
    ],
    winner: { tariff: "COSY", label: "Cosy", is_current: true } };
  assert.deepEqual(billingVerdict(b), { lead: "Cosy é a tarifa certa", others: ["fixo +£6,30", "Agile ≈ igual"], good: true });
  const w = { ...b, compare: [{ tariff: "AG", label: "Agile", short_label: "Agile", delta_gbp: -2.8, tie: false, approximate: false }],
    winner: { tariff: "AG", label: "Agile", is_current: false } };
  const v = billingVerdict(w);
  assert.equal(v.lead, "Agile custaria −£2,80");
  assert.deepEqual(v.others, []);   // the winner's delta appears once
  assert.equal(billingVerdict({ ...b, winner: null, compare: [] }), null);
  assert.equal(cycleLabel(b), "21 out → 20 nov");
  assert.equal(cycleLabel({ ...b, since: "2026-10-25" }), "21 out → 20 nov · desde 25 out");
});

test("spend verdict words describe the price test; line is net and has no peak figure", () => {
  assert.deepEqual(spendVerdict({ score: "ideal" }), { word: "preço médio ótimo", tone: "ok" });
  assert.deepEqual(spendVerdict({ score: "below" }), { word: "preço médio ok", tone: "neutral" });
  assert.deepEqual(spendVerdict({ score: "above" }), { word: "preço médio alto", tone: "warn" });
  const line = spendLine({ realised_import_kwh: 6.1, realised_avg_import_p: 14.2, pv_kwh: 4.2, peak_import_kwh: 0 });
  assert.equal(line, "líquido (com taxa fixa, menos exportação) · 6,1 kWh da rede a 14,2p · 4,2 kWh de sol");
});

test("casa next: earliest event first, then the coast / return detail", () => {
  const h = { windows: [
    { kind: "boost", offset_c: 3, start_utc: utc("13:00"), end_utc: utc("16:00"), start_local: "13:00", end_local: "16:00" },
    { kind: "setback", offset_c: -2, start_utc: utc("16:00"), end_utc: utc("19:00"), start_local: "16:00", end_local: "19:00" },
    { kind: "boost", offset_c: 3, start_utc: utc("22:00"), end_utc: utc("24:00"), start_local: "22:00", end_local: "24:00" },
  ] };
  assert.equal(nextHeating(h, tariff, Date.parse(utc("12:00")), false), "Próximo: aquece 13–16 · depois 16–19 sem aquecer (pico)");
  assert.equal(nextHeating(h, tariff, Date.parse(utc("14:30")), false), "Agora: aquecendo até 16:00 · depois 16–19 sem aquecer (pico)");
  assert.equal(nextHeating(h, tariff, Date.parse(utc("17:00")), false), "Agora: sem aquecer até 19:00 (pico) · volta a aquecer 22:00");
  assert.equal(nextHeating(h, tariff, Date.parse(utc("15:59")), false).startsWith("Agora"), true);
});

test("casa tile: peak coast is not 'Frio'; night window comes from the plan", () => {
  const heating = { indoor_now_c: 20.2, setpoint_c: 21, night_floor_c: 20, peak_coast_delta_c: 1, windows: [], night_start_hour: 22, night_end_hour: 7 };
  const inPeak = cockpit({ now_utc: utc("17:00"), state: { indoor_c: 20.2 } });
  assert.match(casaTile(inPeak, { tariff, heating }).meaning, /^Confortável/);
  const midday = cockpit({ now_utc: utc("11:00"), state: { indoor_c: 20.2 } });
  assert.match(casaTile(midday, { tariff, heating }).meaning, /^Frio · abaixo de 21/);
  // a custom night window (23-06): 22:30 is NOT night
  const h2 = { ...heating, night_start_hour: 23, night_end_hour: 6 };
  assert.match(casaTile(cockpit({ now_utc: utc("22:30"), state: { indoor_c: 20.2 } }), { tariff, heating: h2 }).meaning, /^Frio/);
});

test("água tile: judged per shower on the predicted tank", () => {
  const sh = (s, e, floor, pred) => ({ start_local: s, end_local: e, floor_c: floor, label: null, predicted_tank_c: pred });
  const t = (showers, tank_now_c = 44) => ({ tank: { tank_now_c, windows: [], showers } });
  assert.equal(aguaTile(cockpit(), t([sh("20:00", "22:00", 37, 40)])).meaning, "Pronta para banho (≥ 37 até as 22:00)");
  // second shower fails -> only claim up to the passing one, and warn
  const mixed = aguaTile(cockpit(), t([sh("20:00", "22:00", 37, 40), sh("22:00", "23:30", 43, 38)]));
  assert.equal(mixed.tone, "warn");
  assert.match(mixed.meaning, /até as 22:00\) · pode faltar às 22:00/);
  // first shower predicted short: never "Aquecendo", never a claim
  const short = aguaTile(cockpit(), t([sh("20:00", "22:00", 45, 41)]));
  assert.match(short.meaning, /^Pode faltar para o banho das 20:00 \(prevista 41 °C, ≥ 45\)$/);
  assert.equal(aguaTile(cockpit(), t([])).meaning, "Sem banho previsto hoje");
  assert.equal(aguaTile(cockpit(), { tank: { error: "x" } }).meaning, "Água quente");
});

test("bateria tile and next charge: kWh INTO the battery, away wording from the plan", () => {
  const b = { reserve_pct: 10, windows: [
    { kind: "grid_charge", start_utc: utc("22:00"), end_utc: utc("24:00"), start_local: "22:00", end_local: "24:00", grid_kwh: 3.0, charge_kwh: 2.4 },
  ], by_band: [] };
  assert.equal(nextCharge(b, tariff, NOWMS, false), "Próximo: 22–24 carrega da rede (2,4 kWh a 12,5p)");
  const away = cockpit({ vacation: { active: true } });
  assert.match(bateriaTile(away, { tariff, battery: b }).meaning, /^Chega até/);             // grid charge planned -> normal
  const noGrid = { reserve_pct: 10, windows: [], by_band: [] };
  assert.equal(bateriaTile(away, { tariff, battery: noGrid }).meaning, "Só com sol · piso 10 %");
  assert.equal(bateriaTile(away, { tariff, battery: noGrid }).next, "Próximo: carrega com o sol");
});

test("vacation help line is derived, not hard-coded", () => {
  const vac = { active: true, holiday_enabled: true, recovery_at: "2026-10-31T23:00:00+00:00" };
  const withGrid = { reserve_pct: 12, windows: [{ kind: "grid_charge", start_utc: utc("22:00"), end_utc: utc("24:00") }] };
  const h = vacationHelp(vac, withGrid, NOWMS, TZ);
  assert.match(h, /bateria carrega nas faixas baratas, piso 12 %/);
  assert.match(h, /aquecimento religado sáb 23:00/);
  assert.doesNotMatch(h, /12 h|só com sol/);
  assert.match(vacationHelp({ holiday_enabled: false }, { reserve_pct: 10, windows: [] }, NOWMS, TZ),
    /^Em férias: bateria só com sol, piso 10 %, aquecimento religado antes da chegada\.$/);
});

test("band list (phone): one row per band, each band names the tank window covering most of it", () => {
  const f = { tariff,
    battery: { by_band: [{ start_local: "13:00", end_local: "16:00", planned_import_kwh: 2.4, realised_import_kwh: 0.6 }] },
    heating: { setpoint_c: 21, windows: [{ kind: "boost", offset_c: 3, start_utc: utc("13:00"), end_utc: utc("16:00") }], by_band: [] },
    tank: { windows: [
      { kind: "warmup", start_utc: utc("12:30"), end_utc: utc("15:30"), start_local: "12:30", end_local: "15:30", tank_target_c: 47 },
      { kind: "setback", start_utc: utc("15:30"), end_utc: utc("24:00"), start_local: "15:30", end_local: "24:00", tank_target_c: 37 },
    ] } };
  const rows = bandRows(f, NOWMS, { climate: false, tank: false });
  assert.equal(rows.length, 7);
  const r = rows.find((x) => x.startLocal === "13:00");
  assert.equal(r.current, true);
  assert.equal(r.grid, "0,6 de 2,4 kWh");
  assert.equal(r.heat.text, "aquece +3");
  assert.equal(r.water, "47 °C (aquece)");            // 13-16 overlaps the 12:30-15:30 warmup the most
  assert.equal(rows.filter((x) => x.current).length, 1);
  assert.equal(rows.find((x) => x.startLocal === "19:00").water, "37 °C (economia)");
  const hol = bandRows(f, NOWMS, { climate: true, tank: true });
  assert.equal(hol.find((x) => x.startLocal === "13:00").water, "desligado");
  assert.equal(hol.find((x) => x.startLocal === "13:00").heat.text, "desligado");
});

test("local time labels use the house timezone", () => {
  assert.equal(localHHMM("2026-10-07T13:30:00Z", TZ), "14:30");
  assert.equal(localHHMM("2026-12-07T13:30:00Z", TZ), "13:30");
});
