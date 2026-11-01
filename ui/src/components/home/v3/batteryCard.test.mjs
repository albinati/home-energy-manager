import test from "node:test";
import assert from "node:assert/strict";
import { batteryCardModel, stateWord, nextTarget, foxModePhrase } from "./batteryCard.ts";

// BST (UTC+1) on 2026-10-07; "24:00"/"00:00" ends roll to the next day.
const utc = (hhmm, day = 7) => { const [h, m] = hhmm.split(":").map(Number); return new Date(Date.UTC(2026, 9, day, h - 1, m)).toISOString(); };
const tw = (key, label, s, e, p) => ({
  key, label, start_utc: utc(s), end_utc: e === "00:00" ? utc("00:00", 8) : utc(e), start_local: s, end_local: e, price_p: p, status: "upcoming",
});
const cosy = (cheap = "band_cheap", peak = "band_peak", day = "band_day") => ({
  display_name: "Cosy", structure: "banded",
  windows: [
    tw(day, "day", "00:00", "04:00", 25.45), tw(cheap, "cheap", "04:00", "07:00", 12.49), tw(day, "day", "07:00", "13:00", 25.45),
    tw(cheap, "cheap", "13:00", "16:00", 12.49), tw(peak, "peak", "16:00", "19:00", 38.17), tw(day, "day", "19:00", "22:00", 25.45),
    tw(cheap, "cheap", "22:00", "00:00", 12.49),
  ],
});
const now = (over = {}) => ({
  now_utc: utc("14:30"), planner_tz: "Europe/London",
  state: { soc_pct: 64, battery_kw: 1.2 },
  current_slot: { fox_mode: "ForceCharge" },
  freshness: { fox: { age_s: 20, fetched_at_utc: utc("14:29"), stale: false }, plan: { age_s: 60, fetched_at_utc: null, stale: false } },
  plan: { pushed_at_utc: utc("14:05") },
  ...over,
});
const win = (kind, s, e, a, b, dis = 0) => ({
  kind, start_utc: utc(s), end_utc: e === "00:00" ? utc("00:00", 8) : utc(e), start_local: s, end_local: e,
  grid_kwh: 0, charge_kwh: 0, discharge_kwh: dis, soc_start_pct: a, soc_end_pct: b, fox_mode: null,
});
const bands = (peakKey = "band_peak", cheapKey = "band_cheap") => [
  { key: cheapKey, label: "cheap", start_local: "13:00", end_local: "16:00", planned_import_kwh: 2.4, realised_import_kwh: 0.6, soc_entry_pct: 62 },
  { key: peakKey, label: "peak", start_local: "16:00", end_local: "19:00", planned_import_kwh: 0, realised_import_kwh: null, soc_entry_pct: 94 },
  { key: "band_day", label: "day", start_local: "19:00", end_local: "22:00", planned_import_kwh: 1.1, realised_import_kwh: null, soc_entry_pct: 50 },
  { key: cheapKey, label: "cheap", start_local: "22:00", end_local: "00:00", planned_import_kwh: 0.4, realised_import_kwh: null, soc_entry_pct: 31 },
];
const mkFronts = (over = {}, tariff = cosy()) => ({
  tariff,
  battery: {
    reserve_pct: 15, soc_now_pct: 62,
    windows: [
      win("grid_charge", "04:00", "06:00", 22, 38),
      win("grid_charge", "13:30", "16:00", 62, 94),
      win("self_use", "16:00", "19:00", 94, 50, 4.6),
      win("grid_charge", "22:00", "00:00", 31, 34),
    ],
    by_band: bands(),
    ...over,
  },
});
const row = (m, k) => m.rows.find((r) => r.key === k);

test("charging: word, reserve, target, rows (Cosy)", () => {
  const m = batteryCardModel(now(), mkFronts());
  assert.equal(m.stateWord, "carregando");
  assert.equal(m.reservePct, 15);
  assert.deepEqual(m.target, { pct: 94, at: "16:00" });
  assert.equal(row(m, "agora").text, "Carregando da rede na faixa barata");
  assert.equal(row(m, "pico").label, "Pico 16–19");
  assert.match(row(m, "pico").text, /sem comprar da rede/);
  assert.equal(row(m, "noite").text, "Chega a ~31% às 22:00 e volta a carregar");
  assert.equal(row(m, "inversor").text, "Fox respondendo");
  assert.ok(!JSON.stringify(m).includes("14:05"), "plan push time stays in the footer");
});

test("discharging and idle states", () => {
  assert.equal(batteryCardModel(now({ state: { soc_pct: 80, battery_kw: -2 }, current_slot: { fox_mode: "SelfUse" } }), mkFronts()).stateWord, "descarregando");
  const idle = batteryCardModel(now({ state: { soc_pct: 80, battery_kw: 0.01 } }), mkFronts());
  assert.equal(idle.stateWord, "parada");
  assert.equal(stateWord(null).word, null);
});

test("Agora band word follows the current band, not a hard-coded cheap", () => {
  const at = (hhmm) => batteryCardModel(now({ now_utc: utc(hhmm) }), mkFronts());
  assert.equal(row(at("10:00"), "agora").text, "Carregando da rede na faixa dia");
  assert.equal(foxModePhrase("ForceCharge", null), "Carregando da rede");
});

test("SoC at the reserve: warn state 'na reserva'", () => {
  const m = batteryCardModel(now({ state: { soc_pct: 15.4, battery_kw: -0.3 } }), mkFronts());
  assert.equal(m.stateWord, "na reserva");
  assert.equal(m.stateTone, "warn");
  assert.equal(batteryCardModel(now({ state: { soc_pct: 16, battery_kw: -0.3 } }), mkFronts()).stateWord, "descarregando");
  const ch = batteryCardModel(now({ state: { soc_pct: 15, battery_kw: 3 } }), mkFronts());
  assert.equal(ch.stateWord, "carregando");
  assert.equal(ch.stateTone, "charge");
});

test("runway warns when the plan empties the battery before the next charge", () => {
  const f = mkFronts({ windows: [win("self_use", "15:00", "20:00", 60, 15, 4), win("grid_charge", "23:00", "00:00", 15, 40)] });
  const r = row(batteryCardModel(now({ state: { soc_pct: 60, battery_kw: -1 } }), f), "autonomia");
  assert.equal(r.tone, "warn");   // reserve crossed 20:00, next charge 23:00 (>2 h)
  const near = mkFronts({ windows: [win("self_use", "15:00", "20:00", 60, 15, 4), win("grid_charge", "21:00", "00:00", 15, 40)] });
  assert.equal(row(batteryCardModel(now({ state: { soc_pct: 60, battery_kw: -1 } }), near), "autonomia").tone, "dim");
  const noRes = mkFronts({ windows: f.battery.windows, reserve_pct: null });
  assert.equal(row(batteryCardModel(now({ state: { soc_pct: 60, battery_kw: -1 } }), noRes), "autonomia"), undefined);
  assert.match(r.text, /reserva às \d\d:\d\d/);
  assert.equal(row(batteryCardModel(now(), mkFronts()), "autonomia"), undefined);
});

test("target: highest SoC of the run, not a PV intermediate or an idle split; stops at the peak", () => {
  const f = mkFronts({
    windows: [
      win("pv_charge", "09:00", "13:00", 38, 68),
      win("grid_charge", "13:00", "14:00", 62, 80),
      win("idle", "14:00", "15:00", 80, 80),
      win("grid_charge", "15:00", "16:00", 80, 94),
      win("self_use", "16:00", "19:00", 94, 50, 4.6),
      win("grid_charge", "22:00", "00:00", 31, 100),
    ],
  });
  assert.deepEqual(nextTarget(f.battery, f.tariff, Date.parse(utc("08:30"))), { pct: 94, at: "16:00" });
  // while discharging in the peak, the next target is the night run ("00:00" end, not "24:00")
  assert.deepEqual(nextTarget(f.battery, f.tariff, Date.parse(utc("17:00"))), { pct: 100, at: "00:00" });
});

test("Pico: Agile tier keys, first peak not ended, 'buys' drops the kWh", () => {
  const agile = cosy("very_cheap", "severe_peak", "mid");
  const f = mkFronts({ by_band: bands("severe_peak", "very_cheap") }, agile);
  assert.equal(row(batteryCardModel(now(), f), "pico").tone, "ok");
  const buys = mkFronts({ by_band: bands("severe_peak", "very_cheap").map((b, i) => (i === 1 ? { ...b, planned_import_kwh: 1.34 } : b)) }, agile);
  const r = row(batteryCardModel(now(), buys), "pico");
  assert.equal(r.text, "Precisa comprar da rede");
  assert.equal(r.tone, "warn");
});

test("Pico: realised import during the peak = bought; hidden after the peak ends", () => {
  const bought = mkFronts({ by_band: bands().map((b, i) => (i === 1 ? { ...b, realised_import_kwh: 0.8 } : b)) });
  const during = batteryCardModel(now({ now_utc: utc("17:00") }), bought);
  assert.match(row(during, "pico").text, /Já comprou/);
  assert.equal(row(batteryCardModel(now({ now_utc: utc("20:00") }), bought), "pico"), undefined);
});

test("Noite: hidden once the band started; 'só com sol' when away; no tail without a grid window", () => {
  assert.equal(row(batteryCardModel(now({ now_utc: utc("22:30") }), mkFronts()), "noite"), undefined);
  const noGrid = mkFronts({ windows: [win("self_use", "16:00", "19:00", 94, 50, 4.6)] });
  assert.equal(row(batteryCardModel(now(), noGrid), "noite").text, "Chega a ~31% às 22:00");
  const away = now({ vacation: { active: true, return_at: null, recovery_at: null, daikin_holiday_until_sent: null, sidecar_ok: null, degraded: false } });
  assert.equal(row(batteryCardModel(away, noGrid), "noite").text, "Chega a ~31% às 22:00 e carrega só com sol");
});

test("Pico/Noite label handles half hours and 00:00 ends", () => {
  const t = cosy();
  t.windows[4] = tw("band_peak", "peak", "16:30", "19:00", 38.17);
  const b = bands().map((x, i) => (i === 1 ? { ...x, start_local: "16:30" } : x));
  assert.equal(row(batteryCardModel(now(), { tariff: t, battery: { ...mkFronts().battery, by_band: b } }), "pico").label, "Pico 16:30–19");
});

test("missing plan hides plan rows, target and reserve; keeps live rows", () => {
  for (const f of [null, undefined, { battery: null }, { battery: { error: "x" } }]) {
    const m = batteryCardModel(now(), f);
    assert.equal(m.target, null);
    assert.equal(m.reservePct, null);
    assert.deepEqual(m.rows.map((r) => r.key), ["agora", "inversor"]);
  }
});

test("Inversor: real null freshness shape, stale, plan stale, actuation stale, drift", () => {
  const withFox = (fox, plan) => now({ freshness: { fox, plan } });
  const nullFox = { age_s: null, fetched_at_utc: null, stale: false };
  const r0 = row(batteryCardModel(withFox(nullFox), mkFronts()), "inversor");
  assert.equal(r0.text, "Fox sem leitura");
  assert.equal(r0.tone, "warn");
  assert.equal(row(batteryCardModel(now({ freshness: undefined }), mkFronts()), "inversor"), undefined);
  // 301 s is stale (shared 300 s threshold) even if the flag is not set
  const r1 = row(batteryCardModel(withFox({ age_s: 301, fetched_at_utc: null, stale: false }), mkFronts()), "inversor");
  assert.equal(r1.tone, "warn");
  assert.match(r1.text, /sem resposta há 5 min/);
  assert.equal(row(batteryCardModel(withFox({ age_s: 1500, fetched_at_utc: null, stale: true }), mkFronts()), "inversor").tone, "warn");
  const fresh = { age_s: 20, fetched_at_utc: null, stale: false };
  assert.equal(row(batteryCardModel(withFox(fresh, { age_s: 9000, fetched_at_utc: null, stale: true }), mkFronts()), "inversor").tone, "warn");
  assert.equal(row(batteryCardModel(now(), mkFronts(), { actuation_stale: true }), "inversor").tone, "warn");
  assert.equal(row(batteryCardModel(now(), mkFronts(), { in_sync: false }), "inversor").tone, "warn");
  assert.equal(row(batteryCardModel(now(), mkFronts(), { in_sync: null }), "inversor").tone, "ok");
});

test("Agile peak run: one 'Pico 16–20' row that sees the severe_peak core; Noite skips a midday/evening dip", () => {
  const keys = ["very_cheap", "expensive", "severe_peak", "expensive", "moderate", "very_cheap"];
  const spec = [["13:00", "16:00"], ["16:00", "17:00"], ["17:00", "19:00"], ["19:00", "20:00"], ["20:00", "22:00"], ["22:00", "00:00"]];
  const tariff = { display_name: "Agile", structure: "dynamic",
    windows: spec.map(([s, e], i) => ({ ...tw(keys[i], keys[i].replace("_", " "), s, e, 20 + i)})) };
  const band = (s, e, planned, soc) => ({ key: "k", label: "l", start_local: s, end_local: e, planned_import_kwh: planned, realised_import_kwh: null, soc_entry_pct: soc });
  const by_band = [band("13:00", "16:00", 0, 90), band("16:00", "17:00", 0, 90), band("17:00", "19:00", 2, 70),
    band("19:00", "20:00", 0, 40), band("20:00", "22:00", 0, 35), band("22:00", "00:00", 0, 30)];
  const m = batteryCardModel(now({ now_utc: utc("12:00") }), mkFronts({ by_band }, tariff));
  assert.equal(row(m, "pico").label, "Pico 16–20");
  assert.equal(row(m, "pico").text, "Precisa comprar da rede");
  // a cheap 20:00-22:00 dip does not overlap the night window: Noite is the 22:00 band
  const dip = { ...tariff, windows: tariff.windows.map((w) => w.start_local === "20:00" ? { ...w, key: "very_cheap", label: "very cheap" } : w) };
  const m2 = batteryCardModel(now({ now_utc: utc("12:00") }), mkFronts({ by_band }, dip));
  assert.equal(row(m2, "noite").text.startsWith("Chega a ~30% às 22:00"), true);
});

test("Cosy Noite row survives the evening after the peak and hides once the cheap band starts", () => {
  assert.equal(row(batteryCardModel(now({ now_utc: utc("20:00") }), mkFronts()), "noite").text, "Chega a ~31% às 22:00 e volta a carregar");
  assert.equal(row(batteryCardModel(now({ now_utc: utc("17:30") }), mkFronts()), "noite").text.startsWith("Chega a ~31% às 22:00"), true);
  assert.equal(row(batteryCardModel(now({ now_utc: utc("22:30") }), mkFronts()), "noite"), undefined);
});
