import test from "node:test";
import assert from "node:assert/strict";
import {
  comfortWord, roomChips, lwtWord, pumpLine, stepperHidden, peakRow, targetsLine,
  setpointBounds, stepSetpoint, previewText, feedbackMessage, errorMessage, runApply,
} from "./heatingLogic.ts";

const utc = (hhmm) => { const [h, m] = hhmm.split(":").map(Number); return new Date(Date.UTC(2026, 9, 7, h - 1, m)).toISOString(); };
const win = (s, e, label) => ({ key: `band_${label}`, label, start_utc: utc(s), end_utc: utc(e), start_local: s, end_local: e, price_p: 1, status: "upcoming" });
const tariff = { display_name: "Cosy", structure: "banded", windows: [win("13:00", "16:00", "cheap"), win("16:00", "19:00", "peak")] };
const hw = (s, e, kind, offset_c) => ({ kind, offset_c, start_utc: utc(s), end_utc: utc(e), start_local: s, end_local: e, source: "tier" });
const windows = [hw("13:00", "16:00", "boost", 3), hw("16:00", "19:00", "setback", -2), hw("19:00", "22:00", "restore", 0)];
const at = (h) => Date.parse(utc(h));

test("comfort word: only the three real words, holiday/sensor texts are empty", () => {
  assert.deepEqual(comfortWord("Confortável · meta 21", "ok"), { word: "confortável", tone: "ok" });
  assert.equal(comfortWord("Frio · abaixo de 21", "warn").word, "frio");
  assert.equal(comfortWord("Quente · acima de 23", "warn").word, "quente");
  assert.equal(comfortWord("Aquecimento desligado (modo férias)", "dim").word, "");
  assert.equal(comfortWord("Sem leitura dos sensores", "dim").word, "");
  assert.equal(comfortWord("Temperatura média da casa", "dim").word, "");
});

test("chips: state rooms preferred, <2 rooms hidden, outdoor always kept", () => {
  const st = [{ room: "sala", temp_c: 21, stale: false }, { room: "corredor", temp_c: 21.6 }, { room: "x", temp_c: 5, stale: true }];
  assert.deepEqual(roomChips(st, { a: 1, b: 2 }, 12.4).map((c) => `${c.label}=${c.value}`),
    ["Corredor=21,6", "Sala=21", "Lá fora=12,4"]);
  assert.deepEqual(roomChips([{ room: "sala", temp_c: 21 }], null, 9).map((c) => c.label), ["Lá fora"]);
  assert.deepEqual(roomChips(null, { sala: 21, quarto: 20.8 }, null).map((c) => c.label), ["Quarto", "Sala"]);
  assert.deepEqual(roomChips(null, null, null), []);
  assert.deepEqual(roomChips([{ room: "a", temp_c: null }], { solo: 20 }, null), []);
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

test("stepper hidden in passive / holiday / away", () => {
  assert.equal(stepperHidden("active", false, false), false);
  assert.equal(stepperHidden("passive", false, false), true);
  assert.equal(stepperHidden("active", true, false), true);
  assert.equal(stepperHidden("active", false, true), true);
  assert.equal(stepperHidden(undefined, false, false), false);
});

test("peak row: colder water + predicted at end; hidden once peak is over", () => {
  const r = peakRow(tariff, windows, { at_19_c: 20.44 }, at("14:30"));
  assert.equal(r.label, "Pico 16–19");
  assert.equal(r.text, "Água mais fria no pico (a casa segura o calor); previsto chegar às 19:00 com 20,4 °C");
  assert.doesNotMatch(r.text, /Pausa|Desliga/);
  assert.equal(peakRow(tariff, windows, null, at("14:30")).text, "Água mais fria no pico (a casa segura o calor)");
  assert.equal(peakRow(tariff, windows, null, at("20:00")), null);
  assert.equal(peakRow({ windows: [win("13:00", "16:00", "cheap")] }, windows, null, at("14:30")), null);
  assert.equal(peakRow(tariff, [hw("16:00", "19:00", "restore", 0)], null, at("14:30")).text, "Mantém o aquecimento normal");
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

test("targets line from real config; day hidden when the stepper shows it", () => {
  assert.equal(targetsLine(21, 17.5), "21 °C de dia · 17,5 °C à noite");
  assert.equal(targetsLine(null, null), null);
  assert.equal(targetsLine(21, 17.5, true), "17,5 °C à noite");
  assert.equal(targetsLine(21, null, true), null);
});

test("stepper clamps to spec range and the LIVE ceiling setting, plan ceiling as fallback", () => {
  assert.deepEqual(setpointBounds(16, 26, 23), { min: 16, max: 22.5 });
  assert.deepEqual(setpointBounds(16, 26, 24, 23), { min: 16, max: 23.5 });   // setting wins
  assert.deepEqual(setpointBounds(16, 26, null, 23), { min: 16, max: 22.5 });  // fallback
  assert.deepEqual(setpointBounds(null, null, null, null), { min: 16, max: 26 });
  assert.equal(stepSetpoint(22.5, 1, 16, 22.5), 22.5);
  assert.equal(stepSetpoint(16, -1, 16, 22.5), 16);
  assert.equal(stepSetpoint(21, 1, 16, 26), 21.5);
});

test("preview: what changes, that battery and pump get the new plan, no money", () => {
  const p = previewText(21, 21.5);
  assert.match(p, /de 21 para 21,5 °C/);
  assert.match(p, /bateria e a bomba já recebem o novo plano/);
  assert.doesNotMatch(p, /£|reiniciar|temporário preso/);
});

test("feedback copy matches the backend", () => {
  assert.equal(feedbackMessage("ok"), "Anotado: conforto bom.");
  for (const v of ["cold", "hot"]) {
    assert.equal(feedbackMessage(v),
      "Anotado. Não muda o aquecimento agora; entra no resumo de domingo. Se repetir, pode sugerir ajustar o mínimo da noite ou do pico.");
  }
});

test("error messages are pt-BR, never raw JSON", () => {
  assert.match(errorMessage(409, '{"detail":{"error":"BatchPartialFailure","results":[]}}'), /nada foi alterado/);
  assert.match(errorMessage(409, '{"detail":"SimulationExpired"}'), /expirou/);
  assert.match(errorMessage(400, "INDOOR_SETPOINT_C (24) must be <= ..."), /limite/);
  assert.match(errorMessage(401, ""), /administrador/);
  assert.match(errorMessage(500, "Traceback"), /servidor/);
  assert.equal(errorMessage(null, null), "Algo deu errado. Tente de novo.");
  assert.doesNotMatch(errorMessage(409, '{"detail":{"error":"BatchPartialFailure"}}'), /[{}"]/);
});

const deps = (over = {}) => ({
  apply: async () => ({ ok: true }), replan: async () => ({}), fetchSetpoint: async () => 21.5, ...over,
});

test("runApply: happy path verifies", async () => {
  assert.deepEqual(await runApply(deps(), 21.5), { replanError: null, verified: "yes" });
});

test("runApply: apply failure propagates and nothing else runs", async () => {
  let replanned = false;
  await assert.rejects(runApply(deps({ apply: async () => { throw new Error("x"); }, replan: async () => { replanned = true; } }), 21.5));
  assert.equal(replanned, false);
});

test("runApply: replan failure after save is reported, not thrown", async () => {
  const r = await runApply(deps({ replan: async () => { throw new Error("500"); } }), 21.5);
  assert.ok(r.replanError instanceof Error);
  assert.equal(r.verified, "yes");
});

test("runApply: verification only counts on a fresh successful read", async () => {
  assert.equal((await runApply(deps({ fetchSetpoint: async () => { throw new Error("net"); } }), 21.5)).verified, "unknown");
  assert.equal((await runApply(deps({ fetchSetpoint: async () => null }), 21.5)).verified, "unknown");
  assert.equal((await runApply(deps({ fetchSetpoint: async () => 21 }), 21.5)).verified, "no");
});
