// Unit tests for the Home v3 "Água quente" pure helpers. Run: `npm test`.
import test from "node:test";
import assert from "node:assert/strict";
import { barPct, waterHead, afterRow, legionellaRow, boostView, confirmCopy, hhmm } from "./water.ts";

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

test("waterHead: heating only when a warm-up is open and tank is clearly below target", () => {
  assert.deepEqual(waterHead(tk(), 42.8, NOW), { tank: 42.8, state: "heating", text: "aquecendo até 47 °C", target: 47 });
  assert.equal(waterHead(tk(), 46.5, NOW).state, "holding");
  assert.equal(waterHead(tk(), 46.5, NOW).text, "mantendo 47 °C");
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
    "Banho extra segura a meta até 18:30, depois volta ao plano");
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
test("boostView: active shows the real end time; unknown status hides", () => {
  assert.deepEqual(boostView({ ...base, active: true, until_utc: "2026-10-07T17:30:00Z" }, 44, "Europe/London"), { kind: "active", until: "18:30" });
  assert.deepEqual(boostView(null, 44), { kind: "hidden" });
});

test("confirmCopy states the real hold and never a price", () => {
  const c = confirmCopy({ target_c: 50, hold_hours: 4 }, "18:30", "cheap");
  assert.equal(c, "Aquece o tanque até 50 °C agora. O plano não mexe nele por 4 h (até 18:30) e depois volta ao normal. Agora é faixa barata.");
  assert.doesNotMatch(c, /£|p\/kWh/);
  assert.match(confirmCopy({ target_c: 50, hold_hours: 4 }, null, "peak"), /pico/);
  assert.match(confirmCopy({ target_c: 50, hold_hours: 2.5 }, null, undefined), /2,5 h/);
});

test("hhmm renders in the planner timezone", () => {
  assert.equal(hhmm("2026-10-07T13:30:00Z", "Europe/London"), "14:30");
});
