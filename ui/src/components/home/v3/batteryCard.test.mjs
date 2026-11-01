import test from "node:test";
import assert from "node:assert/strict";
import { batteryCardModel, stateWord, nextTarget } from "./batteryCard.ts";

const utc = (hhmm) => { const [h, m] = hhmm.split(":").map(Number); return new Date(Date.UTC(2026, 9, 7, h - 1, m)).toISOString(); };
const now = (over = {}) => ({
  now_utc: utc("14:30"), planner_tz: "Europe/London",
  state: { soc_pct: 64, battery_kw: 1.2 },
  current_slot: { fox_mode: "ForceCharge" },
  freshness: { fox: { age_s: 20, stale: false } },
  plan: { pushed_at_utc: utc("14:05") },
  ...over,
});
const fronts = {
  battery: {
    reserve_pct: 15, soc_now_pct: 62,
    windows: [
      { kind: "grid_charge", start_utc: utc("04:00"), end_utc: utc("06:00"), end_local: "06:00", soc_end_pct: 38 },
      { kind: "grid_charge", start_utc: utc("13:30"), end_utc: utc("16:00"), end_local: "16:00", soc_end_pct: 94.2 },
    ],
    by_band: [
      { key: "band_cheap", label: "cheap", start_local: "13:00", end_local: "16:00", planned_import_kwh: 2.4, soc_entry_pct: 62 },
      { key: "band_peak", label: "peak", start_local: "16:00", end_local: "19:00", planned_import_kwh: 0, soc_entry_pct: 94 },
      { key: "band_day", label: "day", start_local: "19:00", end_local: "22:00", planned_import_kwh: 1.1, soc_entry_pct: 50 },
      { key: "band_cheap", label: "cheap", start_local: "22:00", end_local: "24:00", planned_import_kwh: 0.4, soc_entry_pct: 31 },
    ],
  },
};
const row = (m, k) => m.rows.find((r) => r.key === k);

test("charging: word, reserve, next target skips finished windows, rows", () => {
  const m = batteryCardModel(now(), fronts);
  assert.equal(m.stateWord, "carregando");
  assert.equal(m.socPct, 64);
  assert.equal(m.reservePct, 15);
  assert.deepEqual(m.target, { pct: 94, at: "16:00" });
  assert.match(row(m, "agora").text, /Carregando da rede/);
  assert.equal(row(m, "pico").label, "Pico 16–19");
  assert.match(row(m, "pico").text, /sem comprar da rede/);
  assert.match(row(m, "noite").text, /~31% às 22:00/);
  assert.equal(row(m, "inversor").text, "Fox respondendo · último plano enviado 14:05");
  assert.equal(row(m, "inversor").tone, "ok");
});

test("discharging and idle states", () => {
  assert.equal(batteryCardModel(now({ state: { soc_pct: 80, battery_kw: -2 }, current_slot: { fox_mode: "SelfUse" } }), fronts).stateWord, "descarregando");
  const idle = batteryCardModel(now({ state: { soc_pct: 80, battery_kw: 0.01 }, current_slot: { fox_mode: "Backup" } }), fronts);
  assert.equal(idle.stateWord, "parada");
  assert.equal(idle.stateTone, "idle");
  assert.equal(stateWord(null).word, null);
});

test("peak needing grid import warns", () => {
  const f = structuredClone(fronts);
  f.battery.by_band[1].planned_import_kwh = 1.34;
  const r = row(batteryCardModel(now(), f), "pico");
  assert.equal(r.tone, "warn");
  assert.match(r.text, /~1,3 kWh/);
});

test("missing plan hides plan rows, target and reserve; keeps live rows", () => {
  for (const f of [null, undefined, { battery: null }, { battery: { error: "x" } }]) {
    const m = batteryCardModel(now(), f);
    assert.equal(m.target, null);
    assert.equal(m.reservePct, null);
    assert.deepEqual(m.rows.map((r) => r.key), ["agora", "inversor"]);
  }
  assert.equal(nextTarget(null, 0), null);
});

test("unknown fox mode and absent freshness hide their rows", () => {
  const m = batteryCardModel(now({ current_slot: { fox_mode: "Weird" }, freshness: undefined }), fronts);
  assert.equal(row(m, "agora"), undefined);
  assert.equal(row(m, "inversor"), undefined);
});

test("stale Fox warns", () => {
  const m = batteryCardModel(now({ freshness: { fox: { age_s: 1500, stale: true } } }), fronts);
  assert.equal(row(m, "inversor").tone, "warn");
  assert.match(row(m, "inversor").text, /sem resposta há 25 min/);
});

test("schedule divergence warns; unknown (null) does not", () => {
  assert.equal(row(batteryCardModel(now(), fronts, { in_sync: false }), "inversor").tone, "warn");
  assert.equal(row(batteryCardModel(now(), fronts, { in_sync: null }), "inversor").tone, "ok");
});
