// Unit tests for the Settings grouping/search helpers. Run: `npm test`.
// The registry-completeness check against src/runtime_settings.py lives in
// tests/test_ui_settings_groups.py (it parses groups.ts).
import test from "node:test";
import assert from "node:assert/strict";
import {
  VISIBLE_GROUPS, ADVANCED_GROUPS, listedKeys, unlistedKeys, duplicateKeys, filterAdvanced, modeLine, labelFor,
  isBoolEnum, enumLabel, parseApiError, isGenericSummary, safetyFlagText, DANGER,
} from "./groups.ts";

const specs = (keys, desc = {}) => new Map(keys.map((k) => [k, { key: k, description: desc[k] ?? "" }]));

test("no key is listed twice", () => {
  assert.deepEqual(duplicateKeys(), []);
});

test("unlistedKeys reports registry keys no group lists", () => {
  assert.deepEqual(unlistedKeys(["INDOOR_SETPOINT_C", "SOME_NEW_KEY"]), ["SOME_NEW_KEY"]);
  assert.deepEqual(unlistedKeys(["DHW_TEMP_NORMAL_C", "REQUIRE_SIMULATION_ID"]), []);
});

test("visible groups hold exactly the agreed keys", () => {
  const byId = Object.fromEntries(VISIBLE_GROUPS.map((g) => [g.id, g.keys]));
  assert.deepEqual(byId["conforto"], [
    "INDOOR_SETPOINT_C", "LP_W3_NIGHT_FLOOR_C", "LP_W3_PEAK_COAST_DELTA_C", "LP_W3_CEILING_C", "INDOOR_COMFORT_AGGREGATE",
  ]);
  assert.deepEqual(byId["conta"], ["OCTOPUS_BILLING_CYCLE_DAY"]);
  assert.deepEqual(byId["agua-quente"], [
    "DHW_TEMP_NORMAL_C", "DHW_TEMP_COMFORT_C", "DHW_SHOWER_COMFORT_C", "DHW_SHOWER_EVENING_START_HOUR",
    "DHW_SHOWER_EVENING_END_HOUR", "DHW_MORNING_RESERVE_C", "DHW_MORNING_RESERVE_START_HOUR", "DHW_MORNING_RESERVE_END_HOUR",
  ]);
});

test("every visible key has a plain-words label", () => {
  for (const g of VISIBLE_GROUPS) for (const k of g.keys) assert.notEqual(labelFor(k), k, k);
});

test("filterAdvanced: empty query keeps keys present in specs, drops unknown ones", () => {
  const all = ADVANCED_GROUPS.flatMap((g) => g.keys);
  const out = filterAdvanced(ADVANCED_GROUPS, specs(all.slice(0, 3)), "");
  assert.deepEqual(out.flatMap((g) => g.keys), all.slice(0, 3));
});

test("filterAdvanced matches key, label, description and ignores accents/case", () => {
  const all = ADVANCED_GROUPS.flatMap((g) => g.keys);
  const m = specs(all, { WEATHER_LAT: "Site latitude for forecasts" });
  assert.deepEqual(filterAdvanced(ADVANCED_GROUPS, m, "daikin_lwt_source").flatMap((g) => g.keys), ["DAIKIN_LWT_SOURCE"]);
  assert.ok(filterAdvanced(ADVANCED_GROUPS, m, "EMERGENCIA").flatMap((g) => g.keys).includes("LP_W3_EMERGENCY_FLOOR_C"));
  assert.ok(filterAdvanced(ADVANCED_GROUPS, m, "latitude forecasts").flatMap((g) => g.keys).includes("WEATHER_LAT"));
  assert.deepEqual(filterAdvanced(ADVANCED_GROUPS, m, "zzzz-nothing"), []);
});

test("modeLine: normal, guests, vacation (+ travel/away aliases)", () => {
  assert.equal(modeLine("normal", ""), "Normal");
  assert.equal(modeLine("guests", ""), "Hóspedes");
  assert.equal(modeLine("vacation", ""), "Férias");
  assert.equal(modeLine("travel", ""), "Férias");
  assert.equal(modeLine("away", "garbage"), "Férias");
  // 2026-11-08 is a Sunday; naive ISO is shown as typed.
  const t = modeLine("vacation", "2026-11-08T12:00");
  assert.match(t, /^Férias até dom/);
  assert.match(t, /08\/11/);
  assert.match(t, /12:00$/);
});

test("modeLine: lower-cased stored value with a z suffix still shows the date", () => {
  assert.match(modeLine("vacation", "2026-11-08t12:00:00z", "Europe/London"), /^Férias até dom\.? .*12:00$/);
});

test("enum helpers", () => {
  assert.equal(isBoolEnum(["true", "false"]), true);
  assert.equal(isBoolEnum(["tier", "lp"]), false);
  assert.equal(enumLabel("OPTIMIZATION_PRESET", "vacation"), "Férias");
  assert.equal(enumLabel("DAIKIN_LWT_SOURCE", "lp"), "lp");
});

test("danger + safety flag copy", () => {
  for (const k of ["DAIKIN_CONTROL_MODE", "OPTIMIZATION_PRESET", "REQUIRE_SIMULATION_ID"]) assert.ok(DANGER[k]);
  assert.match(safetyFlagText("disables_daikin_writes"), /deixa de escrever/);
  assert.equal(safetyFlagText("some_new_flag"), "some new flag");
  assert.equal(isGenericSummary("Change setting FOO: 1 → 2", "FOO"), true);
  assert.equal(isGenericSummary("Troca algo", "FOO"), false);
});

test("parseApiError: plain strings, vacation 422, per-key 409", () => {
  const v = parseApiError(JSON.stringify({ detail: "OPTIMIZATION_PRESET=vacation requires VACATION_RETURN_AT" }), "x");
  assert.equal(v.message, "Férias exige data de volta (defina na Casa).");
  const p = parseApiError(JSON.stringify({ detail: { error: "BatchPartialFailure", results: [
    { key: "A", ok: true }, { key: "B", ok: false, error: "out of range" } ] } }), "x");
  assert.deepEqual(p.byKey, { B: "out of range" });
  assert.match(p.message, /não foram aplicados/);
  assert.equal(parseApiError("not json", "fb").message, "not json");
  assert.equal(parseApiError("", "fb").message, "fb");
});
