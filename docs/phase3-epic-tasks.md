# Phase 3 — Polish (MPC/fetch, calibration hints, comfort, Fox, DHW telemetry)

Epic: [#33 — Phase 3: Polish](https://github.com/albinati/home-energy-manager/issues/33)

Working branch: `chore/phase3-polish`

**Git preference:** one commit per issue on the branch (not one mega-commit).

---

## How to close GitHub issues from a PR

Same as [phase2-epic-tasks.md](./phase2-epic-tasks.md): use `Closes #…` on its own line in the PR description for each completed issue.

---

## Issue [#34](https://github.com/albinati/home-energy-manager/issues/34) — MPC vs Octopus fetch duplicate PuLP

- **Done:** `mpc_should_skip_hour_for_octopus_fetch` + early return in `bulletproof_mpc_job` when local hour equals `OCTOPUS_FETCH_HOUR`; dead code removed after `return total` in `_sync_fox_energy_history`.
- **Tests:** `tests/test_runner_mpc_fetch_coalesce.py`

## Issue [#26](https://github.com/albinati/home-energy-manager/issues/26) — Calibration comments for building model

- **Done:** `# CALIBRATION REQUIRED` (style) comments on `BUILDING_UA_W_PER_K` and `BUILDING_THERMAL_MASS_KWH_PER_K` in `src/config.py`.

## Issue [#25](https://github.com/albinati/home-energy-manager/issues/25) — morning comfort_check logging

- **Done:** Edge-triggered `execution_log` rows with `source=comfort_check` at `LP_OCCUPIED_MORNING_START` / `LP_OCCUPIED_MORNING_END` windows (heartbeat-aligned).
- **Tests:** `tests/test_runner_comfort_morning.py`

## Issue [#23](https://github.com/albinati/home-energy-manager/issues/23) — Fox scheduler read-back after set

- **Done:** `FoxESSClient.warn_if_scheduler_v3_mismatch` after `set_scheduler_v3` in optimizer, `lp_dispatch`, and `state_machine` re-upload paths.
- **Tests:** `tests/test_foxess_scheduler_readback.py`

## Issue [#24](https://github.com/albinati/home-energy-manager/issues/24) — DHW standing loss from logs

- **Done:** `db.estimate_dhw_standing_loss_c_per_hour_p50` and `scripts/print_dhw_standing_loss.py`.
- **Tests:** `tests/test_db_dhw_standing_loss.py`
- **Note:** Requires `daikin_tank_power_on=0` in logs during cooldown; if the heartbeat still logs a constant, calibrate once real tank power is logged.

## Issue [#22](https://github.com/albinati/home-energy-manager/issues/22) — Occupancy-aware DHW

Tracked separately (e.g. `feature/occupancy-dhw-pulp`); not part of this chore branch unless explicitly ported.
