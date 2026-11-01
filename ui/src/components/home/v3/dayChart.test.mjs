// Unit tests for the Home v3 day chart + flow pills (#898). Run: `npm test`.
import test from "node:test";
import assert from "node:assert/strict";
import {
  localYmd, addDaysYmd, localDayBounds, utcDatesOf, hhmm, buildDaySeries, linePath, areaPath, kwCeil,
  valueAt, daySummary, fullAt, flowModel, flowCaption,
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

test("summary: only real facts, today vs tomorrow wording", () => {
  const base = { pvFc: [], pvAct: [], socAct: [], loadAct: [], loadFc: [], pvSoFarKwh: null, pvForecastKwh: null };
  const plan = [{ t: 0, v: 60 }, { t: 10 * H, v: 100 }, { t: 14 * H, v: 80 }, { t: 17 * H, v: 40 }];
  const peak = { startMs: 14 * H, endMs: 17 * H, label: "16–19", plannedImportKwh: 0 };
  const today = daySummary({ isToday: true, series: { ...base, socPlan: plan, pvCmp: { actual: 6, expected: 6.2 } }, peak, tz: "UTC" });
  assert.match(today, /O sol rendeu quase o previsto\./);
  assert.match(today, /A bateria completa às 10:00\./);
  assert.match(today, /No pico \(16–19\) a bateria vai de 80 % a 40 % sem comprar da rede\./);
  const tomorrow = daySummary({ isToday: false, series: { ...base, socPlan: plan, pvCmp: null }, peak: null, tz: "UTC" });
  assert.match(tomorrow, /^A bateria deve completar às 10:00\.$/);
  assert.equal(daySummary({ isToday: false, series: { ...base, socPlan: [], pvCmp: null }, peak: null }), "");
  const low = daySummary({ isToday: true, series: { ...base, socPlan: [], pvCmp: { actual: 2, expected: 6 } }, peak: null });
  assert.match(low, /menos que o previsto/);
  const part = daySummary({ isToday: true, series: { ...base, socPlan: [{ t: 0, v: 50 }, { t: H, v: 90 }], pvCmp: null }, peak: null, tz: "UTC" });
  assert.match(part, /A bateria chega a 90 % às 01:00\./);
  assert.equal(fullAt({ ...base, socPlan: plan, pvCmp: null }), 10 * H);
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
