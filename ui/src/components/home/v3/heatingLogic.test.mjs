import test from "node:test";
import assert from "node:assert/strict";
import {
  comfortWord, roomChips, lwtWord, pumpLine, controlsDisabledReason, peakRow, targetsLine,
  setpointBounds, stepSetpoint, previewText, feedbackMessage,
} from "./heatingLogic.ts";

const utc = (hhmm) => { const [h, m] = hhmm.split(":").map(Number); return new Date(Date.UTC(2026, 9, 7, h - 1, m)).toISOString(); };
const win = (s, e, label) => ({ key: `band_${label}`, label, start_utc: utc(s), end_utc: utc(e), start_local: s, end_local: e, price_p: 1, status: "upcoming" });
const tariff = { display_name: "Cosy", structure: "banded", windows: [win("13:00", "16:00", "cheap"), win("16:00", "19:00", "peak")] };
const hw = (s, e, kind, offset_c) => ({ kind, offset_c, start_utc: utc(s), end_utc: utc(e), start_local: s, end_local: e, source: "tier" });
const windows = [hw("13:00", "16:00", "boost", 3), hw("16:00", "19:00", "setback", -2), hw("19:00", "22:00", "restore", 0)];
const at = (h) => Date.parse(utc(h));

test("comfort word is read from the casaTile meaning", () => {
  assert.deepEqual(comfortWord("Confortável · meta 21", "ok"), { word: "confortável", tone: "ok" });
  assert.equal(comfortWord("Frio · abaixo de 21", "warn").word, "frio");
});

test("chips: rooms sorted, outdoor last, fallback to state.indoor, stale dropped", () => {
  assert.deepEqual(roomChips({ sala: 21, corredor: 21.6 }, null, 12.4).map((c) => `${c.label}=${c.value}`),
    ["Corredor=21,6", "Sala=21", "Lá fora=12,4"]);
  const fb = [{ room: "quarto", temp_c: 20.8 }, { room: "x", temp_c: 5, stale: true }];
  assert.deepEqual(roomChips(null, fb, null).map((c) => c.label), ["Quarto"]);
  assert.deepEqual(roomChips(null, null, null), []);
});

test("lwt words: boost / setback in peak / restore", () => {
  assert.equal(lwtWord(windows, at("14:30"), false), "um pouco mais quente, guardando calor");
  assert.equal(lwtWord(windows, at("17:00"), true), "mais fria (pico)");
  assert.equal(lwtWord(windows, at("20:00"), false), "normal");
  assert.equal(lwtWord([], at("20:00"), false), "normal");
});

test("pump line: active, passive, holiday; no jargon or signed numbers", () => {
  const base = { controlMode: "active", holidayClimate: false, away: false, daikinMode: "heating", lwtC: 36.2, windows, nowMs: at("14:30"), inPeak: false };
  const act = pumpLine(base);
  assert.match(act, /água do radiador 36 °C \(um pouco mais quente/);
  assert.doesNotMatch(act, /offset|\+\d/);
  assert.match(pumpLine({ ...base, controlMode: "passive" }), /só observa/);
  assert.doesNotMatch(pumpLine({ ...base, controlMode: "passive" }), /mais quente/);
  assert.match(pumpLine({ ...base, holidayClimate: true }), /férias da Daikin/);
  assert.equal(pumpLine({ ...base, daikinMode: null, lwtC: null }), "Sem leitura da bomba");
});

test("controls hidden in passive / holiday / away", () => {
  assert.equal(controlsDisabledReason("active", false, false), null);
  assert.ok(controlsDisabledReason("passive", false, false));
  assert.ok(controlsDisabledReason("active", true, false));
  assert.ok(controlsDisabledReason("active", false, true));
});

test("peak row: pause + predicted at end; hidden once peak is over", () => {
  const r = peakRow(tariff, windows, { at_19_c: 20.44 }, at("14:30"));
  assert.equal(r.label, "Pico 16–19");
  assert.equal(r.text, "Pausa o aquecimento; previsto chegar às 19:00 com 20,4 °C");
  assert.equal(peakRow(tariff, windows, null, at("14:30")).text, "Pausa o aquecimento");
  assert.equal(peakRow(tariff, windows, null, at("20:00")), null);
  assert.equal(peakRow({ windows: [win("13:00", "16:00", "cheap")] }, windows, null, at("14:30")), null);
  assert.equal(peakRow(tariff, [hw("16:00", "19:00", "restore", 0)], null, at("14:30")).text, "Mantém o aquecimento normal");
});

test("targets line from real config", () => {
  assert.equal(targetsLine(21, 17.5), "21 °C de dia · 17,5 °C à noite");
  assert.equal(targetsLine(null, null), null);
});

test("stepper clamps to the setting range and ceiling - 0.5", () => {
  const b = setpointBounds(16, 26, 23);
  assert.deepEqual(b, { min: 16, max: 22.5 });
  assert.equal(stepSetpoint(22.5, 1, b.min, b.max), 22.5);
  assert.equal(stepSetpoint(16, -1, b.min, b.max), 16);
  assert.equal(stepSetpoint(21, 1, 16, 26), 21.5);
  assert.deepEqual(setpointBounds(null, null, null), { min: 16, max: 26 });
});

test("preview says only what simulate knows, warns on shadowed value", () => {
  const p = previewText(21, 21.5, 21, 21.5);
  assert.match(p.text, /de 21 para 21,5 °C/);
  assert.doesNotMatch(p.text, /£/);
  assert.equal(p.warn, null);
  assert.match(previewText(21, 21.5, 20, 21.5).warn, /valor temporário preso/);
});

test("feedback copy is honest about the weekly review", () => {
  assert.match(feedbackMessage("cold"), /com frio/);
  assert.match(feedbackMessage("hot"), /revisão de domingo/);
  assert.match(feedbackMessage("ok"), /Não muda o aquecimento agora/);
});

test("Agile (dynamic) tier keys and end_local 00:00", () => {
  const dyn = { display_name: "Agile", structure: "dynamic", windows: [
    { key: "peak", label: "peak", start_utc: utc("16:00"), end_utc: utc("19:00"), start_local: "16:00", end_local: "19:00", price_p: 30, status: "upcoming" },
  ] };
  assert.equal(peakRow(dyn, windows, null, at("14:30")).label, "Pico 16–19");
  const late = { windows: [{ key: "x", label: "peak", start_utc: utc("22:00"), end_utc: "2026-10-07T23:00:00Z", start_local: "22:00", end_local: "00:00", price_p: 1, status: "upcoming" }] };
  assert.equal(peakRow(late, null, { at_19_c: 20 }, at("14:30")), null);
  assert.equal(peakRow(null, null, null, at("14:30")), null);
});

test("day target hidden from Meta when the stepper shows it", () => {
  assert.equal(targetsLine(21, 17.5, true), "17,5 °C à noite");
  assert.equal(targetsLine(21, null, true), null);
});
