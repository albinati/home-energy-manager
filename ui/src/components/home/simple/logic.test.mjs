// Unit tests for the pure Home helpers. Run: `npm test` (node --test, no deps;
// logic.ts is loaded through Node's built-in type stripping).
import test from "node:test";
import assert from "node:assert/strict";
import {
  fmt, mergeRuns, dayAxis, frac, waterSegments, tariffSegments, gridLabel, heatingCell,
  batteryRunway, buildStatus, tierOf, billingVerdict, cycleLabel, nextHeating, aguaTile,
} from "./logic.ts";

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

test("fmt uses a decimal comma", () => assert.equal(fmt(6.2), "6,2"));

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
  assert.ok(frac(axis, Date.parse(NOW)) > 0.5);
});

test("water lane merges equal adjacent windows and labels them", () => {
  const axis = dayAxis(tariff);
  const tank = {
    windows: [
      { kind: "warmup", start_utc: utc("13:00"), end_utc: utc("15:00"), start_local: "13:00", end_local: "15:00", tank_target_c: 47 },
      { kind: "warmup", start_utc: utc("15:00"), end_utc: utc("16:00"), start_local: "15:00", end_local: "16:00", tank_target_c: 47 },
      { kind: "setback", start_utc: utc("22:00"), end_utc: utc("24:00"), start_local: "22:00", end_local: "24:00", tank_target_c: 37 },
      { kind: "legionella", start_utc: utc("11:00"), end_utc: utc("12:00"), start_local: "11:00", end_local: "12:00", tank_target_c: 60 },
    ],
  };
  const segs = waterSegments(tank, axis);
  assert.deepEqual(segs.map((s) => s.text), ["60 °C legionella", "47 °C (banhos)", "37 °C (madrugada)"]);
});

test("grid lane wording: realised of planned, goal on peak, plan for future", () => {
  const battery = { by_band: [
    { start_local: "04:00", end_local: "07:00", planned_import_kwh: 6.2, realised_import_kwh: 6.0 },
    { start_local: "16:00", end_local: "19:00", planned_import_kwh: 0, realised_import_kwh: null },
    { start_local: "22:00", end_local: "24:00", planned_import_kwh: 2.1, realised_import_kwh: null },
  ] };
  const [, cheap, , , peak, , night] = tariff.windows;
  assert.equal(gridLabel(cheap, battery), "6,0 de 6,2 kWh");
  assert.equal(gridLabel(peak, battery), "0 (meta)");
  assert.equal(gridLabel(night, battery), "plano 2,1");
});

test("heating lane: boost / hold (night vs day) / off / holiday", () => {
  const heating = {
    setpoint_c: 21, night_floor_c: 20,
    windows: [
      { kind: "boost", offset_c: 3, start_utc: utc("13:00"), end_utc: utc("16:00") },
      { kind: "setback", offset_c: -2, start_utc: utc("16:00"), end_utc: utc("19:00") },
    ],
  };
  const w = tariff.windows;
  assert.deepEqual(heatingCell(w[3], heating, false), { mode: "boost", text: "aquece +3" });
  assert.deepEqual(heatingCell(w[4], heating, false), { mode: "off", text: "desligado" });
  assert.deepEqual(heatingCell(w[5], heating, false), { mode: "hold", text: "mantém 21" });
  assert.deepEqual(heatingCell(w[6], heating, false), { mode: "hold", text: "mantém 20" });
  assert.deepEqual(heatingCell(w[3], heating, true), { mode: "off", text: "desligado" });
});

test("battery runway: reserve crossing beats a later recharge; fallback is the next cheap band", () => {
  const nowMs = Date.parse(NOW);
  const draining = { reserve_pct: 10, windows: [
    { kind: "self_use", start_utc: utc("16:00"), end_utc: utc("19:00"), start_local: "16:00", end_local: "19:00", soc_start_pct: 50, soc_end_pct: 10 },
  ] };
  const r = batteryRunway(draining, tariff, nowMs);
  assert.equal(r.kind, "empty");
  assert.equal(r.at, "19:00");
  const holds = { reserve_pct: 10, windows: [
    { kind: "self_use", start_utc: utc("16:00"), end_utc: utc("19:00"), start_local: "16:00", end_local: "19:00", soc_start_pct: 94, soc_end_pct: 50 },
  ] };
  const r2 = batteryRunway(holds, tariff, nowMs);
  assert.deepEqual(r2, { kind: "recharge", at: "22:00" });
  const late = batteryRunway(holds, tariff, Date.parse(utc("23:00")));
  assert.deepEqual(late, { kind: "recharge", at: "04:00" });
  const charge = { reserve_pct: 10, windows: [
    { kind: "grid_charge", start_utc: utc("15:00"), end_utc: utc("16:00"), start_local: "15:00", end_local: "16:00", soc_start_pct: 60, soc_end_pct: 90 },
    { kind: "grid_charge", start_utc: utc("20:00"), end_utc: utc("21:00"), start_local: "20:00", end_local: "21:00", soc_start_pct: 60, soc_end_pct: 90 },
  ] };
  assert.equal(batteryRunway(charge, tariff, nowMs).at, "15:00");   // earliest upcoming grid charge
  assert.equal(batteryRunway(charge, tariff, Date.parse(utc("15:30"))).at, "20:00");
});

const cockpit = (extra = {}) => ({
  now_utc: NOW, planner_tz: "Europe/London",
  state: { soc_pct: 72, solar_kw: 0, load_kw: 0.5, grid_kw: 0, battery_kw: -0.5, tank_c: 44, indoor_c: 21.1 },
  ...extra,
});
const fronts = {
  tariff,
  battery: { reserve_pct: 10, peak_import_planned_kwh: 0, windows: [], by_band: [
    { start_local: "16:00", end_local: "19:00", planned_import_kwh: 0, realised_import_kwh: null },
  ] },
};

test("status sentence: normal day", () => {
  const s = buildStatus({ now: cockpit(), fronts });
  assert.match(s, /^Tudo normal\. Agora: faixa barato \(12p\) · casa na bateria · próximo evento 16:00 pico, bateria segura a casa até 19:00\.$/);
});

test("status sentence: vacation + holiday", () => {
  const now = cockpit({
    vacation: { active: true, return_at: "2026-11-01T12:00:00+00:00" },
    daikin: { holiday: { climate: true, tank: true } },
  });
  assert.equal(buildStatus({ now, fronts }), "Fora até dom 12:00 · Daikin em holiday · bateria só com sol");
});

test("billing verdict: current is the winner, or a cheaper tariff exists", () => {
  const b = { cycle_start: "2026-10-21", cycle_end: "2026-11-20", current_label: "Cosy",
    compare: [{ tariff: "FIX", label: "British Gas Fixed", delta_gbp: 6.3 }, { tariff: "AG", label: "Agile", delta_gbp: 1.1 }],
    winner: { tariff: "COSY", label: "Cosy", is_current: true } };
  assert.deepEqual(billingVerdict(b), { lead: "Cosy é a tarifa certa", others: "fixo +£6,30 · Agile +£1,10", good: true });
  const w = { ...b, compare: [{ tariff: "AG", label: "Agile", delta_gbp: -0.8 }], winner: { tariff: "AG", label: "Agile", is_current: false } };
  assert.equal(billingVerdict(w).lead, "Agile custaria −£0,80");
  assert.equal(cycleLabel(b), "21 out → 20 nov");
});

test("casa next: peak coast then back to heating", () => {
  const h = { windows: [
    { kind: "setback", offset_c: -2, start_utc: utc("16:00"), end_utc: utc("19:00"), start_local: "16:00", end_local: "19:00" },
    { kind: "boost", offset_c: 3, start_utc: utc("22:00"), end_utc: utc("24:00"), start_local: "22:00", end_local: "24:00" },
  ] };
  assert.equal(nextHeating(h, Date.parse(NOW), false), "Próximo: 16–19 sem aquecer (pico) · volta a aquecer 22:00");
});

test("água tile: ready for showers until the last window", () => {
  const f = { tank: { tank_now_c: 44, windows: [], showers: [
    { start_local: "20:00", end_local: "22:00", floor_c: 37, label: null, predicted_tank_c: 40 },
  ] } };
  assert.equal(aguaTile(cockpit(), f).meaning, "Pronta para banho (≥ 37 até as 22:00)");
});
