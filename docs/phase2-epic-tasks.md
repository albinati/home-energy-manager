# Phase 2 — Thermodynamic brain (solver & COP)

Epic: [#32 — Phase 2: The Thermodynamic Brain (Physics and COP in PuLP)](https://github.com/albinati/home-energy-manager/issues/32)

Working branch: `feat/phase2-thermodynamic-brain`

Scope (from epic): **thermodynamics and COP in PuLP** — do **not** change dispatcher hardware paths or calendar integrations in this epic unless a fix is unavoidable.

---

## How to close GitHub issues from a PR

### Authors

1. In the **PR description**, link every issue you fully resolve using a [closing keyword](https://docs.github.com/en/issues/tracking-your-work-with-issues/linking-a-pull-request-to-an-issue) on its **own line** (or clearly grouped):

   ```text
   Closes #19
   Closes #29
   ```

   Use **`Closes`**, **`Fixes`**, or **`Resolves`** so GitHub **auto-closes** the issue when the PR merges into the default branch.

2. If the PR only **partially** addresses an issue, use:

   ```text
   Related to #29
   ```

   and **do not** use `Closes` — then the maintainer closes the issue manually after verification.

3. **Stacked / follow-up PRs:** If issue A is done in PR #1 and issue B in PR #2, each PR should only list `Closes` for the issues it actually completes.

### Reviewers / maintainers (before merge)

- [ ] PR description lists the correct `Closes #…` lines for completed work.
- [ ] Required tests (below, per issue) were run or consciously skipped (with reason).
- [ ] After merge: confirm linked issues show as **closed**; if not, close them with a comment referencing the merge commit.

### After merge (verification)

- Re-open the issue only if production or CI shows a regression; reference the new bug issue or revert PR.

---

## Issue [#19](https://github.com/albinati/home-energy-manager/issues/19) — Space heating thermal load in PuLP

**Goal:** Solver accounts for space-heating / climate-curve electrical draw so overnight cold does not strand the battery.

**Status in repo (baseline):** `solve_lp` includes `e_space`, building dynamics, climate-curve `space_floor_kwh` / `space_ceil_kwh`, and `micro_climate_offset_c` on outdoor temperature. `lwt_offset_c` is back-computed for dispatch.

### Tasks

- [x] **Verify** end-to-end: LP plan shows non-zero `space_electric_kwh` / indoor trajectory on a cold-weather fixture (unit or scripted scenario).
- [x] **Tests:** `tests/test_lp_optimizer_space_floor.py` — optimal plan satisfies `e_dhw + e_space >= space_floor` per slot when the climate floor is active.
- [ ] **Docs:** in PR description, note any behaviour change vs previous releases.
- [ ] PR: `Closes #19` only when acceptance above is met.

---

## Issue [#29](https://github.com/albinati/home-energy-manager/issues/29) — Dynamic COP + thermal realism (pre-processing)

**Goal:** COP and effective electrical cost reflect **outdoor conditions** (and, per issue text, implied LWT / setpoint where required) using **Python pre-processing only** — no non-linear COP expressions inside PuLP constraints.

**Status in repo (baseline):** `forecast_to_lp_inputs` fills per-slot `cop_space` / `cop_dhw` from `cop_at_temperature(DAIKIN_COP_CURVE, temp)` and `COP_DHW_PENALTY`. Tank loss is linear in the MILP.

### Tasks

- [x] **Gap analysis:** optional **LWT-lift** multiplier (`LP_COP_LIFT_PENALTY_PER_KELVIN`, default `0` = legacy outdoor-only COP). When `>0`, COP is reduced from implied space LWT (`get_lwt_base_c` + max offset, ceiling from `LP_COP_SPACE_LWT_CEILING_C`) and DHW representative supply (`LP_COP_DHW_LIFT_SUPPLY_C`).
- [x] **Implement:** `physics.apply_cop_lift_multiplier` + wiring in `weather.forecast_to_lp_inputs` (all config-driven).
- [x] **Tests:** `tests/test_physics_cop_lift.py`, `tests/test_weather_cop_preprocess.py`.
- [x] **No PuLP change** — degradation stays in Python before `solve_lp`.
- [ ] PR: `Closes #29` when merged after review.

---

## Issue [#21](https://github.com/albinati/home-energy-manager/issues/21) — Overnight comfort floor + LWT levers

**Goal:** Configurable overnight indoor floor; sensible use of LWT offset as a pre-heat lever.

**Status in repo (baseline):** `LP_OVERNIGHT_COMFORT_FLOOR_C` exists and `_slot_occupancy_bounds` uses it for unoccupied slots. Dispatch uses LP-derived `lwt_offset_c` via `lwt_offset_from_space_kw` in the LP path.

### Tasks

- [x] **Part A:** `_slot_occupancy_bounds` uses `config.LP_OVERNIGHT_COMFORT_FLOOR_C` (no hardcoded 16 °C).
- [x] **Part B:** **Option A (partial):** LP sets `e_space[i]`; dispatch maps to LWT via `lwt_offset_from_space_kw` / `lwt_offset_c` — no extra rule-based tier boosts required for the solver path. Further heuristic boosts remain a product choice.
- [x] **Optional:** `.env.example` documents `LP_OVERNIGHT_COMFORT_FLOOR_C`.
- [ ] PR: `Closes #21` when merged after review.

---

## Issue [#20](https://github.com/albinati/home-energy-manager/issues/20) — Micro-climate calibration

**Goal:** Use divergence between Daikin outdoor sensor and forecast to steer the solver; validate once enough `execution_log` history exists.

**Status in repo (baseline):** `db.get_micro_climate_offset_c()` and `solve_lp(..., micro_climate_offset_c=...)` apply the offset to `t_out`.

### Tasks

- [x] **Operational:** use `DAIKIN_MICRO_CLIMATE_LOOKBACK` (default 96) rows where `daikin_outdoor_temp` and `forecast_temp_c` differ; **2–4 weeks** of diverse weather improves the mean — see issue body.
- [x] **Tooling:** `scripts/print_micro_climate_offset.py` prints the current offset.
- [x] **Tests:** `tests/test_db_micro_climate.py`.
- [ ] PR: `Closes #20` when merged after review.

---

## Suggested test commands (CI / local)

```bash
.venv/bin/python -m pytest tests/test_lp_optimizer.py -q
.venv/bin/python -m pytest tests/ -q -k "lp or weather or physics" --tb=no
```

Run the full suite before merge when touching `lp_optimizer`, `weather`, or `physics`.

---

## PR description template (copy-paste)

```text
## Summary
<!-- What changed and why (1–3 sentences). -->

## Issues
Closes #<!-- issue numbers -->

## How to test
<!-- Commands run, or "manual: checked LP plan on …". -->

## Checklist
- [ ] Tests added/updated
- [ ] No dispatcher-only changes unless required for solver I/O
- [ ] Config: new keys documented / defaults safe
```

Replace `Closes #…` with the actual issues this PR completes.
