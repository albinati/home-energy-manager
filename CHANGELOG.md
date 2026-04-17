# Changelog

## 2026-04-17 — Remove V7 optimization stack

- **Single planner:** Only the Bulletproof path (`src/scheduler/optimizer.py`, SQLite, Fox Scheduler V3, heartbeat) schedules hardware. The parallel V7 package (`src/optimization/`: solver, dispatcher, consent, executor) was deleted so two schedulers cannot conflict.
- **Rollback:** An annotated git tag **`pre-v7-removal`** points at the last commit that still contained `src/optimization/`. Restore that tree with:
  `git checkout pre-v7-removal -- src/optimization`
  then rewire imports if you need to run it again.
- **API / MCP:** `/api/v1/optimization/*` and MCP tools keep similar names; **propose** runs `run_optimizer`. Consent **approve/reject** are no-ops. **GET …/plan** returns SQLite + Fox snapshot instead of a 48-slot solver table. **dispatch-preview** returns a retired notice.
- **New modules:** `src/agile_cache.py` (Agile rate cache for tariff tools), `src/presets.py` (`OperationPreset`), `src/config_snapshots.py` (snapshots without V7 consent).
- **Planner:** `TARGET_PRICE_PENCE` widens the cheap band like the old solver. Heartbeat adds a **MIN_SOC_RESERVE_PERCENT** vs peak price warning alongside the existing low-SoC alert.
