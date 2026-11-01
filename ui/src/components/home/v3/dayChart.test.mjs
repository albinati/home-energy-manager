// Unit tests for the Home v3 day chart + flow pills (#898). Run: `npm test`.
import test from "node:test";
import assert from "node:assert/strict";
import {
  localYmd, addDaysYmd, localDayBounds, utcDatesOf, hhmm, buildDaySeries, linePath, areaPath, kwCeil,
  valueAt, daySummary, fullAt, flowModel, flowCaption, planCoversDay, bandDetail, bandRange, cachedPast, isPastUtcDate,
} from "./dayChart.ts";
import { tierOf, agoraStale, span } from "../simple/logic.ts";

const TZ = "Europe/London";
const H = 3600000;

test("local day bounds: GMT day equals the UTC day", () => {
  const b = localDayBounds("2026-11-01", TZ);
  assert.equal(new Date(b.startMs).toISOString(), "2026-11-01T00:00:00.000Z");
  assert.equal(new Date(b.endMs).toISOString(), "2026-11-02T00:00:00.000Z");
  assert.deepEqual(utcDatesOf(b), ["2026-11-01"]);
});

test("local day bounds: BST day starts at 23:00Z and spans two UTC dates", () => {
  const b = localDayBounds("2026-10-07", TZ);
  assert.equal(new Date(b.startMs).toISOString(), "2026-10-06T23:00:00.000Z");
  assert.equal(b.endMs - b.startMs, 24 * H);
  assert.deepEqual(utcDatesOf(b), ["2026-10-06", "2026-10-07"]);
});

test("DST change days are 25 h / 23 h", () => {
  const fall = localDayBounds("2026-10-25", TZ);
  assert.equal((fall.endMs - fall.startMs) / H, 25);
  const spring = localDayBounds("2026-03-29", TZ);
  assert.equal((spring.endMs - spring.startMs) / H, 23);
});

test("localYmd / addDaysYmd / hhmm follow the house timezone", () => {
  const ms = Date.parse("2026-10-06T23:30:00Z");   // 00:30 BST on the 7th
  assert.equal(localYmd(ms, TZ), "2026-10-07");
  assert.equal(addDaysYmd("2026-10-31", 1), "2026-11-01");
  assert.equal(hhmm(ms, TZ), "00:30");
});

const slot = (iso, o = {}) => ({
  slot_utc: iso, pv_forecast_kwh: 0, pv_planned_kwh: null, pv_actual_kwh: null, load_forecast_kwh: null, ...o,
});

test("series: elapsed slots use plan + actual, future slots use the live forecast", () => {
  const bounds = localDayBounds("2026-11-01", TZ);
  const nowMs = Date.parse("2026-11-01T12:00:00Z");
  const pv = [{
    date: "2026-11-01", now_utc: "", slots: [
      slot("2026-11-01T10:00:00Z", { pv_planned_kwh: 0.4, pv_actual_kwh: 0.3 }),
      slot("2026-11-01T11:00:00Z", { pv_planned_kwh: 0.6, pv_actual_kwh: 0.6 }),
      slot("2026-11-01T13:00:00Z", { pv_forecast_kwh: 0.5, load_forecast_kwh: 0.4 }),
      slot("2026-11-02T01:00:00Z", { pv_forecast_kwh: 9 }),   // other day: ignored
    ],
  }];
  const s = buildDaySeries({ bounds, nowMs, isToday: true, pv, exec: [], timeline: null, capacityKwh: null, socNowPct: null });
  assert.equal(s.pvAct.length, 2);
  assert.equal(s.pvFc.length, 3);
  assert.ok(Math.abs(s.pvSoFarKwh - 0.9) < 1e-9);
  assert.ok(Math.abs(s.pvForecastKwh - 1.5) < 1e-9);
  assert.ok(Math.abs(s.pvCmp.expected - 1.0) < 1e-9);
  assert.equal(s.loadFc.length, 1);
  assert.equal(s.socPlan.length, 0);
});

test("series: merges two UTC days for a BST local day and de-duplicates slots", () => {
  const bounds = localDayBounds("2026-10-07", TZ);
  const a = { date: "2026-10-06", now_utc: "", slots: [slot("2026-10-06T22:30:00Z", { pv_forecast_kwh: 5 }), slot("2026-10-06T23:00:00Z", { pv_forecast_kwh: 0.1 })] };
  const b = { date: "2026-10-07", now_utc: "", slots: [slot("2026-10-07T00:00:00Z", { pv_forecast_kwh: 0.2 })] };
  const s = buildDaySeries({ bounds, nowMs: bounds.startMs - H, isToday: false, pv: [a, b, b], exec: [], timeline: null, capacityKwh: null, socNowPct: null });
  assert.equal(s.pvFc.length, 2);   // 22:30Z is the previous local day
});

test("series: measured SoC from execution log; planned SoC from timeline as % of capacity", () => {
  const bounds = localDayBounds("2026-11-01", TZ);
  const nowMs = Date.parse("2026-11-01T12:00:00Z");
  const exec = [{ date: "2026-11-01", slots: [
    { slot_utc: "2026-11-01T10:00:00Z", soc_percent: 40, consumption_kwh: 0.3 },
    { slot_utc: "2026-11-01T11:30:00Z", soc_percent: 55, consumption_kwh: 0.4 },
    { slot_utc: "2026-11-01T12:00:00Z", soc_percent: 60, consumption_kwh: 0.4 },   // not elapsed
  ] }];
  const timeline = {
    ok: true,
    executed: [{ slot_time_utc: "2026-11-01T10:00:00+00:00", soc_kwh: 4 }],
    ongoing: { slot_time_utc: "2026-11-01T12:00:00+00:00", soc_kwh: 5 },
    planned: [{ slot_time_utc: "2026-11-01T12:30:00+00:00", soc_kwh: 10 }, { slot_time_utc: "2026-11-01T13:00:00+00:00", soc_kwh: null }],
  };
  const s = buildDaySeries({ bounds, nowMs, isToday: true, pv: [], exec, timeline, capacityKwh: 10, socNowPct: 55 });
  assert.deepEqual(s.socAct.map((p) => p.v), [40, 55]);
  assert.equal(s.loadAct.length, 2);
  assert.deepEqual(s.socPlan.map((p) => Math.round(p.v)), [55, 50, 100]);   // now point + future slots only
  assert.equal(s.socPlan[0].t, nowMs);
});

test("series: no capacity means no planned battery line (never guessed)", () => {
  const bounds = localDayBounds("2026-11-02", TZ);
  const timeline = { ok: true, planned: [{ slot_time_utc: "2026-11-02T03:00:00+00:00", soc_kwh: 5 }] };
  const s = buildDaySeries({ bounds, nowMs: bounds.startMs - H, isToday: false, pv: [], exec: [], timeline, capacityKwh: null, socNowPct: null });
  assert.equal(s.socPlan.length, 0);
  const t = buildDaySeries({ bounds, nowMs: bounds.startMs - H, isToday: false, pv: [], exec: [], timeline, capacityKwh: 10, socNowPct: null });
  assert.equal(t.socPlan.length, 1);   // tomorrow: whatever the horizon covers
});

test("paths: gaps split the line, area closes to the baseline, kw ceiling", () => {
  const f = { w: 100, h: 50, pad: 5, startMs: 0, endMs: 100 * 60000 };
  const d = linePath([{ t: 0, v: 0 }, { t: 30 * 60000, v: 1 }, { t: 99 * 60000, v: 1 }], f, 1, 40 * 60000);
  assert.equal((d.match(/M/g) ?? []).length, 2);
  assert.ok(areaPath([{ t: 0, v: 0 }, { t: 60000, v: 1 }], f, 1).endsWith("Z"));
  assert.equal(areaPath([{ t: 0, v: 0 }], f, 1), "");
  assert.equal(kwCeil([[{ t: 0, v: 0.2 }]]), 1);
  assert.equal(kwCeil([[{ t: 0, v: 1.3 }], [{ t: 1, v: 2.1 }]]), 3);
  assert.equal(valueAt([{ t: 0, v: 0 }, { t: 10, v: 10 }], 5), 5);
  assert.equal(valueAt([{ t: 0, v: 0 }], 5), null);
});

const DAY = 24 * H;
const baseSeries = { socPlan: [], pvFc: [], pvAct: [], socAct: [], loadAct: [], loadFc: [], pvSoFarKwh: null, pvForecastKwh: null, pvForecastMissing: false, pvCmp: null };

test("summary: only real facts, today vs tomorrow wording, no peak kWh", () => {
  const plan = [{ t: 0, v: 60 }, { t: 10 * H, v: 100 }, { t: 14 * H, v: 80 }, { t: 17 * H, v: 40 }];
  const peak = { label: "16–19", coverage: { kind: "covered", until: "19:00", kwh: 0 } };
  const today = daySummary({ isToday: true, series: { ...baseSeries, socPlan: plan, pvCmp: { actual: 6, expected: 6.2 } }, peak, endMs: DAY, tz: "UTC" });
  assert.match(today, /O sol rendeu quase o previsto\./);
  // N1: on Hoje the battery + peak clauses belong to BatteryCard / the status line.
  assert.equal(today, "O sol rendeu quase o previsto.");
  const tomorrow = daySummary({ isToday: false, series: { ...baseSeries, socPlan: plan }, peak, endMs: DAY, tz: "UTC" });
  assert.equal(tomorrow, "A bateria deve completar às 10:00. No pico (16–19) a bateria deve segurar a casa sem comprar da rede.");
  assert.doesNotMatch(tomorrow, /kWh/);
  assert.equal(daySummary({ isToday: false, series: { ...baseSeries, socPlan: [] }, peak: null, endMs: DAY }), "");
  const low = daySummary({ isToday: true, series: { ...baseSeries, socPlan: [], pvCmp: { actual: 2, expected: 6 } }, peak: null, endMs: DAY });
  assert.match(low, /menos que o previsto/);
  assert.equal(fullAt({ ...baseSeries, socPlan: plan }), 10 * H);
});

test("summary (#5): peak clause follows peakCoverage and is past tense, never a plan-as-fact", () => {
  const mk = (coverage, isToday = false) => daySummary({ isToday, series: baseSeries, peak: { label: "16–19", coverage }, endMs: DAY });
  assert.equal(mk({ kind: "bought", until: "19:00", kwh: 0.8 }), "No pico (16–19) a casa já comprou da rede.");
  assert.equal(mk({ kind: "buys", until: "19:00", kwh: 1.2 }), "No pico (16–19) o plano compra da rede.");
  assert.equal(mk({ kind: "covered", until: "19:00", kwh: 0 }), "No pico (16–19) a bateria deve segurar a casa sem comprar da rede.");
  assert.equal(mk(null), "");
  assert.equal(mk({ kind: "bought", until: "19:00", kwh: 0.8 }, true), "");   // Hoje: dropped (N1)   // peak over (peakCoverage returns null) or no basis: clause dropped
});

test("summary (#13): max-% claim only when the plan covers the whole day", () => {
  const short = [{ t: 0, v: 50 }, { t: 10 * H, v: 90 }];
  const s = daySummary({ isToday: false, series: { ...baseSeries, socPlan: short }, peak: null, endMs: DAY, tz: "UTC" });
  assert.equal(s, "O plano da bateria vai até 10:00.");
  const whole = [{ t: 0, v: 50 }, { t: 12 * H, v: 90 }, { t: 23.5 * H, v: 60 }];
  assert.equal(daySummary({ isToday: false, series: { ...baseSeries, socPlan: whole }, peak: null, endMs: DAY, tz: "UTC" }), "A bateria deve chegar a 90 % às 12:00.");
  assert.equal(planCoversDay(short, DAY), false);
  assert.equal(planCoversDay(whole, DAY), true);
});

test("series (#6): committed plan wins; an all-zero forecast is missing, not 0 sun", () => {
  const bounds = localDayBounds("2026-11-02", TZ);
  const nowMs = bounds.startMs - H;
  const zero = [{ date: "2026-11-02", now_utc: "", slots: Array.from({ length: 48 }, (_, i) =>
    slot(new Date(bounds.startMs + i * 1800000).toISOString(), { pv_forecast_kwh: 0 })) }];
  const s = buildDaySeries({ bounds, nowMs, isToday: false, pv: zero, exec: [], timeline: null, capacityKwh: null, socNowPct: null });
  assert.equal(s.pvForecastMissing, true);
  assert.equal(s.pvForecastKwh, null);
  assert.equal(s.pvFc.length, 0);
  const planned = [{ date: "2026-11-02", now_utc: "", slots: [slot(new Date(bounds.startMs + 12 * H).toISOString(), { pv_forecast_kwh: 0, pv_planned_kwh: 0.8 })] }];
  const t = buildDaySeries({ bounds, nowMs, isToday: false, pv: planned, exec: [], timeline: null, capacityKwh: null, socNowPct: null });
  assert.equal(t.pvForecastMissing, false);
  assert.ok(Math.abs(t.pvForecastKwh - 0.8) < 1e-9);
});

test("series (#8): load forecast keeps only points after now (no step back)", () => {
  const bounds = localDayBounds("2026-11-01", TZ);
  const nowMs = Date.parse("2026-11-01T12:20:00Z");   // inside the 12:00 slot, whose midpoint is 12:15
  const pv = [{ date: "2026-11-01", now_utc: "", slots: [
    slot("2026-11-01T12:00:00Z", { load_forecast_kwh: 0.5 }),
    slot("2026-11-01T12:30:00Z", { load_forecast_kwh: 0.5 }),
  ] }];
  const s = buildDaySeries({ bounds, nowMs, isToday: true, pv, exec: [], timeline: null, capacityKwh: null, socNowPct: null });
  assert.ok(s.loadFc.every((p) => p.t > nowMs));
  assert.equal(s.loadFc.length, 1);
});

test("bands (#1): detail line is real text with the whole band", () => {
  const peak = bandDetail({ startLocal: "16:00", endLocal: "19:00", tier: "peak", price: "38,2p", heat: { mode: "off", text: "desligado" }, water: "37 °C (economia)" });
  assert.equal(peak, "16–19 · pico 38,2p · aquecimento desligado · água 37 °C (economia)");
  const cheap = bandDetail({ startLocal: "04:00", endLocal: "07:00", tier: "cheap", price: "12,5p", heat: { mode: "boost", text: "aquece +3" }, water: null });
  assert.equal(cheap, "04–07 · barata 12,5p · aquecimento +3");
  const last = bandDetail({ startLocal: "22:00", endLocal: "00:00", tier: "standard", price: null, heat: { mode: "hold", text: "mantém ≥ 20" }, water: "desligado" });
  assert.equal(last, "22–00 · dia · aquecimento mantém ≥ 20 · água desligada");
  assert.equal(bandRange({ startLocal: "04:30", endLocal: "07:00" }), "04:30–07");
});

test("bands (N3): price omitted for the current band on Hoje only", () => {
  const b = { startLocal: "13:00", endLocal: "16:00", tier: "cheap", price: "12,5p", heat: null, water: null };
  assert.equal(bandDetail(b, true), "13–16 · barata");
  assert.equal(bandDetail(b), "13–16 · barata 12,5p");
});

test("cache (N2): a UTC date is past only 3 h after it ends", () => {
  assert.equal(isPastUtcDate("2026-10-06", Date.parse("2026-10-07T02:59:00Z")), false);
  assert.equal(isPastUtcDate("2026-10-06", Date.parse("2026-10-07T03:01:00Z")), true);
  assert.equal(isPastUtcDate("2026-10-07", Date.parse("2026-10-07T13:00:00Z")), false);
});

test("cache (#12): past UTC days fetch once, the current day always refetches", async () => {
  const cache = new Map();
  let calls = 0;
  const f = async () => ++calls;
  assert.equal(await cachedPast(cache, "2026-10-06", true, f), 1);
  assert.equal(await cachedPast(cache, "2026-10-06", true, f), 1);
  assert.equal(await cachedPast(cache, "2026-10-07", false, f), 2);
  assert.equal(await cachedPast(cache, "2026-10-07", false, f), 3);
});

test("flow: sol + rede -> casa + bateria", () => {
  const m = flowModel({ solar_kw: 0.9, grid_kw: 2.3, load_kw: 1.1, battery_kw: 2.1 });
  assert.deepEqual(m.sources.map((p) => p.label), ["Sol", "Rede importa"]);
  assert.deepEqual(m.sinks.map((p) => p.label), ["Casa", "Bateria carrega"]);
  assert.equal(flowCaption(m), "Entra: sol + rede · vai para: casa + bateria.");
});

test("flow: signs flip the direction words", () => {
  const m = flowModel({ solar_kw: 3, grid_kw: -1.2, load_kw: 0.8, battery_kw: -0.5 });
  assert.deepEqual(m.sources.map((p) => p.label), ["Sol", "Bateria descarrega"]);
  assert.deepEqual(m.sinks.map((p) => p.label), ["Casa", "Rede exporta"]);
  assert.equal(m.sinks[1].kw, 1.2);
});

test("flow: idle battery/grid drop out, null readings drop the pill", () => {
  const m = flowModel({ solar_kw: 0, grid_kw: 0.01, load_kw: 0.4, battery_kw: null });
  assert.deepEqual(m.sources.map((p) => p.key), ["sol"]);
  assert.deepEqual(m.sinks.map((p) => p.key), ["casa"]);
  assert.deepEqual(flowModel(null), { sources: [], sinks: [] });
  assert.equal(flowCaption(flowModel(null)), "");
});

test("real payload shapes: null freshness, Agile tier keys, midnight end_local", () => {
  assert.equal(agoraStale({ freshness: { fox: { age_s: null, stale: null } } }), "");
  assert.equal(agoraStale({ freshness: {} }), "");
  assert.equal(tierOf({ key: "agile_peak", label: "peak" }), "peak");
  assert.equal(tierOf({ key: "band_cheap", label: "cheap" }), "cheap");
  assert.equal(span("22:00", "00:00"), "22–00");
});
