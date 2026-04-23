# OpenClaw ↔ Home Energy Manager — Sanctioned Surface

**Purpose:** enumerate exactly what OpenClaw may touch, and make the out-of-bounds list explicit so future tools and skills cannot drift.

This is the reference any change to the OpenClaw integration must cite.

---

## The rule

**OpenClaw interacts with Home Energy Manager only through MCP tools exposed by `src/mcp_server.py`.**

Everything else is out-of-bounds:
- **No filesystem writes** — `.env`, `data/`, `src/`, `tests/`, `docs/`, git state.
- **No shell execution** — OpenClaw does not run `bash`, `systemctl`, `sqlite3`, `gh`, or any other process on this host.
- **No direct HTTP** — Fox ESS and Daikin Onecta APIs are reached only via the MCP tool surface (which routes through the service/cache/quota layers).
- **No Python import of `src.*`** — `src.db`, `src.scheduler.*`, `src.daikin.*`, `src.foxess.*`, `src.config` are internal.

Hardware writes are additionally gated by `OPENCLAW_READ_ONLY` (default: `true`) and, per tool, a `confirmed=True` parameter. The MCP server boot-time self-check (Phase 4.5) warns if any hardware-write tool ships without that parameter (`src/mcp_server.py::audit_mcp_tool_surface`).

---

## Sanctioned MCP tool surface

Grouped by side-effect class. "Hardware write" tools require `confirmed=True` **and** `OPENCLAW_READ_ONLY=false`. "Planner write" tools mutate SQLite (plans, consents, settings) but never dial out to Fox or Daikin.

### Read-only (no side effects, no quota burn)

| Tool | Returns |
|---|---|
| `get_soc` | Fox battery SoC, solar/grid/battery power, work mode |
| `get_daikin_status` | Cached Daikin device snapshot |
| `get_schedule` | Today's Daikin action_schedule + Fox V3 snapshot |
| `get_optimization_status` | Preset, backend, consent state, cooldown |
| `get_optimization_plan` | Current 48-slot plan |
| `get_energy_metrics` | Daily/weekly/monthly PnL, VWAP, slippage |
| `get_daily_brief` | Morning report on demand |
| `get_battery_forecast` | SoC + daily target snapshot |
| `get_weather_context` | 48 h forecast + live Daikin temps |
| `get_action_log` | Executed hardware action audit trail |
| `get_optimizer_log` | Optimizer run history |
| `get_config_snapshots` | Saved config rollback points |
| `get_pending_approval` | Pending plan_consent info |
| `get_tariff_recommendation_tool` | Tariff switch suggestion |
| `list_available_tariffs` | Octopus catalogue |
| `get_octopus_account` | Current Octopus account config |
| `auto_detect_octopus_setup` | Account probe (read) |
| `get_occupancy_mcp` | Occupancy settings |
| `simulate_plan` | Read-only "what-if" LP solve (Phase 4.4 — zero hardware, zero quota) |

### Planner write (SQLite only, no hardware dial-out)

| Tool | Mutates |
|---|---|
| `propose_optimization_plan` | Creates a plan row + consent; may auto-apply if `PLAN_AUTO_APPROVE=true` |
| `approve_optimization_plan` / `confirm_plan` | Marks plan_consent as approved |
| `reject_optimization_plan` / `reject_plan` | Marks plan_consent as rejected |
| `set_optimization_preset` | Writes `OPTIMIZATION_PRESET` at runtime |
| `set_optimizer_backend` | Writes `OPTIMIZER_BACKEND` at runtime |
| `set_auto_approve` | Toggles `PLAN_AUTO_APPROVE` |
| `set_occupancy_mcp` | Upserts occupancy settings in SQLite |
| `set_notification_route` | Upserts notification_routes row |
| `list_settings` / `get_setting` | Read-only (listed here because they pair with `set_setting`) |
| `set_setting` | Writes a key in `runtime_settings` (#52 / PR #63); dry-run unless `confirmed=True`. Schedule-class keys also trigger APScheduler cron re-registration. |
| `rollback_config` | Restores config snapshot (preset, thresholds, targets) |

### Hardware write (requires `OPENCLAW_READ_ONLY=false` and `confirmed=True`)

| Tool | Writes to |
|---|---|
| `set_daikin_power` | Daikin — climate on/off |
| `set_daikin_temperature` | Daikin — room setpoint |
| `set_daikin_lwt_offset` | Daikin — leaving-water offset |
| `set_daikin_mode` | Daikin — operation_mode (heating/cooling/auto/...) |
| `set_daikin_tank_temperature` | Daikin — DHW tank setpoint |
| `set_daikin_tank_power` | Daikin — DHW on/off |
| `set_inverter_mode` | Fox ESS — work mode (Self Use / Force charge / ...) |

---

## Out-of-bounds (explicit)

These paths are NOT exposed as tools and must never be. If OpenClaw needs a new capability, add an MCP tool with its own confirmation/quota/caching; do not relax these bounds.

- Editing `.env`, `pyproject.toml`, `src/`, `tests/`, `docs/`, or any file on this host.
- Running any shell command (including read-only ones like `sqlite3`, `cat`, `systemctl status`).
- Git operations (`git push`, `git commit`, `gh pr`, etc.).
- Direct HTTP calls to `api.onecta.daikineurope.com` or `foxesscloud.com` (use MCP tools; they route through the quota-tracked service layer).
- Direct SQL queries against `data/energy_state.db` (use MCP tools).
- Reading the Daikin OAuth token file, Fox API key, Octopus API key, or any credential from disk.

---

## Enforcement

Inside this repo:
- **Boot-time tool audit** (`src/mcp_server.py::audit_mcp_tool_surface`) warns when any hardware-write tool regresses on the `confirmed` gate.
- **`OPENCLAW_READ_ONLY` gate** (`src/api/safeguards.py::audit_log` + each tool's `_daikin_write_preamble` / `_foxess_write_preamble`) blocks all hardware writes when set.
- **Quota layer** (`src/api_quota.py`, `src/daikin/service.py`) limits calls per vendor and serves from cache when possible.

Outside this repo (in OpenClaw's own config):
- **OpenClaw must be configured without filesystem or shell access to `/root/home-energy-manager/`.** The MCP transport is the only sanctioned channel. Specifically: no `bash` tool binding, no `Edit`/`Write` binding targeting this directory, no `git` binding.

See `skills/home-energy-manager/SKILL.md` for the OpenClaw-facing documentation.
