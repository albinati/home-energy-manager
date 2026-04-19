# Changelog

## 2026-04-19 — OpenClaw hook-only notifications

- **Breaking:** User-facing notifications no longer use `openclaw message send` (subprocess). All deliveries use **`POST` to `OPENCLAW_HOOKS_URL`** (Gateway `/hooks/agent`). Set **`OPENCLAW_HOOKS_URL`** and **`OPENCLAW_HOOKS_TOKEN`** when `OPENCLAW_NOTIFY_ENABLED=true`.
- **Removed env:** `OPENCLAW_CLI_PATH`, `OPENCLAW_CLI_TIMEOUT_SECONDS`, `OPENCLAW_PLAN_NOTIFY_MODE`.
- **Behaviour:** On hook failure, only stdout logs — no CLI fallback. See `docs/RUNBOOK.md` and `docs/openclaw-nikola-plan-prompt.md`.

## 2026-04-18 — V8 PuLP MILP planner

- **Default planner:** `OPTIMIZER_BACKEND=lp` runs `src/scheduler/lp_optimizer.solve_lp` (PuLP + CBC): battery, grid, PV curtailment, DHW tank + building thermal, COP vs outdoor temperature, discrete HP buckets. Dispatch: `src/scheduler/lp_dispatch.py`. Weather: `forecast_to_lp_inputs()` in `src/weather.py`.
- **Rollback:** `OPTIMIZER_BACKEND=heuristic` restores the price-quantile classifier (`_classify_slots`, overnight consolidation, pre-peak extension). API: `POST /api/v1/optimization/backend` with `{"backend":"heuristic"}` or `"lp"`.
- **Removed:** `TARGET_PRICE_PENCE` and `/api/v1/optimization/target-price` (replaced by backend switch). MCP: `set_optimizer_backend` replaces `set_target_price`.
- **Dependencies:** `pulp>=2.8` in `requirements.txt`.
- **PoC scripts removed:** `pulp_simulation.py`, `pulp_daikin_sim.py`, `run_tomorrow_pulp_solar.py` (superseded by `lp_optimizer.py`).

## 2026-04-17 — Remove V7 optimization stack

- **Single planner:** Only the Bulletproof path (`src/scheduler/optimizer.py`, SQLite, Fox Scheduler V3, heartbeat) schedules hardware. The parallel V7 package (`src/optimization/`: solver, dispatcher, consent, executor) was deleted so two schedulers cannot conflict.
- **Rollback:** An annotated git tag **`pre-v7-removal`** points at the last commit that still contained `src/optimization/`. Restore that tree with:
  `git checkout pre-v7-removal -- src/optimization`
  then rewire imports if you need to run it again.
- **API / MCP:** `/api/v1/optimization/*` and MCP tools keep similar names; **propose** runs `run_optimizer`. Consent **approve/reject** are no-ops. **GET …/plan** returns SQLite + Fox snapshot instead of a 48-slot solver table. **dispatch-preview** returns a retired notice.
- **New modules:** `src/agile_cache.py` (Agile rate cache for tariff tools), `src/presets.py` (`OperationPreset`), `src/config_snapshots.py` (snapshots without V7 consent).
- **Planner (heuristic mode):** cheap band from price quantiles + forecast solar skip. Heartbeat adds **MIN_SOC_RESERVE_PERCENT** vs peak price warning alongside the existing low-SoC alert.
