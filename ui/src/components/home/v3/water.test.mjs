// Unit tests for the Home v3 "Água quente" pure helpers. Run: `npm test`.
import test from "node:test";
import assert from "node:assert/strict";
import { barPct, waterHead, afterRow, legionellaRow, boostView, confirmCopy, confirmCopyFromDiff, bandText, hhmm } from "./water.ts";

const NOW = Date.parse("2026-10-07T13:30:00Z"); // 14:30 BST
const win = (kind, s, e, t) => ({ kind, start_utc: s, end_utc: e, start_local: "", end_local: "", tank_target_c: t });
const tk = (over = {}) => ({
  tank_now_c: 42.8, target_now_c: 47, power_on: true, telemetry_at_utc: null, model: null, decision: null,
  windows: [win("warmup", "2026-10-07T12:00:00Z", "2026-10-07T15:00:00Z", 47)], showers: [], next_action: null, ...over,
});

test("barPct clamps to the 30-60 bar", () => {
  assert.equal(barPct(30), 0);
  assert.equal(barPct(45), 50);
  assert.equal(barPct(80), 100);
  assert.equal(barPct(10), 0);
  assert.equal(barPct(null), null);
});

test("waterHead: heating only past the calibrated differential, else just the target", () => {
  const o = { holidayOff: false, differential: 4 };
  assert.deepEqual(waterHead(tk(), 42.8, NOW, o), { tank: 42.8, state: "heating", text: "aquecendo até 47 °C", target: 47 });
  // 5 C under target but inside a 6 C differential: the unit is NOT heating.
  const inside = waterHead(tk(), 42, NOW, { holidayOff: false, differential: 6 });
  assert.equal(inside.state, "holding");
  assert.equal(inside.text, "meta 47 °C");
  // No calibrated differential available: never claim "aquecendo".
  assert.equal(waterHead(tk(), 40, NOW).state, "holding");
});

test("waterHead: setback is economia, power off and holiday say so", () => {
  const sb = tk({ target_now_c: 37, windows: [win("setback", "2026-10-07T12:00:00Z", "2026-10-07T23:00:00Z", 37)] });
  assert.equal(waterHead(sb, 41, NOW).state, "eco");
  assert.equal(waterHead(tk({ power_on: false }), 41, NOW).state, "off");
  assert.equal(waterHead(tk(), 41, NOW, { holidayOff: true }).text, "desligada (modo férias)");
});

test("waterHead: an active boost wins over the plan", () => {
  const h = waterHead(tk(), 44, NOW, { holidayOff: false, boostUntil: "2026-10-07T17:30:00Z", boostTarget: 50 });
  assert.equal(h.state, "boost");
  assert.equal(h.text, "banho extra, meta 50 °C");
});

test("waterHead: no data -> unknown, never invented", () => {
  assert.equal(waterHead(null, null, NOW).state, "unknown");
  assert.equal(waterHead(tk({ target_now_c: null }), 40, NOW).text, "");
});

test("afterRow strips the prefix; boost replaces the planned step", () => {
  assert.equal(afterRow("Próximo: 16:00 baixa para 37", null), "16:00 baixa para 37");
  assert.equal(afterRow("", null), null);
  assert.equal(afterRow("Próximo: 16:00 baixa para 37", { until: "18:30", target: 50 }),
    "Volta ao plano quando o banho extra acabar");
});

test("legionellaRow: next window, hidden when absent or already over", () => {
  const l = { start_utc: "2026-11-08T12:00:00Z", end_utc: "2026-11-08T14:00:00Z" };
  const s = legionellaRow(l, NOW, "Europe/London");
  assert.match(s, /^Próxima dom 08\/11 às 12:00$/);
  assert.equal(legionellaRow(null, NOW, "Europe/London"), null);
  assert.equal(legionellaRow({ start_utc: "2026-10-01T12:00:00Z", end_utc: "2026-10-01T14:00:00Z" }, NOW), null);
  assert.match(legionellaRow({ start_utc: "2026-10-07T13:00:00Z", end_utc: "2026-10-07T15:00:00Z" }, NOW, "Europe/London"), /^Em andamento desde 14:00$/);
});

const base = { active: false, available: true, until_utc: null, target_c: 50, blocked: null };
test("boostView: idle when available and the tank is below the target", () => {
  assert.deepEqual(boostView(base, 44), { kind: "idle" });
});
test("boostView: hidden when the tank is already at the boost target", () => {
  assert.deepEqual(boostView(base, 50.5), { kind: "hidden" });
});
test("boostView: passive, holiday and the rest hide the button", () => {
  for (const error of ["PassiveModeLocked", "HolidayMode", "VacationMode", "ReadOnly", "TankOff", "TankAlreadyHot", "TargetAlreadyHigh", "NoDevice"]) {
    assert.deepEqual(boostView({ ...base, available: false, blocked: { error } }, 44), { kind: "hidden" }, error);
  }
});
test("boostView: quota and legionella say why instead of vanishing", () => {
  assert.match(boostView({ ...base, available: false, blocked: { error: "QuotaExhausted" } }, 44).hint, /cota/);
  assert.match(boostView({ ...base, available: false, blocked: { error: "LegionellaStandoff" } }, 44).hint, /legionela/);
});
test("boostView: a pending restore says why; an unknown tank temperature hides", () => {
  assert.match(boostView({ ...base, available: false, blocked: { error: "RestorePending" } }, 44).hint, /voltando ao plano/);
  assert.deepEqual(boostView({ ...base, available: false, blocked: { error: "TankTempUnknown" } }, 44), { kind: "hidden" });
});
test("boostView: active shows the real end time; unknown status hides", () => {
  assert.deepEqual(boostView({ ...base, active: true, until_utc: "2026-10-07T17:30:00Z" }, 44, "Europe/London"), { kind: "active", until: "18:30" });
  assert.deepEqual(boostView(null, 44), { kind: "hidden" });
});

test("confirmCopy: Powerful wording is honest about the resistance, no price", () => {
  const c = confirmCopy({ target_c: 50, hold_hours: 4, uses_powerful: true }, "18:30", "cheap");
  assert.equal(c, "Liga o modo Powerful e aquece até 50 °C agora (pode usar a resistência, mais cara). O plano não mexe no tanque por 4 h (até 18:30) e depois volta ao normal. Agora é faixa barata.");
  assert.doesNotMatch(c, /£|p\/kWh/);
  const plain = confirmCopy({ target_c: 50, hold_hours: 2.5, uses_powerful: false }, null, undefined);
  assert.match(plain, /^Aquece o tanque até 50 °C agora\./);
  assert.match(plain, /2,5 h/);
  assert.doesNotMatch(plain, /Powerful/);
});

test("bandText maps every cockpit kind explicitly; unknown says nothing", () => {
  assert.match(bandText("negative"), /negativo/);
  assert.match(bandText("solar_charge"), /sol/);
  assert.match(bandText("cheap"), /barata/);
  assert.match(bandText("standard"), /dia/);
  assert.match(bandText("peak"), /pico/);
  assert.match(bandText("peak_export"), /pico/);
  assert.equal(bandText("whatever"), "");
  assert.equal(bandText(undefined), "");
});

test("hhmm renders in the planner timezone", () => {
  assert.equal(hhmm("2026-10-07T13:30:00Z", "Europe/London"), "14:30");
});

test("confirmCopyFromDiff: Powerful wording follows the simulate diff, not the status poll", () => {
  const stale = { target_c: 50, hold_hours: 4, uses_powerful: true };
  assert.doesNotMatch(confirmCopyFromDiff(stale, { after: { powerful: false } }, "18:30", "cheap"), /Powerful/);
  assert.doesNotMatch(confirmCopyFromDiff(stale, { after: {} }, null, undefined), /Powerful/);
  const fresh = { target_c: 50, hold_hours: 4, uses_powerful: false };
  assert.match(confirmCopyFromDiff(fresh, { after: { powerful: true } }, "18:30", "cheap"), /Liga o modo Powerful/);
});

test("confirmCopyFromDiff: the backend hold_until_utc wins over the client estimate", () => {
  const st = { target_c: 50, hold_hours: 4 };
  const c = confirmCopyFromDiff(st, { after: { powerful: false, hold_until_utc: "2026-10-07T17:45:00Z" } }, "99:99", undefined, "Europe/London");
  assert.match(c, /até 18:45\)/);
  assert.doesNotMatch(c, /99:99/);
  assert.match(confirmCopyFromDiff(st, { after: {} }, "18:30", undefined), /até 18:30\)/);
});
