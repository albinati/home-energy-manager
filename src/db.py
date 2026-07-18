"""SQLite persistence for Bulletproof Energy Manager (thread-safe).

Uses stdlib sqlite3 for compatibility with APScheduler and sync device clients.
"""
from __future__ import annotations

import contextlib
import json
import logging
import math
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

from .config import config

_lock = threading.RLock()

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS agile_rates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    valid_from TEXT NOT NULL,
    valid_to TEXT NOT NULL,
    value_inc_vat REAL NOT NULL,
    tariff_code TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    UNIQUE(valid_from, tariff_code)
);

CREATE TABLE IF NOT EXISTS action_schedule (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    device TEXT NOT NULL,
    action_type TEXT NOT NULL,
    params TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    restore_action_id INTEGER,
    created_at TEXT NOT NULL,
    executed_at TEXT,
    error_msg TEXT,
    overridden_by_user_at TEXT,
    FOREIGN KEY (restore_action_id) REFERENCES action_schedule(id)
);

CREATE INDEX IF NOT EXISTS idx_action_schedule_date_device    ON action_schedule(date, device, status);

CREATE TABLE IF NOT EXISTS execution_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    consumption_kwh REAL,
    agile_price_pence REAL,
    svt_shadow_price_pence REAL,
    fixed_shadow_price_pence REAL,
    cost_realised_pence REAL,
    cost_svt_shadow_pence REAL,
    cost_fixed_shadow_pence REAL,
    delta_vs_svt_pence REAL,
    delta_vs_fixed_pence REAL,
    soc_percent REAL,
    fox_mode TEXT,
    daikin_lwt_offset REAL,
    daikin_tank_temp REAL,
    daikin_tank_target REAL,
    daikin_tank_power_on INTEGER,
    daikin_powerful_mode INTEGER,
    daikin_room_temp REAL,
    daikin_outdoor_temp REAL,
    daikin_lwt REAL,
    forecast_temp_c REAL,
    forecast_solar_kw REAL,
    forecast_heating_demand REAL,
    slot_kind TEXT,
    source TEXT
);

CREATE INDEX IF NOT EXISTS idx_execution_log_ts ON execution_log(timestamp);

CREATE TABLE IF NOT EXISTS action_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    device TEXT NOT NULL,
    action TEXT NOT NULL,
    params TEXT,
    result TEXT NOT NULL,
    error_msg TEXT,
    trigger TEXT NOT NULL,
    slot_kind TEXT,
    agile_price_at_time REAL
);

CREATE INDEX IF NOT EXISTS idx_action_log_ts ON action_log(timestamp);

CREATE TABLE IF NOT EXISTS optimizer_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at TEXT NOT NULL,
    rates_count INTEGER,
    cheap_slots INTEGER,
    peak_slots INTEGER,
    standard_slots INTEGER,
    negative_slots INTEGER,
    target_vwap REAL,
    actual_agile_mean REAL,
    battery_warning INTEGER,
    strategy_summary TEXT,
    fox_schedule_uploaded INTEGER,
    daikin_actions_count INTEGER
);

CREATE TABLE IF NOT EXISTS daily_targets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL UNIQUE,
    target_vwap REAL,
    estimated_total_kwh REAL,
    estimated_cost_pence REAL,
    cheap_threshold REAL,
    peak_threshold REAL,
    forecast_min_temp_c REAL,
    forecast_max_temp_c REAL,
    forecast_total_solar_kwh REAL,
    strategy_summary TEXT
);

CREATE TABLE IF NOT EXISTS fox_schedule_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    uploaded_at TEXT NOT NULL,
    groups_json TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    verified_at TEXT
);

CREATE TABLE IF NOT EXISTS acknowledged_warnings (
    warning_key TEXT PRIMARY KEY,
    acknowledged_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS octopus_fetch_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_success_at TEXT,
    last_attempt_at TEXT,
    consecutive_failures INTEGER DEFAULT 0,
    survival_mode_since TEXT
);
INSERT OR IGNORE INTO octopus_fetch_state (id) VALUES (1);

CREATE TABLE IF NOT EXISTS meteo_forecast (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    forecast_date TEXT NOT NULL,
    slot_time TEXT NOT NULL,
    temp_c REAL,
    solar_w_m2 REAL,
    cloud_cover_pct REAL,
    UNIQUE(slot_time)
);

CREATE TABLE IF NOT EXISTS meteo_forecast_snapshot (
    forecast_fetch_at_utc TEXT PRIMARY KEY,
    source TEXT,
    model_name TEXT,
    model_version TEXT,
    raw_payload_json TEXT
);

CREATE TABLE IF NOT EXISTS meteo_forecast_value (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    forecast_fetch_at_utc TEXT NOT NULL,
    slot_time TEXT NOT NULL,
    temp_c REAL,
    solar_w_m2 REAL,
    cloud_cover_pct REAL,
    direct_pv_kw REAL,
    UNIQUE(forecast_fetch_at_utc, slot_time)
);

CREATE INDEX IF NOT EXISTS idx_meteo_forecast_value_slot
ON meteo_forecast_value(slot_time);

CREATE INDEX IF NOT EXISTS idx_meteo_forecast_value_fetch
ON meteo_forecast_value(forecast_fetch_at_utc);

CREATE TABLE IF NOT EXISTS meteo_forecast_latest_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    forecast_fetch_at_utc TEXT
);
INSERT OR IGNORE INTO meteo_forecast_latest_state (id, forecast_fetch_at_utc) VALUES (1, NULL);

CREATE TABLE IF NOT EXISTS api_call_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vendor TEXT NOT NULL,
    kind TEXT NOT NULL,
    ts_utc REAL NOT NULL,
    ok INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_api_call_log_vendor_ts ON api_call_log(vendor, ts_utc);

CREATE TABLE IF NOT EXISTS notification_routes (
    alert_type TEXT PRIMARY KEY,
    enabled INTEGER NOT NULL DEFAULT 1,
    severity TEXT NOT NULL DEFAULT 'reports',
    target_override TEXT,
    channel_override TEXT,
    silent INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL DEFAULT (strftime('%s','now'))
);

CREATE TABLE IF NOT EXISTS plan_consent (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id     TEXT NOT NULL UNIQUE,
    plan_date   TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending_approval',
    proposed_at REAL NOT NULL,
    approved_at REAL,
    rejected_at REAL,
    expires_at  REAL NOT NULL,
    summary     TEXT,
    plan_hash   TEXT,
    last_notified_at REAL,
    created_at  REAL NOT NULL DEFAULT (strftime('%s','now'))
);

CREATE INDEX IF NOT EXISTS idx_plan_consent_date ON plan_consent(plan_date, status);

CREATE TABLE IF NOT EXISTS calendar_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    calendar_id TEXT NOT NULL,
    plan_date TEXT NOT NULL,
    slot_start_utc TEXT NOT NULL,
    slot_end_utc TEXT NOT NULL,
    tier TEXT NOT NULL,
    price_min REAL NOT NULL,
    price_max REAL NOT NULL,
    price_mean REAL NOT NULL,
    google_event_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(calendar_id, slot_start_utc)
);

CREATE INDEX IF NOT EXISTS idx_calendar_events_date ON calendar_events(calendar_id, plan_date);

CREATE TABLE IF NOT EXISTS dispatch_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    slot_time_utc TEXT NOT NULL,
    lp_kind TEXT NOT NULL,
    dispatched_kind TEXT NOT NULL,
    committed INTEGER NOT NULL,
    reason TEXT NOT NULL,
    scen_optimistic_exp_kwh REAL,
    scen_nominal_exp_kwh REAL,
    scen_pessimistic_exp_kwh REAL,
    export_price_p_kwh REAL,
    refill_price_p_kwh REAL,
    economic_margin_p_kwh REAL,
    outgoing_rate_percentile REAL,  -- #274: where the slot's Outgoing rate sat in the LP horizon (0=lowest, 100=highest)
    created_at TEXT NOT NULL,
    UNIQUE(run_id, slot_time_utc)
);

CREATE INDEX IF NOT EXISTS idx_dispatch_decisions_run ON dispatch_decisions(run_id);
CREATE INDEX IF NOT EXISTS idx_dispatch_decisions_slot ON dispatch_decisions(slot_time_utc);

CREATE TABLE IF NOT EXISTS scenario_solve_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL,
    nominal_run_id INTEGER NOT NULL,
    scenario_kind TEXT NOT NULL,         -- 'optimistic' | 'nominal' | 'pessimistic'
    lp_status TEXT NOT NULL,
    objective_pence REAL,
    perturbation_temp_delta_c REAL NOT NULL,
    perturbation_load_factor REAL NOT NULL,
    perturbation_pv_factor REAL,          -- NULL on pre-2026-07 rows (= 1.0, no PV perturbation)
    peak_export_slot_count INTEGER,
    duration_ms INTEGER,
    error TEXT,
    solved_at TEXT NOT NULL,
    UNIQUE(batch_id, scenario_kind)
);

CREATE INDEX IF NOT EXISTS idx_scenario_solve_log_batch ON scenario_solve_log(batch_id);
CREATE INDEX IF NOT EXISTS idx_scenario_solve_log_run ON scenario_solve_log(nominal_run_id);

CREATE TABLE IF NOT EXISTS forecast_skill_log (
    date_utc TEXT NOT NULL,
    hour_of_day INTEGER NOT NULL CHECK(hour_of_day >= 0 AND hour_of_day < 24),
    predicted_temp_c REAL,
    actual_temp_c REAL,
    predicted_pv_kwh REAL,
    actual_pv_kwh REAL,
    predicted_load_kwh REAL,
    actual_load_kwh REAL,
    built_at_utc TEXT NOT NULL,
    PRIMARY KEY (date_utc, hour_of_day)
);

CREATE INDEX IF NOT EXISTS idx_forecast_skill_log_date ON forecast_skill_log(date_utc);

-- daikin_lwt_kw_calibration: rolling regression of observed kwh_heating against
-- the integral of (LWT(t_outdoor) - 18) over each day. Replaces the hardcoded
-- _KW_PER_DEGC_LWT in src/physics.py with a per-installation calibrated value.
-- Single-row table (id = 1, INSERT OR REPLACE). Loader falls back to the
-- module default when the row is missing or k is outside safety bounds.
CREATE TABLE IF NOT EXISTS daikin_lwt_kw_calibration (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    k_per_degc REAL NOT NULL,
    samples INTEGER NOT NULL,
    window_days INTEGER NOT NULL,
    rmse_kwh REAL,
    bias_kwh REAL,
    computed_at TEXT NOT NULL
);
"""


def _db_path() -> Path:
    return Path(config.DB_PATH).expanduser().resolve()


def get_connection() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


def _migrate_schema(conn: sqlite3.Connection) -> None:
    cur = conn.execute("PRAGMA table_info(octopus_fetch_state)")
    cols = {str(r[1]) for r in cur.fetchall()}
    if "failure_streak_started_at" not in cols:
        conn.execute(
            "ALTER TABLE octopus_fetch_state ADD COLUMN failure_streak_started_at TEXT"
        )

    # #714 units fix (2026-07-15): the shadow's delta covers the whole ~48h horizon;
    # the gate normalises it per day. The table shipped hours earlier without the
    # column, so prod needs the ALTER (NULL on old rows → the gate assumes 2.0 days,
    # the conservative read for a 48h horizon).
    cur = conn.execute("PRAGMA table_info(dhw_lp_shadow_log)")
    shadow_cols = {str(r[1]) for r in cur.fetchall()}
    if shadow_cols and "horizon_days" not in shadow_cols:
        conn.execute("ALTER TABLE dhw_lp_shadow_log ADD COLUMN horizon_days REAL")
    if shadow_cols and "terminal_credit_p" not in shadow_cols:
        conn.execute("ALTER TABLE dhw_lp_shadow_log ADD COLUMN terminal_credit_p REAL")

    # 2026-07-02 LP audit — side scenarios now perturb PV too; record the factor
    # so scenario_solve_log rows stay auditable. NULL on pre-migration rows = 1.0.
    ssl_cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(scenario_solve_log)")}
    if ssl_cols and "perturbation_pv_factor" not in ssl_cols:
        conn.execute("ALTER TABLE scenario_solve_log ADD COLUMN perturbation_pv_factor REAL")

    # Phase 4.3: user-override marker on action_schedule rows.
    cur = conn.execute("PRAGMA table_info(action_schedule)")
    as_cols = {str(r[1]) for r in cur.fetchall()}
    if "overridden_by_user_at" not in as_cols:
        conn.execute("ALTER TABLE action_schedule ADD COLUMN overridden_by_user_at TEXT")

    # V2: meteo_forecast (may already exist via SCHEMA constant)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS meteo_forecast (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            forecast_date TEXT NOT NULL,
            slot_time TEXT NOT NULL,
            temp_c REAL,
            solar_w_m2 REAL,
            UNIQUE(slot_time)
        )"""
    )
    # PR #232: cloud-aware PV calibration needs cloud_cover_pct on the live
    # meteo_forecast rows. Older DBs predate this column.
    cur = conn.execute("PRAGMA table_info(meteo_forecast)")
    mf_cols = {str(r[1]) for r in cur.fetchall()}
    if "cloud_cover_pct" not in mf_cols:
        conn.execute("ALTER TABLE meteo_forecast ADD COLUMN cloud_cover_pct REAL")
    # V2 cleanup: pnl_execution_log was a never-populated stale schema (helper
    # functions existed but had zero call sites). Drop it on existing prod DBs.
    conn.execute("DROP TABLE IF EXISTS pnl_execution_log")
    # occupancy_settings — superseded by runtime_settings + presence_periods.
    # No call site since the runtime-settings refactor; drop on existing DBs.
    conn.execute("DROP TABLE IF EXISTS occupancy_settings")

    # V3: fox_energy_daily — actual daily PV, load, import, export from Fox ESS
    conn.execute(
        """CREATE TABLE IF NOT EXISTS fox_energy_daily (
            date TEXT PRIMARY KEY,
            solar_kwh REAL,
            load_kwh REAL,
            import_kwh REAL,
            export_kwh REAL,
            charge_kwh REAL,
            discharge_kwh REAL,
            fetched_at TEXT NOT NULL
        )"""
    )

    # Phase A (#306 follow-up): cache Octopus's smart-meter daily totals
    # alongside the Fox CT-clamp totals so the daily brief can surface a
    # side-by-side audit line (Fox vs meter divergence). Populated by the
    # nightly consumption backfill cron right after V13's half-hour backfill.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS octopus_daily_meter (
            date TEXT PRIMARY KEY,
            import_kwh REAL,
            export_kwh REAL,
            fetched_at TEXT NOT NULL
        )"""
    )

    # V10.2: daikin_consumption_daily — per-day heat-pump energy attribution.
    # Source: 'onecta' (preferred) when Onecta consumption endpoint returns
    # the day, 'telemetry_integral' when computed from daikin_telemetry rows
    # via physics, 'unknown' for legacy rows. Never used for today's row
    # (today is computed live from physics + Fox load).
    conn.execute(
        """CREATE TABLE IF NOT EXISTS daikin_consumption_daily (
            date TEXT PRIMARY KEY,
            kwh_total REAL,
            kwh_heating REAL,
            kwh_dhw REAL,
            cop_daily REAL,
            source TEXT NOT NULL DEFAULT 'unknown',
            fetched_at TEXT NOT NULL
        )"""
    )

    # 2-hourly Daikin consumption (#238). The Onecta API exposes
    # ``consumptionData.value.electrical.<mode>.d`` as a 24-element array =
    # 12 yesterday + 12 today (2-hour buckets). bucket_idx 0 = 00:00–02:00,
    # bucket_idx 11 = 22:00–24:00, both anchored to the user's local TZ.
    # Already in the cached /gateway-devices payload — zero extra Daikin quota.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS daikin_consumption_2hourly (
            date TEXT NOT NULL,
            bucket_idx INTEGER NOT NULL CHECK (bucket_idx BETWEEN 0 AND 11),
            kwh_total REAL,
            kwh_heating REAL,
            kwh_dhw REAL,
            source TEXT NOT NULL DEFAULT 'unknown',
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (date, bucket_idx)
        )"""
    )

    # V4: fox_realtime_snapshot — single-row live telemetry for MPC seeding
    conn.execute(
        """CREATE TABLE IF NOT EXISTS fox_realtime_snapshot (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            captured_at TEXT NOT NULL,
            soc_pct REAL,
            solar_power_kw REAL,
            load_power_kw REAL
        )"""
    )

    # V5: api_call_log — per-vendor HTTP call counter for quota management
    conn.execute(
        """CREATE TABLE IF NOT EXISTS api_call_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vendor TEXT NOT NULL,
            kind TEXT NOT NULL,
            ts_utc REAL NOT NULL,
            ok INTEGER NOT NULL DEFAULT 1
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_api_call_log_vendor_ts ON api_call_log(vendor, ts_utc)"
    )

    # V6: notification_routes — per-AlertType routing config, settable via MCP at runtime
    conn.execute(
        """CREATE TABLE IF NOT EXISTS notification_routes (
            alert_type TEXT PRIMARY KEY,
            enabled INTEGER NOT NULL DEFAULT 1,
            severity TEXT NOT NULL DEFAULT 'reports',
            target_override TEXT,
            channel_override TEXT,
            silent INTEGER NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL DEFAULT (strftime('%s','now'))
        )"""
    )
    # Seed default routes (INSERT OR IGNORE so user overrides survive migrations)
    _NOTIFICATION_DEFAULTS = [
        # (alert_type,          severity,   silent)
        ("risk_alert",          "critical", 0),
        ("critical_error",      "critical", 0),
        ("peak_window_start",   "critical", 0),
        ("cheap_window_start",  "critical", 0),
        ("morning_report",      "reports",  0),
        ("daily_pnl",           "reports",  0),
        ("strategy_update",     "reports",  1),
        ("action_confirmation", "reports",  1),
        # 2026-07-01 (#611 review follow-up): seed rows for types that predated
        # this list or were added without one. Without a row the env fallback
        # still DELIVERS, but severity defaults to 'reports' (wrong channel if
        # a critical target override is ever configured) and the type can't be
        # muted/routed via notification_routes. INSERT OR IGNORE → any user
        # overrides survive.
        ("lp_failure",            "critical", 0),
        ("lp_health_regression",  "critical", 0),
        ("guests_mode_suggested", "reports",  0),
        ("dhw_bias_enable_ready", "reports",  0),
    ]
    for _at, _sev, _sil in _NOTIFICATION_DEFAULTS:
        conn.execute(
            """INSERT OR IGNORE INTO notification_routes
               (alert_type, enabled, severity, silent)
               VALUES (?, 1, ?, ?)""",
            (_at, _sev, _sil),
        )

    # V7: plan_consent — holds proposed plans until the user approves/rejects via MCP
    conn.execute(
        """CREATE TABLE IF NOT EXISTS plan_consent (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_id     TEXT NOT NULL UNIQUE,
            plan_date   TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'pending_approval',
            proposed_at REAL NOT NULL,
            approved_at REAL,
            rejected_at REAL,
            expires_at  REAL NOT NULL,
            summary     TEXT,
            plan_hash   TEXT,
            last_notified_at REAL,
            created_at  REAL NOT NULL DEFAULT (strftime('%s','now'))
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_plan_consent_date ON plan_consent(plan_date, status)"
    )
    # V8: add plan_hash column if missing (existing DBs from V7)
    cur = conn.execute("PRAGMA table_info(plan_consent)")
    pc_cols = {str(r[1]) for r in cur.fetchall()}
    if "plan_hash" not in pc_cols:
        conn.execute("ALTER TABLE plan_consent ADD COLUMN plan_hash TEXT")
    # Seed plan_proposed notification route (critical, audible)
    conn.execute(
        """INSERT OR IGNORE INTO notification_routes
           (alert_type, enabled, severity, silent)
           VALUES ('plan_proposed', 1, 'critical', 0)"""
    )

    # V9: daikin_telemetry — physics-estimator seed + live-fetch audit trail (#55)
    # NOTE: fetched_at is REAL Unix epoch seconds (intentional asymmetry vs the
    # rest of the schema, which uses ISO strings). Productive consumers
    # (estimator.seed_age, service.py rate calcs) need sub-second math; parsing
    # ISO strings on every call would be ~100× slower with no behaviour gain.
    # For human / ad-hoc queries that expect ISO, use the read-only view
    # ``daikin_telemetry_iso`` (defined later in _migrate_schema). Don't write
    # `WHERE fetched_at LIKE 'YYYY-MM-DD%'` — it silently returns 0 rows.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS daikin_telemetry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fetched_at REAL NOT NULL,
            source TEXT NOT NULL,
            tank_temp_c REAL,
            indoor_temp_c REAL,
            outdoor_temp_c REAL,
            tank_target_c REAL,
            lwt_actual_c REAL,
            mode TEXT,
            weather_regulation INTEGER
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_daikin_telemetry_source_ts ON daikin_telemetry(source, fetched_at DESC)"
    )

    # V10: runtime_settings — user-editable tunables that take effect without restart (#52)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS runtime_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )"""
    )

    # V11: durable LP snapshots (cockpit History replay).
    #
    # lp_solution_snapshot holds the per-slot decision vector of each solve so
    # the History view can render "what the LP decided" for any past run.
    # lp_inputs_snapshot holds the scalar inputs + JSON-encoded base_load and
    # config snapshot that fed that solve.
    # Both reference optimizer_log.id as run_id (optimizer_log is written after
    # every successful _run_optimizer_lp with lastrowid returned).
    conn.execute(
        """CREATE TABLE IF NOT EXISTS lp_solution_snapshot (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id          INTEGER NOT NULL,
            slot_index      INTEGER NOT NULL,
            slot_time_utc   TEXT NOT NULL,
            price_p         REAL,
            import_kwh      REAL,
            export_kwh      REAL,
            charge_kwh      REAL,
            discharge_kwh   REAL,
            pv_use_kwh      REAL,
            pv_curtail_kwh  REAL,
            pv_forecast_kwh REAL,
            dhw_kwh         REAL,
            space_kwh       REAL,
            soc_kwh         REAL,
            tank_temp_c     REAL,
            indoor_temp_c   REAL,
            outdoor_temp_c  REAL,
            lwt_offset_c    REAL,
            UNIQUE(run_id, slot_index)
        )"""
    )
    # Additive migration for existing DBs: pv_forecast_kwh = the calibrated
    # per-slot PV-generation forecast the LP committed to (lets the UI show the
    # frozen "committed plan" line distinct from the live forecast). Mirrors the
    # PRAGMA/ALTER idiom used elsewhere in init_db (e.g. meteo_forecast_value).
    lss_cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(lp_solution_snapshot)")}
    if "pv_forecast_kwh" not in lss_cols:
        conn.execute("ALTER TABLE lp_solution_snapshot ADD COLUMN pv_forecast_kwh REAL")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_lp_solution_snapshot_run ON lp_solution_snapshot(run_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_lp_solution_snapshot_slot ON lp_solution_snapshot(slot_time_utc)"
    )

    conn.execute(
        """CREATE TABLE IF NOT EXISTS lp_inputs_snapshot (
            run_id                   INTEGER PRIMARY KEY,
            run_at_utc               TEXT NOT NULL,
            plan_date                TEXT,
            horizon_hours            INTEGER,
            soc_initial_kwh          REAL,
            tank_initial_c           REAL,
            indoor_initial_c         REAL,
            soc_source               TEXT,
            tank_source              TEXT,
            indoor_source            TEXT,
            base_load_json           TEXT,
            micro_climate_offset_c   REAL,
            forecast_fetch_at_utc    TEXT,
            exogenous_snapshot_json  TEXT,
            config_snapshot_json     TEXT,
            price_quantize_p         REAL,
            peak_threshold_p         REAL,
            cheap_threshold_p        REAL,
            daikin_control_mode      TEXT,
            optimization_preset      TEXT,
            energy_strategy_mode     TEXT,
            lp_status                TEXT
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_lp_inputs_snapshot_plan_date ON lp_inputs_snapshot(plan_date)"
    )

    # 2026-05-21: lp_failure_log — append-only audit log of every LP solver
    # failure (Infeasible status, CBC crash, Python exception). The defensive
    # hold-previous-schedule path keeps prod safe; this table is for
    # investigation. Notifier rate-limits the per-failure alert; this table
    # is the full history.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS lp_failure_log (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at_utc          TEXT NOT NULL,
            plan_date           TEXT,
            error_class         TEXT NOT NULL,
            error_msg           TEXT,
            stacktrace          TEXT,
            lp_inputs_run_id    INTEGER,
            resolved_at_utc     TEXT,
            FOREIGN KEY (lp_inputs_run_id) REFERENCES lp_inputs_snapshot(run_id)
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_lp_failure_log_run_at ON lp_failure_log(run_at_utc DESC)"
    )

    # V11b: meteo_forecast_history — append-only audit log of every forecast
    # fetch. The existing meteo_forecast table is latest-per-slot (overwrites on
    # UNIQUE(slot_time)) so heartbeat + LP reads stay simple; this companion
    # table preserves the full fetch history so the cockpit can show "what did
    # the LP think the weather would be on date D when it ran at 10:00?".
    conn.execute(
        """CREATE TABLE IF NOT EXISTS meteo_forecast_history (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            forecast_fetch_at_utc TEXT NOT NULL,
            slot_time             TEXT NOT NULL,
            temp_c                REAL,
            solar_w_m2            REAL,
            UNIQUE(forecast_fetch_at_utc, slot_time)
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_meteo_forecast_history_slot ON meteo_forecast_history(slot_time)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_meteo_forecast_history_fetch ON meteo_forecast_history(forecast_fetch_at_utc)"
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS meteo_forecast_snapshot (
            forecast_fetch_at_utc TEXT PRIMARY KEY,
            source TEXT,
            model_name TEXT,
            model_version TEXT,
            raw_payload_json TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS meteo_forecast_value (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            forecast_fetch_at_utc TEXT NOT NULL,
            slot_time TEXT NOT NULL,
            temp_c REAL,
            solar_w_m2 REAL,
            cloud_cover_pct REAL,
            direct_pv_kw REAL,
            UNIQUE(forecast_fetch_at_utc, slot_time)
        )"""
    )
    cur = conn.execute("PRAGMA table_info(meteo_forecast_value)")
    mfv_cols = {str(r[1]) for r in cur.fetchall()}
    if "direct_pv_kw" not in mfv_cols:
        conn.execute("ALTER TABLE meteo_forecast_value ADD COLUMN direct_pv_kw REAL")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_meteo_forecast_value_slot ON meteo_forecast_value(slot_time)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_meteo_forecast_value_fetch ON meteo_forecast_value(forecast_fetch_at_utc)"
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS meteo_forecast_latest_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            forecast_fetch_at_utc TEXT
        )"""
    )
    conn.execute(
        "INSERT OR IGNORE INTO meteo_forecast_latest_state (id, forecast_fetch_at_utc) VALUES (1, NULL)"
    )

    # V11c: config_audit — append-only log of runtime_settings changes so a
    # past plan can always be explained even if a tunable was changed later.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS config_audit (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            key            TEXT NOT NULL,
            value          TEXT,
            op             TEXT NOT NULL,
            actor          TEXT,
            changed_at_utc TEXT NOT NULL
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_config_audit_key_ts ON config_audit(key, changed_at_utc DESC)"
    )

    # V12: action_log duration tracking. Existing ``timestamp`` is still the
    # "when this row was written" column; new ``started_at`` + ``completed_at``
    # + ``duration_ms`` are populated only by the log_action_timed() path so
    # the cockpit can show "propose_optimization_plan ran 1.23s ago, took
    # 312ms". ``actor`` captures who fired the action (mcp / api / heartbeat
    # / optimizer). Nullable so legacy rows + the fast-path log_action stay
    # compatible.
    cur = conn.execute("PRAGMA table_info(action_log)")
    al_cols = {str(r[1]) for r in cur.fetchall()}
    for col, typ in (
        ("started_at", "TEXT"),
        ("completed_at", "TEXT"),
        ("duration_ms", "INTEGER"),
        ("actor", "TEXT"),
    ):
        if col not in al_cols:
            conn.execute(f"ALTER TABLE action_log ADD COLUMN {col} {typ}")

    # V13: plan_consent.last_notified_at — time of last Telegram/Discord ping
    # for this plan_date. Used by _write_plan_consent to debounce notifications
    # so MPC re-plans don't spam the user when the plan hash changes frequently.
    # Unrelated to proposed_at (which tracks when the optimizer last upserted).
    cur = conn.execute("PRAGMA table_info(plan_consent)")
    pc_cols_v13 = {str(r[1]) for r in cur.fetchall()}
    if "last_notified_at" not in pc_cols_v13:
        conn.execute("ALTER TABLE plan_consent ADD COLUMN last_notified_at REAL")

    # V14: agile_export_rates — Octopus Outgoing Agile half-hourly tariff. Mirrors
    # agile_rates schema 1:1 so the LP can treat export pricing as time-varying just
    # like import. Without this, the LP previously used a flat EXPORT_RATE_PENCE
    # constant and missed the ±20p/kWh swings in Outgoing Agile (peak hours pay much
    # more than overnight, which inverts the optimal export-vs-store trade-off
    # depending on the slot).
    conn.execute(
        """CREATE TABLE IF NOT EXISTS agile_export_rates (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            valid_from      TEXT NOT NULL,
            valid_to        TEXT NOT NULL,
            value_inc_vat   REAL NOT NULL,
            tariff_code     TEXT NOT NULL,
            fetched_at      TEXT NOT NULL,
            UNIQUE(valid_from, tariff_code)
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agile_export_rates_valid_from ON agile_export_rates(valid_from)"
    )

    # V14: pv_realtime_history — append-only per-sample telemetry for PV
    # calibration analysis. Backfilled from Fox CSV exports (5-min) and topped
    # up forward by bulletproof_pv_telemetry_job (30-min default). Columns
    # mirror the Fox webapp exports + heartbeat realtime so both sources can
    # populate the same table; ``source`` distinguishes them.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS pv_realtime_history (
            captured_at            TEXT PRIMARY KEY,
            solar_power_kw         REAL,
            soc_pct                REAL,
            load_power_kw          REAL,
            grid_import_kw         REAL,
            grid_export_kw         REAL,
            battery_charge_kw      REAL,
            battery_discharge_kw   REAL,
            source                 TEXT NOT NULL
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pv_rt_hist_time ON pv_realtime_history(captured_at)"
    )

    # V15: pv_calibration_hourly — cached per-hour-of-day PV calibration factors.
    # Recomputed by ``compute_pv_calibration_hourly_table`` (daily cron + on-demand).
    # The LP reads from this table on every solve; falls back to the flat
    # ``compute_pv_calibration_factor`` when the table is empty (cold start, < 7 days
    # of telemetry, etc).
    conn.execute(
        """CREATE TABLE IF NOT EXISTS pv_calibration_hourly (
            hour_utc        INTEGER PRIMARY KEY CHECK(hour_utc >= 0 AND hour_utc < 24),
            factor          REAL NOT NULL,
            samples         INTEGER NOT NULL,
            window_days     INTEGER NOT NULL,
            computed_at     TEXT NOT NULL
        )"""
    )

    # V16 (PR #232): pv_calibration_hourly_cloud — sister table that splits
    # the per-hour factor by cloud-cover bucket. Captures the "sunny day vs
    # cloudy day" first-order signal that the per-hour table averages out.
    # Buckets:
    #   0 = clear     (cloud_cover_pct in [0, 25))
    #   1 = partly    (cloud_cover_pct in [25, 50))
    #   2 = mostly    (cloud_cover_pct in [50, 75))
    #   3 = overcast  (cloud_cover_pct in [75, 100])
    # When (hour, bucket) has too few samples, callers fall back to the
    # per-hour table; when even that is empty, flat compute_pv_calibration_factor.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS pv_calibration_hourly_cloud (
            hour_utc        INTEGER NOT NULL CHECK(hour_utc >= 0 AND hour_utc < 24),
            cloud_bucket    INTEGER NOT NULL CHECK(cloud_bucket >= 0 AND cloud_bucket < 4),
            factor          REAL NOT NULL,
            samples         INTEGER NOT NULL,
            window_days     INTEGER NOT NULL,
            computed_at     TEXT NOT NULL,
            PRIMARY KEY (hour_utc, cloud_bucket)
        )"""
    )

    # PR L3 (2026-05-24): pv_calibration_3d — 3-dimensional table that adds
    # solar elevation as a binning dimension on top of (hour, cloud_bucket).
    # Separates winter-noon (elev ~10°) from summer-noon (elev ~60°) which
    # the 2D table averages together → masks structurally-different bias
    # patterns (angle-of-incidence + obstruction-shadow geometry both
    # depend on sun position, not just clock hour). Lookup chain:
    # 3d → 2d → 1d → flat.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS pv_calibration_3d (
            hour_utc        INTEGER NOT NULL CHECK(hour_utc >= 0 AND hour_utc < 24),
            cloud_bucket    INTEGER NOT NULL CHECK(cloud_bucket >= 0 AND cloud_bucket < 4),
            elevation_bucket INTEGER NOT NULL CHECK(elevation_bucket >= 0 AND elevation_bucket < 5),
            factor          REAL NOT NULL,
            samples         INTEGER NOT NULL,
            window_days     INTEGER NOT NULL,
            computed_at     TEXT NOT NULL,
            PRIMARY KEY (hour_utc, cloud_bucket, elevation_bucket)
        )"""
    )

    # #486: pv_recent_bias — adaptive closed-loop corrector. Per UTC hour, the
    # damped recency-weighted mean of actual/forecast from ``pv_error_log``
    # (the COMMITTED forecast's own realised error). Applied as a final nudge
    # on the day-ahead PV forecast; self-converges as the error shrinks.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS pv_recent_bias (
            hour_utc        INTEGER PRIMARY KEY CHECK(hour_utc >= 0 AND hour_utc < 24),
            factor          REAL NOT NULL,
            raw_ratio       REAL,
            samples         INTEGER NOT NULL,
            computed_at     TEXT NOT NULL
        )"""
    )

    # Phase-2 load corrector — ADDITIVE per-LOCAL-hour correction (kWh/slot) on
    # the residual base-load forecast, from the damped recency-weighted mean
    # (actual − committed_forecast) in load_error_log. Refreshed nightly; only
    # APPLIED to the LP when LOAD_RECENT_BIAS_ENABLED.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS load_recent_bias (
            hour_local      INTEGER PRIMARY KEY CHECK(hour_local >= 0 AND hour_local < 24),
            bias_kwh        REAL NOT NULL,
            raw_bias_kwh    REAL,
            samples         INTEGER NOT NULL,
            computed_at     TEXT NOT NULL
        )"""
    )

    # DHW bucket-bias corrector — MULTIPLICATIVE per-LOCAL-2h-bucket factor on
    # the pinned DHW forecast, from the damped recency-weighted ratio-of-sums
    # (actual/forecast) in dhw_error_log. Shape-only: application normalizes by
    # the mode's nominal bucket shares so the daily total (the auto-scale's
    # job) is preserved. Refreshed nightly; only APPLIED to the forecast when
    # DHW_BUCKET_BIAS_ENABLED.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS dhw_bucket_bias (
            bucket_idx      INTEGER PRIMARY KEY CHECK(bucket_idx BETWEEN 0 AND 11),
            factor          REAL NOT NULL,
            raw_ratio       REAL,
            samples         INTEGER NOT NULL,
            days            INTEGER,
            computed_at     TEXT NOT NULL
        )"""
    )

    # Winter thermal #540 W1 — indoor temperature ingestion. The Altherma has no
    # room stat (daikin_room_temp is 100% NULL), so the house's indoor temp has
    # never been measured. This is the pipe the user's room sensors push into;
    # multi-room from day one. Idempotent on (captured_at, room). Read by the LP
    # initial state + the dispatch comfort guard (no-op until fresh rows arrive)
    # and, later, the W2 thermal learner.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS room_temperature_history (
            captured_at TEXT NOT NULL,
            room        TEXT NOT NULL DEFAULT 'home',
            temp_c      REAL NOT NULL,
            source      TEXT,
            quality     TEXT,
            PRIMARY KEY (captured_at, room)
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_room_temp_captured ON room_temperature_history(captured_at)"
    )
    # WARM tier (#540): permanent 15-min rollup of indoor readings. Tiny
    # (~4/h/room forever), kept after the raw is pruned, so the UI can draw
    # long-term indoor trends without unzipping the cold archive.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS room_temperature_rollup_15min (
            bucket_utc TEXT NOT NULL,
            room       TEXT NOT NULL DEFAULT 'home',
            mean_c     REAL NOT NULL,
            min_c      REAL NOT NULL,
            max_c      REAL NOT NULL,
            n          INTEGER NOT NULL,
            PRIMARY KEY (bucket_utc, room)
        )"""
    )

    # #540 W1c — full per-device sensor log. room_temperature_history keeps ONLY
    # temp_c (what the LP/thermal model needs); this table is the lossless audit
    # of EVERYTHING a device sends (humidity, pressure, a 2nd temperature, MAC,
    # device_id, …). Typed columns for the known/queryable metrics + payload_json
    # for anything else, so a new sensor field is preserved with no migration.
    # Append-per-distinct-reading: idempotent on (device_key, captured_at) so a
    # retry storm doesn't duplicate, where device_key = mac|device_id|source|room.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS device_reading_log (
            received_at  TEXT NOT NULL,   -- server receipt (UTC), always set
            captured_at  TEXT,            -- device timestamp (UTC) if sent, else NULL
            dedup_key    TEXT NOT NULL,   -- captured_at, or received_at when the
                                          -- device sent no/unparseable timestamp.
                                          -- NEVER NULL, so the PK actually dedups
                                          -- (SQLite treats NULLs in a PK as all
                                          -- distinct → a NULL captured_at would
                                          -- never collapse retries).
            device_key   TEXT NOT NULL,   -- mac|device_id|source|room, for dedup + grouping
            device_id    TEXT,
            mac          TEXT,
            room         TEXT,
            source       TEXT,
            temp_c       REAL,
            humidity_pct REAL,
            pressure_hpa REAL,
            payload_json TEXT NOT NULL,   -- the FULL raw reading, lossless
            PRIMARY KEY (device_key, dedup_key)
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_device_log_device ON device_reading_log(device_key, dedup_key)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_device_log_received ON device_reading_log(received_at)"
    )

    # V14: presence_periods — manually-flagged periods of household presence
    # (home / travel / guests) so future load-pattern analyses + LP calibration
    # can de-bias the rolling load profile by occupancy. Read by analytics
    # only at this stage — LP does not consume yet.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS presence_periods (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            start_utc   TEXT NOT NULL,
            end_utc     TEXT NOT NULL,
            kind        TEXT NOT NULL,    -- 'home' | 'travel' | 'guests'
            note        TEXT,
            created_at  TEXT NOT NULL
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_presence_periods_range ON presence_periods(start_utc, end_utc)"
    )

    # V16: smart appliance scheduling. The `appliances` table is the catalogue
    # of managed devices (Samsung washer first, dishwasher/dryer later); the
    # `appliance_jobs` table records each "armed by remote-mode → fired or
    # cancelled" lifecycle. Trigger model: the LP solve queries SmartThings
    # remoteControlEnabled per enabled appliance; when true, an appliance_jobs
    # row is created/updated and an APScheduler one-shot DateTrigger cron is
    # registered at planned_start_utc.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS appliances (
            id                       INTEGER PRIMARY KEY AUTOINCREMENT,
            vendor                   TEXT NOT NULL,
            vendor_device_id         TEXT NOT NULL,
            name                     TEXT NOT NULL,
            device_type              TEXT NOT NULL,
            default_duration_minutes INTEGER NOT NULL DEFAULT 120,
            deadline_local_time      TEXT NOT NULL DEFAULT '07:00',
            typical_kw               REAL NOT NULL DEFAULT 0.5,
            enabled                  INTEGER NOT NULL DEFAULT 1,
            created_at               TEXT NOT NULL,
            UNIQUE(vendor, vendor_device_id)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS appliance_jobs (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            appliance_id        INTEGER NOT NULL,
            status              TEXT NOT NULL DEFAULT 'scheduled',
            armed_at_utc        TEXT NOT NULL,
            deadline_utc        TEXT NOT NULL,
            duration_minutes    INTEGER NOT NULL,
            planned_start_utc   TEXT NOT NULL,
            planned_end_utc     TEXT NOT NULL,
            avg_price_pence     REAL,
            actual_start_utc    TEXT,
            error_msg           TEXT,
            last_replan_at_utc  TEXT,
            created_at          TEXT NOT NULL,
            updated_at          TEXT NOT NULL,
            FOREIGN KEY (appliance_id) REFERENCES appliances(id)
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_appliance_jobs_status_planned "
        "ON appliance_jobs(status, planned_start_utc)"
    )
    # Partial unique index: one active session per appliance.
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_appliance_jobs_one_active "
        "ON appliance_jobs(appliance_id) "
        "WHERE status IN ('scheduled', 'running')"
    )
    # PR #234: cycle-completion poll. Records when the post-fire poller
    # detected the unit transition out of `run`/`pause` (used to time the
    # finished-laundry notification + downstream metrics).
    cur = conn.execute("PRAGMA table_info(appliance_jobs)")
    aj_cols = {str(r[1]) for r in cur.fetchall()}
    if "completed_at_utc" not in aj_cols:
        conn.execute("ALTER TABLE appliance_jobs ADD COLUMN completed_at_utc TEXT")
    # PR #235: capture actual energy via SmartThings powerConsumptionReport.
    # energy_start_wh = lifetime Wh counter at cycle start; actual_kwh =
    # (counter_at_complete − counter_at_start) / 1000. Both NULL when the
    # device doesn't report the capability or the snapshot was missed.
    if "energy_start_wh" not in aj_cols:
        conn.execute("ALTER TABLE appliance_jobs ADD COLUMN energy_start_wh REAL")
    if "actual_kwh" not in aj_cols:
        conn.execute("ALTER TABLE appliance_jobs ADD COLUMN actual_kwh REAL")

    # Re-arm latch (2026-06-28). Some washer firmwares leave
    # ``remoteControlEnabled`` ON after a cycle finishes, so reconcile would
    # re-arm and re-run the SAME load on the next LP solve. This boolean
    # latches True when a cycle reaches a terminal state and is only cleared
    # once Smart Control is observed OFF — so a fresh manual arm (off→on) is
    # required before the appliance runs again. Persisted (not in-process) so
    # a restart can't bypass it.
    cur = conn.execute("PRAGMA table_info(appliances)")
    ap_cols = {str(r[1]) for r in cur.fetchall()}
    if "rearm_block_until_off" not in ap_cols:
        conn.execute(
            "ALTER TABLE appliances ADD COLUMN "
            "rearm_block_until_off INTEGER NOT NULL DEFAULT 0"
        )

    # V11-A (#194): closed-loop replay needs cloud cover at solve-time.
    # Without this column, lp_replay._reconstruct_weather passes 0.0 to
    # HourlyForecast and skips cloud attenuation, so replay PV is marginally
    # higher than the original LP saw — an attribution-killing fidelity gap
    # for any PV-side regression test (cloud-aware calibration #232 included).
    cur = conn.execute("PRAGMA table_info(meteo_forecast_history)")
    mfh_cols = {str(r[1]) for r in cur.fetchall()}
    if "cloud_cover_pct" not in mfh_cols:
        conn.execute("ALTER TABLE meteo_forecast_history ADD COLUMN cloud_cover_pct REAL")

    # V11-A: nullable forensic / replay-aid fields on lp_inputs_snapshot.
    # ``dhw_draw_prior_json`` populated by V11-C (#196), ``occupancy_prior_json``
    # by V11-D (#197). Both stay NULL until those stories ship — this PR just
    # reserves the columns so V11-C/D don't need their own migration.
    cur = conn.execute("PRAGMA table_info(lp_inputs_snapshot)")
    lp_cols = {str(r[1]) for r in cur.fetchall()}
    if "forecast_fetch_at_utc" not in lp_cols:
        conn.execute("ALTER TABLE lp_inputs_snapshot ADD COLUMN forecast_fetch_at_utc TEXT")
    if "exogenous_snapshot_json" not in lp_cols:
        conn.execute("ALTER TABLE lp_inputs_snapshot ADD COLUMN exogenous_snapshot_json TEXT")
    if "dhw_draw_prior_json" not in lp_cols:
        conn.execute("ALTER TABLE lp_inputs_snapshot ADD COLUMN dhw_draw_prior_json TEXT")
    if "occupancy_prior_json" not in lp_cols:
        conn.execute("ALTER TABLE lp_inputs_snapshot ADD COLUMN occupancy_prior_json TEXT")
    # Infeasible-replay column. NULL on rows written before this migration
    # (interpret as 'Optimal' — only successful solves were snapshotted prior).
    # From 2026-05-19 onwards the Infeasible branch in _run_optimizer_lp also
    # writes a snapshot so the constraints can be replayed offline.
    if "lp_status" not in lp_cols:
        conn.execute("ALTER TABLE lp_inputs_snapshot ADD COLUMN lp_status TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_lp_inputs_snapshot_forecast_fetch ON lp_inputs_snapshot(forecast_fetch_at_utc)"
    )

    # Older DBs predate the dispatch_decisions audit columns. The CREATE TABLE
    # at SCHEMA-load time declares them for fresh installs, but a long-lived
    # prod DB needs explicit ALTER TABLE migrations. ``persist_dispatch_decisions``
    # writes None into these columns until the margin-guard slice ships, so the
    # migration is a pure compatibility fix — no behavior change.
    cur = conn.execute("PRAGMA table_info(dispatch_decisions)")
    dd_cols = {str(r[1]) for r in cur.fetchall()}
    if "export_price_p_kwh" not in dd_cols:
        conn.execute("ALTER TABLE dispatch_decisions ADD COLUMN export_price_p_kwh REAL")
    if "refill_price_p_kwh" not in dd_cols:
        conn.execute("ALTER TABLE dispatch_decisions ADD COLUMN refill_price_p_kwh REAL")
    if "economic_margin_p_kwh" not in dd_cols:
        conn.execute("ALTER TABLE dispatch_decisions ADD COLUMN economic_margin_p_kwh REAL")
    # #274: per-slot percentile of the Outgoing Agile rate within the LP
    # horizon — where in the day's Outgoing distribution did this export sit?
    if "outgoing_rate_percentile" not in dd_cols:
        conn.execute("ALTER TABLE dispatch_decisions ADD COLUMN outgoing_rate_percentile REAL")
    # Older DBs predate the cloud-aware PV table (PR #232). Idempotent — when
    # the table is already created at SCHEMA-load time this block is a no-op.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS pv_calibration_hourly_cloud (
            hour_utc        INTEGER NOT NULL CHECK(hour_utc >= 0 AND hour_utc < 24),
            cloud_bucket    INTEGER NOT NULL CHECK(cloud_bucket >= 0 AND cloud_bucket < 4),
            factor          REAL NOT NULL,
            samples         INTEGER NOT NULL,
            window_days     INTEGER NOT NULL,
            computed_at     TEXT NOT NULL,
            PRIMARY KEY (hour_utc, cloud_bucket)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS forecast_skill_log (
            date_utc TEXT NOT NULL,
            hour_of_day INTEGER NOT NULL CHECK(hour_of_day >= 0 AND hour_of_day < 24),
            predicted_temp_c REAL,
            actual_temp_c REAL,
            predicted_pv_kwh REAL,
            actual_pv_kwh REAL,
            predicted_load_kwh REAL,
            actual_load_kwh REAL,
            built_at_utc TEXT NOT NULL,
            PRIMARY KEY (date_utc, hour_of_day)
        )"""
    )
    cur = conn.execute("PRAGMA table_info(forecast_skill_log)")
    fsl_cols = {str(r[1]) for r in cur.fetchall()}
    if "predicted_load_kwh" not in fsl_cols:
        conn.execute("ALTER TABLE forecast_skill_log ADD COLUMN predicted_load_kwh REAL")
    if "actual_load_kwh" not in fsl_cols:
        conn.execute("ALTER TABLE forecast_skill_log ADD COLUMN actual_load_kwh REAL")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_forecast_skill_log_date ON forecast_skill_log(date_utc)"
    )
    # Per-slot PV forecast error log (issue #462). Persists, for each half-hour
    # slot, the COMMITTED PV-generation forecast (stitched across LP solves — the
    # forecast as known when the slot began) vs the realised actual, so model
    # improvement has a slot-level history and the /pv/today accuracy block has a
    # real baseline for already-elapsed slots. Distinct from forecast_skill_log
    # (which is hourly and sourced from raw meteo snapshots, not the committed LP
    # plan). One row per slot; rebuilt nightly, idempotent on slot_time_utc.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS pv_error_log (
            slot_time_utc   TEXT PRIMARY KEY,
            run_id          INTEGER,
            forecast_kwh    REAL,
            actual_kwh      REAL,
            error_kwh       REAL,
            built_at_utc    TEXT NOT NULL
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pv_error_log_slot ON pv_error_log(slot_time_utc)"
    )

    # load_error_log — per-slot committed LOAD forecast vs realised total load
    # (the load analog of pv_error_log; Phase-1 measurement for load calibration).
    # ``forecast_kwh`` = committed total (base_load + dhw + space); ``forecast_base_kwh``
    # = the LP's exogenous residual-load input alone (what the load profile forecasts
    # and a future recent-bias corrector would target). ``actual_kwh`` = measured
    # total household load (pv_realtime_history.load_power_kw mean × 0.5 h). Per-slot
    # we can only measure TOTAL (no per-slot Daikin meter); the Daikin daily check
    # separates the heat-pump component. One row per slot; rebuilt nightly,
    # idempotent on slot_time_utc.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS load_error_log (
            slot_time_utc    TEXT PRIMARY KEY,
            forecast_kwh     REAL,
            forecast_base_kwh REAL,
            actual_kwh       REAL,
            error_kwh        REAL,
            built_at_utc     TEXT NOT NULL
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_load_error_log_slot ON load_error_log(slot_time_utc)"
    )
    # dhw_error_log — committed LP DHW forecast vs realised Daikin DHW energy,
    # per LOCAL 2-hour bucket (the Daikin consumption API's native resolution;
    # honest granularity beats fabricated 30-min splits). PR C, 2026-07-02 LP
    # audit: DHW was the largest unmonitored forecast stream.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS dhw_error_log (
            day          TEXT NOT NULL,
            bucket_idx   INTEGER NOT NULL CHECK (bucket_idx BETWEEN 0 AND 11),
            forecast_kwh REAL,
            actual_kwh   REAL,
            error_kwh    REAL,
            built_at_utc TEXT NOT NULL,
            applied_factor REAL,
            mode         TEXT,
            PRIMARY KEY (day, bucket_idx)
        )"""
    )
    # dhw_bucket_bias open-loop learning needs the factor that was IN FORCE
    # when each row's forecast was committed (1.0/NULL while disabled) plus the
    # optimization mode, so the learner can de-bias (raw = forecast/factor) and
    # filter to normal-mode rows. NULL on pre-migration rows = factor 1.0,
    # mode unknown→treated as normal (prod has only ever logged normal days).
    del_cols = {str(r[1]) for r in conn.execute("PRAGMA table_info(dhw_error_log)")}
    if del_cols and "applied_factor" not in del_cols:
        conn.execute("ALTER TABLE dhw_error_log ADD COLUMN applied_factor REAL")
    if del_cols and "mode" not in del_cols:
        conn.execute("ALTER TABLE dhw_error_log ADD COLUMN mode TEXT")
    # Daily export opportunity cost: what the day's export earned on the current
    # flat SEG tariff vs what it WOULD have earned on Outgoing Agile. The running
    # tally of money left on the table by not being on Agile export — ammunition
    # to push Octopus to switch. One row per local day, recomputed idempotently.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS export_opportunity_log (
            day                TEXT PRIMARY KEY,
            export_kwh         REAL,
            seg_pence          REAL,
            agile_pence        REAL,
            opportunity_pence  REAL,
            computed_at_utc    TEXT NOT NULL
        )"""
    )
    # Older DBs predate the Daikin LWT calibration table — idempotent CREATE.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS daikin_lwt_kw_calibration (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            k_per_degc REAL NOT NULL,
            samples INTEGER NOT NULL,
            window_days INTEGER NOT NULL,
            rmse_kwh REAL,
            bias_kwh REAL,
            computed_at TEXT NOT NULL
        )"""
    )
    # W2 thermal learner (#540) — building τ / UA / C learned from indoor
    # sensor decay + the measured-indoor HDD regression. Single row, same
    # pattern as daikin_lwt_kw_calibration. τ and UA carry separate quality
    # fields so τ (any cool night) can land months before UA (needs winter).
    conn.execute(
        """CREATE TABLE IF NOT EXISTS building_thermal_calibration (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            tau_hours REAL,
            tau_r2_median REAL,
            tau_episodes INTEGER,
            tau_window_days INTEGER,
            tau_computed_at TEXT,
            ua_w_per_k REAL,
            ua_r2 REAL,
            ua_samples INTEGER,
            ua_window_days INTEGER,
            ua_assumed_cop REAL,
            ua_source TEXT,
            ua_computed_at TEXT,
            c_kwh_per_k REAL,
            c_source TEXT,
            computed_at TEXT NOT NULL
        )"""
    )
    # DHW LP-owned economic shadow (#714). One row per shadow solve: the committed
    # (pinned) grid cost vs the LP-owned grid cost on the SAME inputs, plus the comfort
    # deficit (°C below floor at any shower boundary). The enable gate reads this — the
    # LP-owned regime is only ever suggested after a run of days where it is BOTH cheaper
    # AND leaves comfort untouched.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS dhw_lp_shadow_log (
            run_at_utc TEXT PRIMARY KEY,
            day TEXT NOT NULL,
            cost_pinned_p REAL NOT NULL,
            cost_lp_owned_p REAL NOT NULL,
            delta_p REAL NOT NULL,
            comfort_deficit_c REAL NOT NULL,
            horizon_days REAL,
            terminal_credit_p REAL,
            e_dhw_fixed_kwh REAL,
            e_dhw_lp_kwh REAL,
            n_tank_rows INTEGER
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dhw_shadow_day ON dhw_lp_shadow_log(day)"
    )
    # DHW tank calibration (#714 rewrite). ONE row per component so the UA fit and
    # the draw-event observability land independently. The rewrite's cardinal rule
    # — no energy counter, thermometer only — means there is no COP row here: the
    # COP is the certified databook curve, never a fit (#719).
    conn.execute(
        """CREATE TABLE IF NOT EXISTS dhw_calibration (
            component TEXT PRIMARY KEY,
            fitted_at_utc TEXT NOT NULL,
            status TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            n_samples INTEGER,
            r2 REAL,
            window_days INTEGER
        )"""
    )
    # W2 observability (#540): the learner's LAST run summary — episodes/HDD-days
    # collected + skip reasons — so the UI can show "learning in progress, N/5
    # decay nights" even while the calibration itself is still on env defaults.
    conn.execute(
        """CREATE TABLE IF NOT EXISTS thermal_learning_progress (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            updated_at TEXT NOT NULL,
            result_json TEXT NOT NULL
        )"""
    )
    # Read-only view that exposes daikin_telemetry.fetched_at as ISO 8601
    # alongside the raw float-epoch column. Productive code paths (estimator,
    # service.py rate calcs) consume the float directly for sub-second math —
    # changing the underlying column would force every consumer to parse
    # strings, ~100× slower with no behaviour gain. The view is for human
    # exploration / ad-hoc audit queries that expect ISO-shaped timestamps,
    # so they don't silently get 0 rows from `WHERE fetched_at LIKE '%'`.
    conn.execute("DROP VIEW IF EXISTS daikin_telemetry_iso")
    conn.execute(
        """CREATE VIEW daikin_telemetry_iso AS
           SELECT
               strftime('%Y-%m-%dT%H:%M:%fZ', fetched_at, 'unixepoch') AS fetched_at_iso,
               fetched_at AS fetched_at_epoch,
               source, tank_temp_c, indoor_temp_c, outdoor_temp_c,
               tank_target_c, lwt_actual_c, mode, weather_regulation
           FROM daikin_telemetry"""
    )


def init_db() -> None:
    """Create tables if missing and apply lightweight migrations."""
    with _lock:
        conn = get_connection()
        try:
            conn.executescript(SCHEMA)
            _migrate_schema(conn)
            conn.commit()
        finally:
            conn.close()


def _normalize_utc_iso(ts: str) -> str:
    """Normalize any ISO timestamp to canonical UTC Z format for consistent DB storage and string-sort.

    Parses the timestamp, converts to UTC, and returns 'YYYY-MM-DDTHH:MM:SSZ'.
    Raises ValueError if the string cannot be parsed.
    """
    s = ts.replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        logger.warning("TZ-AUDIT: naive timestamp received (assumed UTC): %s", ts)
        dt = dt.replace(tzinfo=UTC)
    elif dt.utcoffset().total_seconds() != 0:
        logger.warning("TZ-AUDIT: non-UTC offset in Octopus timestamp (converting): %s", ts)
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def save_agile_rates(rates: list[dict[str, Any]], tariff_code: str) -> int:
    """Upsert Agile rate rows. Each rate dict: value_inc_vat, valid_from, valid_to (ISO).

    Timestamps are normalized to UTC Z format before storage so that SQLite string-sort
    and period comparisons are always timezone-consistent.
    """
    if not rates or not tariff_code:
        return 0
    now = datetime.now(UTC).isoformat()
    tz_local = ZoneInfo(config.BULLETPROOF_TIMEZONE)
    n = 0
    first_ts: str | None = None
    last_ts: str | None = None
    with _lock:
        conn = get_connection()
        try:
            for r in rates:
                vf = r.get("valid_from")
                vt = r.get("valid_to")
                v = r.get("value_inc_vat")
                if vf is None or vt is None or v is None:
                    continue
                try:
                    vf_norm = _normalize_utc_iso(str(vf))
                    vt_norm = _normalize_utc_iso(str(vt))
                except ValueError:
                    logger.error("TZ-AUDIT: unparseable timestamp skipped: vf=%s vt=%s", vf, vt)
                    continue
                conn.execute(
                    """INSERT INTO agile_rates (valid_from, valid_to, value_inc_vat, tariff_code, fetched_at)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(valid_from, tariff_code) DO UPDATE SET
                         valid_to=excluded.valid_to,
                         value_inc_vat=excluded.value_inc_vat,
                         fetched_at=excluded.fetched_at""",
                    (vf_norm, vt_norm, float(v), tariff_code, now),
                )
                if first_ts is None:
                    first_ts = vf_norm
                last_ts = vt_norm
                n += 1
            conn.commit()
        finally:
            conn.close()
    if n and first_ts and last_ts:
        dt_first = datetime.fromisoformat(first_ts.replace("Z", "+00:00"))
        dt_last = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
        logger.info(
            "TZ-AUDIT: saved %d rates for %s | UTC %s → %s | local %s → %s",
            n,
            tariff_code,
            first_ts,
            last_ts,
            dt_first.astimezone(tz_local).strftime("%a %d %b %H:%M %Z"),
            dt_last.astimezone(tz_local).strftime("%a %d %b %H:%M %Z"),
        )
    return n


def save_agile_export_rates(rates: list[dict[str, Any]], tariff_code: str) -> int:
    """Upsert Agile **export** (Outgoing) rate rows into ``agile_export_rates``.

    Mirror of :func:`save_agile_rates`: each rate dict has ``value_inc_vat``,
    ``valid_from``, ``valid_to`` (ISO). Timestamps normalised to UTC Z.
    """
    if not rates or not tariff_code:
        return 0
    now = datetime.now(UTC).isoformat()
    n = 0
    with _lock:
        conn = get_connection()
        try:
            for r in rates:
                vf = r.get("valid_from")
                vt = r.get("valid_to")
                v = r.get("value_inc_vat")
                if vf is None or vt is None or v is None:
                    continue
                try:
                    vf_norm = _normalize_utc_iso(str(vf))
                    vt_norm = _normalize_utc_iso(str(vt))
                except ValueError:
                    logger.error("TZ-AUDIT: unparseable export timestamp skipped: vf=%s vt=%s", vf, vt)
                    continue
                conn.execute(
                    """INSERT INTO agile_export_rates (valid_from, valid_to, value_inc_vat, tariff_code, fetched_at)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(valid_from, tariff_code) DO UPDATE SET
                         valid_to=excluded.valid_to,
                         value_inc_vat=excluded.value_inc_vat,
                         fetched_at=excluded.fetched_at""",
                    (vf_norm, vt_norm, float(v), tariff_code, now),
                )
                n += 1
            conn.commit()
        finally:
            conn.close()
    if n:
        logger.info("Octopus export rates saved: %d rows for %s", n, tariff_code)
    return n


def get_agile_export_rates_in_range(
    period_from_iso: str,
    period_to_iso: str,
) -> list[dict[str, Any]]:
    """Return export rate rows whose ``valid_from`` falls in ``[period_from, period_to)``.

    Returns ``[]`` when the table is empty (no Outgoing tariff configured / not yet
    fetched). Caller is responsible for the fallback to a flat constant.
    """
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                """SELECT valid_from, valid_to, value_inc_vat, tariff_code
                   FROM agile_export_rates
                   WHERE valid_from >= ? AND valid_from < ?
                   ORDER BY valid_from""",
                (period_from_iso, period_to_iso),
            )
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()


def get_agile_rate_at(ts_utc: datetime) -> float | None:
    """Import price (p/kWh, inc VAT) of the Agile slot covering *ts_utc*, or None.

    Used by the in-flight bridge's no-authority fallback (#693) to gate a
    blind ForceCharge/Backup bridge on the current slot being negative-priced.
    """
    from .config import config as _config

    tariff = (_config.OCTOPUS_TARIFF_CODE or "").strip()
    if not tariff:
        return None
    ts_z = ts_utc.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                """SELECT value_inc_vat FROM agile_rates
                   WHERE tariff_code = ? AND valid_from <= ? AND valid_to > ?
                   ORDER BY valid_from DESC LIMIT 1""",
                (tariff, ts_z, ts_z),
            )
            r = cur.fetchone()
            return float(r["value_inc_vat"]) if r else None
        finally:
            conn.close()


def get_agile_rates_coverage_max(
    table: str = "agile_rates",
    tariff_code: str | None = None,
) -> str | None:
    """MAX(valid_from) in ``agile_rates`` or ``agile_export_rates`` (ISO Z string).

    Both tables normalise ``valid_from`` to UTC Z on save, so the strings are
    lexicographically comparable — the export-gap check (#691) relies on that.
    Pass ``tariff_code`` to scope coverage to the currently configured tariff:
    both tables are upsert-only, so rows from a previously configured code
    would otherwise mask a real gap after a tariff switch.
    Returns ``None`` when no matching rows exist.
    """
    if table not in ("agile_rates", "agile_export_rates"):
        raise ValueError(f"unsupported rates table: {table}")
    with _lock:
        conn = get_connection()
        try:
            if tariff_code:
                cur = conn.execute(
                    f"SELECT MAX(valid_from) AS mx FROM {table} WHERE tariff_code = ?",  # noqa: S608 — table validated above
                    (tariff_code,),
                )
            else:
                cur = conn.execute(f"SELECT MAX(valid_from) AS mx FROM {table}")  # noqa: S608 — table validated above
            r = cur.fetchone()
            return r["mx"] if r else None
        finally:
            conn.close()


def get_agile_rates_slots_for_local_day(
    tariff_code: str,
    local_date,  # datetime.date
    tz_name: str = "Europe/London",
) -> list[dict[str, Any]]:
    """All Agile slots that overlap the given local-day (handles DST).

    Returns 46 (spring-forward), 48 (normal), or 50 (fall-back) rows ordered
    by ``valid_from`` ASC. Each carries ``valid_from``, ``valid_to``,
    ``value_inc_vat``. Caller can colour-code by percentile etc.
    """
    from datetime import datetime as _dt
    from datetime import time as _time
    from datetime import timedelta as _td
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(tz_name)
    local_start = _dt.combine(local_date, _time(0, 0), tzinfo=tz)
    local_end = local_start + _td(days=1)
    utc_start = local_start.astimezone(UTC)
    utc_end = local_end.astimezone(UTC)
    return get_rates_for_period(tariff_code, utc_start, utc_end)


def get_rates_for_period(
    tariff_code: str,
    period_from_utc: datetime,
    period_to_utc: datetime,
) -> list[dict[str, Any]]:
    """Return rate rows ordered by valid_from."""
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                """SELECT valid_from, valid_to, value_inc_vat, tariff_code, fetched_at
                   FROM agile_rates
                   WHERE tariff_code = ? AND valid_from < ? AND valid_to > ?
                   ORDER BY valid_from""",
                (
                    tariff_code,
                    period_to_utc.isoformat().replace("+00:00", "Z"),
                    period_from_utc.isoformat().replace("+00:00", "Z"),
                ),
            )
            rows = [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()
    return rows


def upsert_action(
    *,
    plan_date: str,
    start_time: str,
    end_time: str,
    device: str,
    action_type: str,
    params: dict[str, Any] | None = None,
    status: str = "pending",
    restore_action_id: int | None = None,
) -> int:
    created = datetime.now(UTC).isoformat()
    params_json = json.dumps(params or {})
    with _lock:
        conn = get_connection()
        try:
            # Genuine upsert on the natural key (device, action_type, start_time).
            # Without this the function was a plain INSERT, so every re-plan that
            # re-emitted the same slot created a duplicate row — and because
            # clear_actions_in_range only removes *pending* rows in range, an
            # already-fired (completed) or in-flight row for the same slot was
            # never cleared, so the re-emit produced a past-dated pending dup
            # that fired again. Observed in prod: ~18 identical tank_warmup rows
            # in one day. Rule:
            #   * existing pending+future row  → refresh it (pick up new params)
            #   * existing completed/failed/in-flight row → SKIP (already handled;
            #     never recreate a past or actively-firing action)
            existing = conn.execute(
                """SELECT id, status, start_time, end_time FROM action_schedule
                   WHERE device = ? AND action_type = ? AND start_time = ?
                   ORDER BY id DESC""",
                (device, action_type, start_time),
            ).fetchall()
            if existing:
                now_iso = created
                refreshable = None
                for row in existing:
                    in_flight = (
                        row["status"] == "pending"
                        and row["start_time"] <= now_iso < (row["end_time"] or "")
                    )
                    if row["status"] == "pending" and not in_flight:
                        refreshable = row
                        break
                if refreshable is not None:
                    conn.execute(
                        """UPDATE action_schedule
                           SET date = ?, end_time = ?, params = ?, status = ?,
                               restore_action_id = ?, created_at = ?
                           WHERE id = ?""",
                        (
                            plan_date, end_time, params_json, status,
                            restore_action_id, created, refreshable["id"],
                        ),
                    )
                    conn.commit()
                    return int(refreshable["id"])
                # Only completed/failed/in-flight rows exist for this slot —
                # the action is already handled; do not create a duplicate.
                conn.commit()
                return int(existing[0]["id"])
            cur = conn.execute(
                """INSERT INTO action_schedule
                   (date, start_time, end_time, device, action_type, params, status,
                    restore_action_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    plan_date,
                    start_time,
                    end_time,
                    device,
                    action_type,
                    params_json,
                    status,
                    restore_action_id,
                    created,
                ),
            )
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()


def update_action_restore_link(action_id: int, restore_id: int) -> None:
    with _lock:
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE action_schedule SET restore_action_id = ? WHERE id = ?",
                (restore_id, action_id),
            )
            conn.commit()
        finally:
            conn.close()


def _row_action(r: sqlite3.Row) -> dict[str, Any]:
    d = dict(r)
    if d.get("params"):
        try:
            d["params"] = json.loads(d["params"])
        except json.JSONDecodeError:
            d["params"] = {}
    return d


def mark_action(
    action_id: int,
    status: str,
    error_msg: str | None = None,
    executed_at: str | None = None,
) -> None:
    ex = executed_at or datetime.now(UTC).isoformat()
    with _lock:
        conn = get_connection()
        try:
            conn.execute(
                """UPDATE action_schedule SET status = ?, error_msg = ?, executed_at = ?
                   WHERE id = ?""",
                (status, error_msg, ex, action_id),
            )
            conn.commit()
        finally:
            conn.close()


def mark_action_user_overridden(
    action_id: int,
    overridden_at: str | None = None,
) -> None:
    """Phase 4.3: flag an action_schedule row as overridden by the user so the reconciler skips it."""
    ts = overridden_at or datetime.now(UTC).isoformat()
    with _lock:
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE action_schedule SET overridden_by_user_at = ? WHERE id = ?",
                (ts, action_id),
            )
            conn.commit()
        finally:
            conn.close()


def find_recent_user_override(
    device: str,
    *,
    within_hours: float,
    now_utc: datetime | None = None,
    respect_until_window_end: bool = False,
) -> dict[str, Any] | None:
    """Epic 14 (#386): the most-recent ``user_overridden`` action_schedule row
    for ``device`` whose override is still in effect. Returns ``None`` when no
    such row exists.

    An override qualifies when EITHER:
      * its ``overridden_by_user_at`` timestamp is inside the trailing
        ``within_hours`` window (the original fixed grace), OR
      * ``respect_until_window_end`` is set AND the overridden row's own
        planned window still covers now (``end_time > now``). This honours
        "if I change the tank/LWT by hand, don't touch it until the end of the
        planned window" for windows longer than ``within_hours`` (e.g. a
        multi-hour negative-price boost) — the 2026-06-07 requirement. The
        caller's live ``user_gesture_still_in_effect`` check remains the real
        safety gate: revert the manual change and HEM resumes control at once,
        so a far-future ``end_time`` can never wedge the schedule.

    Used by the heartbeat reconciler to propagate a user's manual gesture
    (e.g. tank power-off via Onecta) onto fresh rows the LP inserts after
    a replan, so the user doesn't have to keep undoing the same action.
    """
    now = now_utc or datetime.now(UTC)
    clauses: list[str] = ["device = ?", "overridden_by_user_at IS NOT NULL"]
    params: list[Any] = [device]
    time_terms: list[str] = []
    if within_hours > 0:
        # overridden_by_user_at is stored in +00:00 form (datetime.isoformat).
        time_terms.append("overridden_by_user_at >= ?")
        params.append((now - timedelta(hours=within_hours)).isoformat())
    if respect_until_window_end:
        # end_time is stored in Z form (dhw_policy._iso_z / lp_dispatch). Compare
        # against a Z-form "now" so the lexicographic boundary is exact — a
        # +00:00 "now" sorts after the same instant in Z form ('Z' > '+').
        time_terms.append("end_time > ?")
        params.append(now.isoformat().replace("+00:00", "Z"))
    if not time_terms:
        return None
    clauses.append("(" + " OR ".join(time_terms) + ")")
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                "SELECT * FROM action_schedule "
                "WHERE " + " AND ".join(clauses) + " "
                "ORDER BY overridden_by_user_at DESC LIMIT 1",
                tuple(params),
            )
            r = cur.fetchone()
            return _row_action(r) if r else None
        finally:
            conn.close()


def get_action_by_id(action_id: int) -> dict[str, Any] | None:
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute("SELECT * FROM action_schedule WHERE id = ?", (action_id,))
            r = cur.fetchone()
            return _row_action(r) if r else None
        finally:
            conn.close()


def get_actions_for_plan_date(plan_date: str, device: str | None = None) -> list[dict[str, Any]]:
    with _lock:
        conn = get_connection()
        try:
            if device:
                cur = conn.execute(
                    """SELECT * FROM action_schedule WHERE date = ? AND device = ?
                       ORDER BY start_time, id""",
                    (plan_date, device),
                )
            else:
                cur = conn.execute(
                    """SELECT * FROM action_schedule WHERE date = ?
                       ORDER BY device, start_time, id""",
                    (plan_date,),
                )
            return [_row_action(r) for r in cur.fetchall()]
        finally:
            conn.close()


def mean_consumption_kwh_from_execution_logs(limit: int = 2000) -> float:
    """Rolling mean half-hourly kWh from execution_log (fallback 0.4)."""
    rows = get_execution_logs(limit=limit)
    kwhs = [float(r["consumption_kwh"] or 0) for r in rows if r.get("consumption_kwh") is not None]
    if not kwhs:
        return 0.4
    return sum(kwhs) / len(kwhs)


def get_half_hourly_agile_priors(
    tariff_code: str,
    *,
    window_days: int = 28,
    table: str = "agile_rates",
) -> dict[tuple[int, int], float]:
    """Median import (or export) price per UTC ``(hour, minute)`` half-hour bucket
    from the last *window_days* of Agile rates.

    Used by the LP horizon extender (S10.2 / #169) to fill D+1 slots when Octopus
    hasn't published tomorrow's prices yet (typically before ~16:00 BST). Buckets
    by (hour, minute) — 48 buckets — because Agile pricing varies meaningfully
    between :00 and :30 within the same hour (S10.8 / #175). Median per bucket
    is robust to outlier days (e.g. negative-price plunges) so a single weird
    Saturday doesn't skew the prior.

    Returns a dict mapping ``(hour_utc, minute) → pence/kWh`` where minute ∈ {0, 30}.
    Buckets absent from the window are absent from the dict; caller should fall back
    (e.g. mean of all buckets).
    """
    cutoff_iso = (datetime.now(UTC) - timedelta(days=window_days)).isoformat().replace("+00:00", "Z")
    by_bucket: dict[tuple[int, int], list[float]] = {}
    with _lock:
        conn = get_connection()
        try:
            # substr(valid_from, 12, 5) extracts "HH:MM" from "2026-04-27T14:30:00Z"
            cur = conn.execute(
                f"SELECT substr(valid_from, 12, 5) AS hhmm, value_inc_vat AS v "
                f"FROM {table} "
                f"WHERE tariff_code = ? AND valid_from > ?",
                (tariff_code, cutoff_iso),
            )
            for r in cur.fetchall():
                try:
                    hh, mm = r["hhmm"].split(":")
                    bucket = (int(hh), int(mm))
                    v = float(r["v"])
                except (TypeError, ValueError, KeyError, AttributeError):
                    continue
                by_bucket.setdefault(bucket, []).append(v)
        finally:
            conn.close()
    out: dict[tuple[int, int], float] = {}
    for bucket, vs in by_bucket.items():
        if vs:
            vs.sort()
            out[bucket] = vs[len(vs) // 2]
    return out


def half_hourly_residual_load_profile_kwh(
    *,
    window_days: int = 30,
) -> dict[tuple[int, int], float]:
    """Per-(hour, minute) MEDIAN *residual* (non-Daikin) load from real Fox
    telemetry. Subtracts physics-estimated Daikin draw per sample so the LP
    energy balance no longer double-counts Daikin.

    S10.13 (#179): the LP balance ``imp + pv + dis == base_load + exp + chg
    + (e_dhw + e_space)`` adds physics-predicted Daikin (``e_dhw + e_space``)
    on top of ``base_load`` from execution_log. After S10.9 switched
    base_load source to real Fox load_power_kw — which IS house total
    including Daikin's actual past draw — Daikin gets counted twice. This
    function applies the *same* physics estimator the LP uses for prediction
    (``predict_passive_daikin_load``) per past sample to back out the
    residual (non-Daikin) load.

    Outdoor temperature per sample comes from the canonical forecast snapshot
    store. When no historical match exists, fall through to the latest marked
    forecast snapshot for the same hour-of-year;
    if both miss, the sample is dropped rather than emitting a polluted
    residual. The previous behaviour used a 25 °C sentinel that pushed
    physics above the climate-curve cutoff and silently included un-subtracted
    Daikin in the residual — a phantom *inclusion*, not the "conservative
    no-subtraction" the comment claimed. Early-morning hours (sparse history
    coverage, biggest Daikin draw) suffered most.

    Empty `(hour, minute)` buckets fall back hour-of-day-aware: same hour's
    other half-bucket → median of `±2 h` neighbours → global median. Avoids
    the previous global-median fill that inherited mid-day load values for
    empty early-morning buckets.
    """
    from .config import config, cop_at_temperature
    from .physics import predict_passive_daikin_load

    cutoff_iso = (datetime.now(UTC) - timedelta(days=window_days)).isoformat().replace("+00:00", "Z")
    cop_curve = config.DAIKIN_COP_CURVE

    buckets: dict[tuple[int, int], list[float]] = {}
    samples_used = 0
    samples_dropped_no_meteo = 0
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                """SELECT pv.captured_at, pv.load_power_kw,
                          (SELECT mv.temp_c
                           FROM meteo_forecast_value mv
                           JOIN meteo_forecast_snapshot ms
                             ON ms.forecast_fetch_at_utc = mv.forecast_fetch_at_utc
                           WHERE substr(mv.slot_time, 1, 13) = substr(pv.captured_at, 1, 13)
                             AND ms.forecast_fetch_at_utc < pv.captured_at
                           ORDER BY ms.forecast_fetch_at_utc DESC LIMIT 1) AS outdoor_history_c,
                          (SELECT mv.temp_c
                           FROM meteo_forecast_value mv
                           JOIN meteo_forecast_latest_state ls
                             ON ls.id = 1
                            AND ls.forecast_fetch_at_utc = mv.forecast_fetch_at_utc
                           WHERE substr(mv.slot_time, 1, 13) = substr(pv.captured_at, 1, 13)
                           LIMIT 1) AS outdoor_latest_c
                   FROM pv_realtime_history pv
                   WHERE pv.captured_at > ? AND pv.load_power_kw IS NOT NULL""",
                (cutoff_iso,),
            )
            rows = cur.fetchall()
        finally:
            conn.close()

    from datetime import datetime as _dt
    for row in rows:
        ts_str = row["captured_at"]
        load_kw = float(row["load_power_kw"])
        outdoor = row["outdoor_history_c"]
        if outdoor is None:
            outdoor = row["outdoor_latest_c"]
        if outdoor is None:
            samples_dropped_no_meteo += 1
            continue
        outdoor_c = float(outdoor)
        cop_at_t = cop_at_temperature(cop_curve, outdoor_c)
        e_space, e_dhw = predict_passive_daikin_load(
            [outdoor_c], [cop_at_t], [cop_at_t], slot_h=0.5
        )
        daikin_slot_kwh = e_space[0] + e_dhw[0]
        sample_slot_kwh = load_kw * 0.5
        residual = max(0.0, sample_slot_kwh - daikin_slot_kwh)
        try:
            ts = _dt.fromisoformat(str(ts_str).replace("Z", "+00:00"))
            local = ts.astimezone(ZoneInfo(config.BULLETPROOF_TIMEZONE))
        except (ValueError, TypeError):
            continue
        minute_bucket = 30 if local.minute >= 30 else 0
        buckets.setdefault((local.hour, minute_bucket), []).append(residual)
        samples_used += 1

    all_vals = [v for vs in buckets.values() for v in vs]
    if all_vals:
        all_vals.sort()
        global_fallback = all_vals[len(all_vals) // 2]
    else:
        global_fallback = mean_consumption_kwh_from_execution_logs(limit=2016)

    bucket_medians: dict[tuple[int, int], float] = {}
    for (h, m), vs in buckets.items():
        if vs:
            vs_sorted = sorted(vs)
            bucket_medians[(h, m)] = vs_sorted[len(vs_sorted) // 2]

    def _hour_aware_fallback(h: int, m: int) -> float:
        # Tier 1 — same hour, other half-bucket
        other = bucket_medians.get((h, 30 if m == 0 else 0))
        if other is not None:
            return other
        # Tier 2 — median of ±2 h band (any minute), excluding self
        neighbour_vals: list[float] = []
        for dh in (-2, -1, 1, 2):
            nh = (h + dh) % 24
            for nm in (0, 30):
                v = bucket_medians.get((nh, nm))
                if v is not None:
                    neighbour_vals.append(v)
        if neighbour_vals:
            neighbour_vals.sort()
            return neighbour_vals[len(neighbour_vals) // 2]
        # Tier 3 — global fallback (sparse-data degradation path)
        return global_fallback

    profile: dict[tuple[int, int], float] = {}
    empty_buckets = 0
    for h in range(24):
        for m in (0, 30):
            v = bucket_medians.get((h, m))
            if v is None:
                profile[(h, m)] = _hour_aware_fallback(h, m)
                empty_buckets += 1
            else:
                profile[(h, m)] = v

    logger.info(
        "residual_profile: %d samples used, %d dropped (no meteo match), "
        "%d/48 buckets empty (hour-aware fallback applied)",
        samples_used,
        samples_dropped_no_meteo,
        empty_buckets,
    )
    return profile


def tariff_aware_residual_load_profile_kwh(
    *,
    window_days: int = 30,
    min_samples_per_kind: int = 5,
) -> dict[tuple, float]:
    """Per-(hour, minute, slot_kind) MEDIAN residual load — captures the
    household's tariff-driven behavioural shift (e.g. cooking pulled into
    cheap windows; heating deferred during peak).

    Phase B2 (#306 follow-up): the legacy
    :func:`half_hourly_residual_load_profile_kwh` returned one median per
    half-hour bucket regardless of price tier. After ~14 days on Agile, the
    family changed routines (lunch ~12:00 in cheap quartile, evening peak
    avoidance) but the LP forecast was treating "13:00 cheap day" the same
    as "13:00 peak day". This function adds the slot_kind dimension by
    joining with ``execution_log.slot_kind`` retrospectively.

    Returns a dict whose keys may be 2-tuples ``(hour, minute)`` or 3-tuples
    ``(hour, minute, kind)``. Caller looks up the most specific bucket
    available, falling back to the plain bucket. ``kind`` ∈ {``negative``,
    ``cheap``, ``standard``, ``peak``}.

    Buckets with fewer than ``min_samples_per_kind`` samples are dropped
    (graceful degradation): the caller's fallback to ``(hour, minute)``
    median still applies.
    """
    from .config import config, cop_at_temperature
    from .physics import predict_passive_daikin_load

    cutoff_iso = (datetime.now(UTC) - timedelta(days=window_days)).isoformat().replace("+00:00", "Z")
    cop_curve = config.DAIKIN_COP_CURVE

    # Step 1: build slot_kind lookup by half-hour bucket from execution_log.
    # Key shape: (YYYY-MM-DD, hour, 0|30) → kind.
    kind_by_slot: dict[tuple[str, int, int], str] = {}
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                """SELECT timestamp, slot_kind FROM execution_log
                   WHERE timestamp > ? AND slot_kind IS NOT NULL""",
                (cutoff_iso,),
            )
            for row in cur.fetchall():
                ts_str = str(row["timestamp"])
                k = str(row["slot_kind"]).strip().lower()
                if not k:
                    continue
                try:
                    from datetime import datetime as _dt
                    ts = _dt.fromisoformat(ts_str.replace("Z", "+00:00"))
                    local = ts.astimezone(ZoneInfo(config.BULLETPROOF_TIMEZONE))
                except (ValueError, TypeError):
                    continue
                slot_key = (
                    local.date().isoformat(),
                    local.hour,
                    30 if local.minute >= 30 else 0,
                )
                # First-seen wins (heartbeat may write multiple rows per slot
                # before #308 dedup; slot_kind is consistent across them).
                kind_by_slot.setdefault(slot_key, k)
        finally:
            conn.close()

    # Step 2: walk pv_realtime samples, compute residual, key by both
    # (hour, minute) and (hour, minute, kind) when known.
    samples_used = 0
    samples_dropped_no_meteo = 0
    by_hm: dict[tuple[int, int], list[float]] = {}
    by_hmk: dict[tuple[int, int, str], list[float]] = {}
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                """SELECT pv.captured_at, pv.load_power_kw,
                          (SELECT mv.temp_c
                           FROM meteo_forecast_value mv
                           JOIN meteo_forecast_snapshot ms
                             ON ms.forecast_fetch_at_utc = mv.forecast_fetch_at_utc
                           WHERE substr(mv.slot_time, 1, 13) = substr(pv.captured_at, 1, 13)
                             AND ms.forecast_fetch_at_utc < pv.captured_at
                           ORDER BY ms.forecast_fetch_at_utc DESC LIMIT 1) AS outdoor_history_c,
                          (SELECT mv.temp_c
                           FROM meteo_forecast_value mv
                           JOIN meteo_forecast_latest_state ls
                             ON ls.id = 1
                            AND ls.forecast_fetch_at_utc = mv.forecast_fetch_at_utc
                           WHERE substr(mv.slot_time, 1, 13) = substr(pv.captured_at, 1, 13)
                           LIMIT 1) AS outdoor_latest_c
                   FROM pv_realtime_history pv
                   WHERE pv.captured_at > ? AND pv.load_power_kw IS NOT NULL""",
                (cutoff_iso,),
            )
            rows = cur.fetchall()
        finally:
            conn.close()

    from datetime import datetime as _dt
    for row in rows:
        ts_str = row["captured_at"]
        load_kw = float(row["load_power_kw"])
        outdoor = row["outdoor_history_c"]
        if outdoor is None:
            outdoor = row["outdoor_latest_c"]
        if outdoor is None:
            samples_dropped_no_meteo += 1
            continue
        outdoor_c = float(outdoor)
        cop_at_t = cop_at_temperature(cop_curve, outdoor_c)
        e_space, e_dhw = predict_passive_daikin_load(
            [outdoor_c], [cop_at_t], [cop_at_t], slot_h=0.5
        )
        daikin_slot_kwh = e_space[0] + e_dhw[0]
        sample_slot_kwh = load_kw * 0.5
        residual = max(0.0, sample_slot_kwh - daikin_slot_kwh)
        try:
            ts = _dt.fromisoformat(str(ts_str).replace("Z", "+00:00"))
            local = ts.astimezone(ZoneInfo(config.BULLETPROOF_TIMEZONE))
        except (ValueError, TypeError):
            continue
        minute_bucket = 30 if local.minute >= 30 else 0
        hm = (local.hour, minute_bucket)
        by_hm.setdefault(hm, []).append(residual)
        slot_key = (local.date().isoformat(), local.hour, minute_bucket)
        kind = kind_by_slot.get(slot_key)
        if kind:
            by_hmk.setdefault((local.hour, minute_bucket, kind), []).append(residual)
        samples_used += 1

    # Step 3: build profile.
    profile: dict[tuple, float] = {}
    for hm, vs in by_hm.items():
        if vs:
            vs_sorted = sorted(vs)
            profile[hm] = vs_sorted[len(vs_sorted) // 2]
    for hmk, vs in by_hmk.items():
        if len(vs) >= min_samples_per_kind:
            vs_sorted = sorted(vs)
            profile[hmk] = vs_sorted[len(vs_sorted) // 2]

    n_kind_buckets = sum(1 for k in profile if len(k) == 3)
    logger.info(
        "tariff_aware_residual_profile: %d samples (%d dropped no-meteo); "
        "%d (h,m) buckets, %d (h,m,kind) buckets ≥%d samples each",
        samples_used, samples_dropped_no_meteo,
        sum(1 for k in profile if len(k) == 2),
        n_kind_buckets,
        min_samples_per_kind,
    )
    return profile


def _physics_daikin_slot_kwh(outdoor_c: float, cop_curve) -> float:
    """Pure-physics Daikin estimate (space + DHW) for one 0.5 h slot — the same
    estimator the LP uses for prediction. Shared by the residual builders."""
    from .config import cop_at_temperature
    from .physics import predict_passive_daikin_load
    cop_at_t = cop_at_temperature(cop_curve, outdoor_c)
    e_space, e_dhw = predict_passive_daikin_load(
        [outdoor_c], [cop_at_t], [cop_at_t], slot_h=0.5
    )
    return float(e_space[0] + e_dhw[0])


_RESIDUAL_V2_CACHE: dict[Any, tuple[float, dict[str, Any]]] = {}
_RESIDUAL_V2_TTL_S = 240.0  # 4 min — covers the multiple calls within one solve


def clear_residual_profile_cache() -> None:
    """Drop the residual_load_profile_v2 TTL cache (tests / forced refresh)."""
    _RESIDUAL_V2_CACHE.clear()


def residual_load_profile_v2(
    *,
    window_days: int | None = None,
    end_date: str | None = None,
    min_samples_per_bucket: int = 4,
    use_cache: bool = True,
) -> dict[str, Any]:
    """Day-of-week-aware residual (non-Daikin) household load profile with
    robust stats and a measured-split-calibrated Daikin subtraction (#477).

    Improves on :func:`half_hourly_residual_load_profile_kwh` in three ways:

    1. **Day-of-week buckets.** Each retained sample is bucketed into three
       tiers — ``(dow, h, m)``, ``(group, h, m)`` (group ∈ {"weekday",
       "weekend"}), and ``(h, m)``. The lookup (:func:`lookup_residual_kwh`)
       prefers the most specific tier with data, so weekday vs weekend routines
       are captured where there's enough history and gracefully fall back where
       there isn't.
    2. **Measured-split calibration.** The per-sample physics Daikin estimate is
       scaled so its per-day (or per-2h-bucket) sum matches the MEASURED
       Onecta/telemetry heating+DHW split (``daikin_consumption_2hourly`` →
       ``daikin_consumption_daily`` → pure physics fallback). Removes the
       systematic physics bias from the residual.
    3. **Robust stats + away-day exclusion.** Median per bucket (the central
       estimate the LP uses) plus the p75 spread (for the scenario LP). Days
       whose 09:00–21:00 residual signature is anomalously low ("away") are
       detected and excluded so the profile reflects the typical AT-HOME day.

    Returns one object consumed by every caller + the inspection endpoint::

        {"profile": {(dow,h,m)|(group,h,m)|(h,m): median_kwh},
         "spread":  {same keys: p75_kwh},
         "flat": float, "away_days": [iso...],
         "day_counts": {"weekday":N, "weekend":M, "away_excluded":K, "total":T},
         "calibrated_days": int, "physics_only_days": int}
    """
    from statistics import median as _median, quantiles as _quantiles

    from .config import config

    if window_days is None:
        window_days = int(getattr(config, "LP_LOAD_PROFILE_WINDOW_DAYS", 120) or 120)
    # Kill-switch: when off, behave ≈ legacy (only the (h,m) median tier, pure
    # physics, no away exclusion) so the LP plan can be rolled back live (#477).
    _v2 = bool(getattr(config, "LP_RESIDUAL_PROFILE_V2", True))
    away_fraction = float(getattr(config, "LP_AWAY_DAY_FRACTION", 0.4) or 0.0) if _v2 else 0.0
    # During negative-price windows the system DELIBERATELY boosts consumption
    # (battery charge, appliance dispatch, tank to 65 °C). Those slots aren't the
    # household's organic at-home pattern — including them biases the load profile
    # (and thus the LP base-load forecast) upward for whichever (dow,hour) the
    # plunge happened to land on. Drop them from the sample. Kill-switch via
    # LP_LOAD_EXCLUDE_NEGATIVE_SLOTS=false.
    exclude_neg = bool(getattr(config, "LP_LOAD_EXCLUDE_NEGATIVE_SLOTS", True))
    # Drop HEM-commanded LWT-offset windows (+ thermal-lag tail) too — a positive
    # offset wakes the compressor (June phantom-heat self-loop), so that heat is
    # HEM-induced, not organic load. Same treatment as the negative-price boost.
    exclude_lwt = bool(getattr(config, "LP_LOAD_EXCLUDE_LWT_OFFSET_SLOTS", True))

    # Short-TTL cache — this 120-day rebuild runs the physics estimator over
    # ~thousands of pv samples and is called multiple times per optimizer solve
    # (optimizer + appliance_dispatch + the inputs view). The profile only moves
    # as new telemetry / the nightly Daikin sync land, so a 4-min TTL is safe and
    # collapses the per-solve cost to one rebuild. Keyed by DB_PATH so isolated
    # test DBs never share an entry.
    cache_key = (config.DB_PATH, window_days, end_date, min_samples_per_bucket, _v2, exclude_neg, exclude_lwt)
    if use_cache:
        ent = _RESIDUAL_V2_CACHE.get(cache_key)
        if ent is not None and ent[0] > time.monotonic():
            return ent[1]

    def _finish(res: dict[str, Any]) -> dict[str, Any]:
        if use_cache:
            _RESIDUAL_V2_CACHE[cache_key] = (time.monotonic() + _RESIDUAL_V2_TTL_S, res)
        return res

    cop_curve = config.DAIKIN_COP_CURVE
    tz = ZoneInfo(config.BULLETPROOF_TIMEZONE)
    # The window ENDS at end_date (local, inclusive) when given — so the Insights
    # navigator can scope the heatmap to a past period — else at now. The lower
    # bound is end − window_days. end_iso is None for the live (now) case so the
    # query keeps its original single-bound form.
    end_iso: str | None = None
    if end_date:
        try:
            end_local = datetime.fromisoformat(str(end_date)).replace(tzinfo=tz)
            anchor = (end_local + timedelta(days=1)).astimezone(UTC)  # exclusive upper bound
            end_iso = anchor.isoformat().replace("+00:00", "Z")
        except (ValueError, TypeError):
            end_iso = None
    anchor_utc = (
        datetime.fromisoformat(end_iso.replace("Z", "+00:00")) if end_iso else datetime.now(UTC)
    )
    cutoff_iso = (anchor_utc - timedelta(days=window_days)).isoformat().replace("+00:00", "Z")

    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                """SELECT pv.captured_at, pv.load_power_kw,
                          (SELECT mv.temp_c
                           FROM meteo_forecast_value mv
                           JOIN meteo_forecast_snapshot ms
                             ON ms.forecast_fetch_at_utc = mv.forecast_fetch_at_utc
                           WHERE substr(mv.slot_time, 1, 13) = substr(pv.captured_at, 1, 13)
                             AND ms.forecast_fetch_at_utc < pv.captured_at
                           ORDER BY ms.forecast_fetch_at_utc DESC LIMIT 1) AS outdoor_history_c,
                          (SELECT mv.temp_c
                           FROM meteo_forecast_value mv
                           JOIN meteo_forecast_latest_state ls
                             ON ls.id = 1
                            AND ls.forecast_fetch_at_utc = mv.forecast_fetch_at_utc
                           WHERE substr(mv.slot_time, 1, 13) = substr(pv.captured_at, 1, 13)
                           LIMIT 1) AS outdoor_latest_c
                   FROM pv_realtime_history pv
                   WHERE pv.captured_at > ? AND pv.load_power_kw IS NOT NULL"""
                + (" AND pv.captured_at < ?" if end_iso else ""),
                (cutoff_iso, end_iso) if end_iso else (cutoff_iso,),
            )
            rows = cur.fetchall()
            # Negative-price slots over the window (same source the LP priced
            # against: execution_log.agile_price_pence), keyed by half-hour UTC
            # slot start so Pass 1 can drop the deliberately-boosted samples.
            neg_slots: set[str] = set()
            if exclude_neg:
                ncur = conn.execute(
                    """SELECT DISTINCT timestamp FROM execution_log
                       WHERE timestamp > ? AND agile_price_pence < 0"""
                    + (" AND timestamp < ?" if end_iso else ""),
                    (cutoff_iso, end_iso) if end_iso else (cutoff_iso,),
                )
                for nr in ncur.fetchall():
                    try:
                        nt = datetime.fromisoformat(str(nr["timestamp"]).replace("Z", "+00:00"))
                    except (ValueError, TypeError):
                        continue
                    nsm = (nt.minute // 30) * 30
                    neg_slots.add(
                        nt.replace(minute=nsm, second=0, microsecond=0)
                        .astimezone(UTC).isoformat().replace("+00:00", "Z")
                    )
        finally:
            conn.close()

    # HEM LWT-offset windows over the same span (Tracked by #540). Built OUTSIDE
    # the lock above — get_nonzero_lwt_offset_windows takes _lock itself. Expand
    # each window [start, end + thermal-lag tail) into half-hour UTC slot keys so
    # Pass 1 can drop the HEM-induced (phantom) heating the same way it drops the
    # negative-price boost. The tail mirrors the demand-gate decontamination.
    lwt_offset_slots: set[str] = set()
    if exclude_lwt:
        tail_h = 2 * max(0, int(getattr(config, "DAIKIN_LWT_PREHEAT_DECONTAM_TAIL_BUCKETS", 1)))
        for s_iso, e_iso in get_nonzero_lwt_offset_windows(cutoff_iso[:10], anchor_utc.date().isoformat()):
            try:
                s_w = datetime.fromisoformat(s_iso.replace("Z", "+00:00")).astimezone(UTC)
                e_w = (datetime.fromisoformat(e_iso.replace("Z", "+00:00")).astimezone(UTC)
                       + timedelta(hours=tail_h))
            except (ValueError, TypeError):
                continue
            cur_t = s_w.replace(minute=(s_w.minute // 30) * 30, second=0, microsecond=0)
            while cur_t < e_w:
                lwt_offset_slots.add(cur_t.isoformat().replace("+00:00", "Z"))
                cur_t += timedelta(minutes=30)

    # Pass 1 — parse, compute the pure-physics Daikin estimate per sample, and
    # accumulate physics totals per (local_date, 2h-bucket) for calibration.
    from datetime import datetime as _dt
    samples: list[tuple[datetime, str, int, float, float]] = []  # (local, date_iso, bucket_idx, load_slot_kwh, physics_slot_kwh)
    physics_bucket: dict[tuple[str, int], float] = {}
    physics_day: dict[str, float] = {}
    dropped_no_meteo = 0
    dropped_negative = 0
    dropped_lwt_offset = 0
    for row in rows:
        outdoor = row["outdoor_history_c"]
        if outdoor is None:
            outdoor = row["outdoor_latest_c"]
        if outdoor is None:
            dropped_no_meteo += 1
            continue
        try:
            ts = _dt.fromisoformat(str(row["captured_at"]).replace("Z", "+00:00"))
            local = ts.astimezone(tz)
        except (ValueError, TypeError):
            continue
        if (exclude_neg and neg_slots) or lwt_offset_slots:
            sm = (ts.minute // 30) * 30
            skey = (ts.replace(minute=sm, second=0, microsecond=0)
                    .astimezone(UTC).isoformat().replace("+00:00", "Z"))
            if exclude_neg and skey in neg_slots:
                dropped_negative += 1
                continue
            if skey in lwt_offset_slots:
                dropped_lwt_offset += 1
                continue
        load_slot = float(row["load_power_kw"]) * 0.5
        physics_slot = _physics_daikin_slot_kwh(float(outdoor), cop_curve)
        date_iso = local.date().isoformat()
        b = local.hour // 2
        samples.append((local, date_iso, b, load_slot, physics_slot))
        physics_bucket[(date_iso, b)] = physics_bucket.get((date_iso, b), 0.0) + physics_slot
        physics_day[date_iso] = physics_day.get(date_iso, 0.0) + physics_slot

    if not samples:
        flat = mean_fox_load_kwh_per_slot(limit=60)
        if flat is None:
            flat = mean_consumption_kwh_from_execution_logs(limit=2016)
        return _finish({
            "profile": {(h, m): flat for h in range(24) for m in (0, 30)},
            "hp_profile": {}, "hp_dhw_profile": {}, "hp_space_profile": {},
            "spread": {}, "flat": flat, "away_days": [],
            "day_counts": {"weekday": 0, "weekend": 0, "away_excluded": 0,
                           "negative_excluded": dropped_negative,
                           "lwt_offset_excluded": dropped_lwt_offset, "total": 0},
            "calibrated_days": 0, "physics_only_days": 0,
        })

    # Measured Daikin split over the covered date range (helpers take _lock —
    # call them OUTSIDE our own lock block above).
    dates = sorted({s[1] for s in samples})
    measured_2h: dict[tuple[str, int], float] = {}
    # DHW fraction of the measured heat-pump energy per (date, 2h-bucket) and per
    # day, so the calibrated combined ``hp`` can be split into TANK (DHW) vs
    # HEATING (space) for the Insights heatmap. The physics estimator models only
    # space heating, so the split MUST come from the measured Onecta meters here,
    # not from the physics shape (#574 item 2).
    dhw_frac_2h: dict[tuple[str, int], float] = {}
    for r in get_daikin_consumption_2hourly_range(dates[0], dates[-1]):
        h_ = float(r.get("kwh_heating") or 0.0)
        d_ = float(r.get("kwh_dhw") or 0.0)
        tot = h_ + d_
        measured_2h[(str(r["date"]), int(r["bucket_idx"]))] = tot
        if tot > 1e-6:
            dhw_frac_2h[(str(r["date"]), int(r["bucket_idx"]))] = d_ / tot
    measured_day: dict[str, float] = {}
    dhw_frac_day: dict[str, float] = {}
    for r in get_daikin_consumption_daily_range(dates[0], dates[-1]):
        h_ = float(r.get("kwh_heating") or 0.0)
        d_ = float(r.get("kwh_dhw") or 0.0)
        tot = h_ + d_
        measured_day[str(r["date"])] = tot
        if tot > 1e-6:
            dhw_frac_day[str(r["date"])] = d_ / tot

    def _clamp_k(k: float) -> float:
        return min(5.0, max(0.2, k))

    # Calibration factor per (date, bucket): prefer 2-hourly (intra-day shape),
    # then daily, then 1.0 (pure physics). Track which days were calibrated.
    calibrated_dates: set[str] = set()

    def _calib(date_iso: str, bucket_idx: int) -> float:
        if not _v2:
            return 1.0  # kill-switch: pure physics, no measured-split calibration
        pb = physics_bucket.get((date_iso, bucket_idx), 0.0)
        mb = measured_2h.get((date_iso, bucket_idx))
        if mb is not None and pb > 1e-6:
            calibrated_dates.add(date_iso)
            return _clamp_k(mb / pb)
        pd = physics_day.get(date_iso, 0.0)
        md = measured_day.get(date_iso)
        if md is not None and pd > 1e-6:
            calibrated_dates.add(date_iso)
            return _clamp_k(md / pd)
        return 1.0

    # Pass 2 — calibrated residual per sample, grouped by date for away detection.
    # ``hp`` is the calibrated heat-pump (Daikin) slot energy we subtracted — kept
    # so we can publish its own per-(dow,hour) profile for the heat-pump heatmap.
    by_date_daytime: dict[str, list[float]] = {}
    # (local, date_iso, residual, hp, hp_dhw, hp_space)
    residual_samples: list[tuple[datetime, str, float, float, float, float]] = []

    def _dhw_frac(date_iso: str, bucket_idx: int) -> float:
        """Measured DHW share of heat-pump energy for the slot, 2h-bucket first,
        then day. Default 0.0 (all heating) when no measured split exists — the
        physics term is space-heating-shaped, so heating is the honest default."""
        f = dhw_frac_2h.get((date_iso, bucket_idx))
        if f is not None:
            return f
        f = dhw_frac_day.get(date_iso)
        return f if f is not None else 0.0

    for local, date_iso, b, load_slot, physics_slot in samples:
        k = _calib(date_iso, b)
        hp = physics_slot * k
        frac = _dhw_frac(date_iso, b)
        hp_dhw = hp * frac
        hp_space = hp - hp_dhw
        residual = max(0.0, load_slot - hp)
        residual_samples.append((local, date_iso, residual, hp, hp_dhw, hp_space))
        if 9 <= local.hour < 21:
            by_date_daytime.setdefault(date_iso, []).append(residual)

    # Away-day detection: a day is "away" when its daytime (09–21) median
    # residual is far below the population daytime median. Skip thin days.
    day_signatures = {
        d: _median(vs) for d, vs in by_date_daytime.items() if len(vs) >= 4
    }
    away_days: set[str] = set()
    if day_signatures and away_fraction > 0:
        pop_median = _median(list(day_signatures.values()))
        threshold = away_fraction * pop_median
        away_days = {d for d, sig in day_signatures.items() if sig < threshold}

    # Aggregation (away days removed) into the three tiers. ``hp_tiers`` mirrors
    # ``tiers`` for the heat-pump split so both heatmaps share the same buckets.
    tiers: dict[Any, list[float]] = {}
    hp_tiers: dict[Any, list[float]] = {}
    hp_dhw_tiers: dict[Any, list[float]] = {}
    hp_space_tiers: dict[Any, list[float]] = {}
    weekday_days: set[str] = set()
    weekend_days: set[str] = set()
    for local, date_iso, residual, hp, hp_dhw, hp_space in residual_samples:
        if date_iso in away_days:
            continue
        dow = local.weekday()
        group = "weekend" if dow >= 5 else "weekday"
        (weekend_days if dow >= 5 else weekday_days).add(date_iso)
        m = 30 if local.minute >= 30 else 0
        h = local.hour
        if _v2:  # day-of-week + weekday/weekend tiers (kill-switch off → (h,m) only)
            tiers.setdefault((dow, h, m), []).append(residual)
            tiers.setdefault((group, h, m), []).append(residual)
            for tt, val in ((hp_tiers, hp), (hp_dhw_tiers, hp_dhw), (hp_space_tiers, hp_space)):
                tt.setdefault((dow, h, m), []).append(val)
                tt.setdefault((group, h, m), []).append(val)
        tiers.setdefault((h, m), []).append(residual)
        hp_tiers.setdefault((h, m), []).append(hp)
        hp_dhw_tiers.setdefault((h, m), []).append(hp_dhw)
        hp_space_tiers.setdefault((h, m), []).append(hp_space)

    def _p75(vs: list[float]) -> float:
        if len(vs) < 2:
            return vs[0] if vs else 0.0
        return _quantiles(vs, n=4)[2]

    retained = [r for (_l, d, r, _hp, _hd, _hs) in residual_samples if d not in away_days]
    flat = _median(retained) if retained else (
        mean_fox_load_kwh_per_slot(limit=60) or mean_consumption_kwh_from_execution_logs(limit=2016)
    )

    profile: dict[Any, float] = {}
    spread: dict[Any, float] = {}
    for key, vs in tiers.items():
        if len(vs) >= min_samples_per_bucket:
            profile[key] = _median(vs)
            spread[key] = _p75(vs)

    # Heat-pump profile — same tier structure, median per bucket. Unfilled buckets
    # mean "no heat-pump signal learned there" → the lookup falls back to 0, which
    # is the honest reading (the heat pump genuinely idles in many slots).
    def _median_profile(tier_map: dict[Any, list[float]]) -> dict[Any, float]:
        return {
            key: _median(vs)
            for key, vs in tier_map.items()
            if len(vs) >= min_samples_per_bucket
        }

    hp_profile = _median_profile(hp_tiers)
    # TANK (DHW) vs HEATING (space) split of the same heat-pump energy, from the
    # measured Onecta meters (#574 item 2). Per-bucket medians won't sum exactly
    # to ``hp_profile`` (median of a sum ≠ sum of medians), but each is the honest
    # central estimate of its own component and the UI presents them as toggles.
    hp_dhw_profile = _median_profile(hp_dhw_tiers)
    hp_space_profile = _median_profile(hp_space_tiers)

    # Always fill all 48 (h, m) buckets (hour-aware fallback) so the lookup chain
    # has a guaranteed leaf below the dow/group tiers.
    hm_medians = {k: v for k, v in profile.items() if isinstance(k, tuple) and len(k) == 2}

    def _hour_aware(h: int, m: int) -> float:
        other = hm_medians.get((h, 30 if m == 0 else 0))
        if other is not None:
            return other
        neigh: list[float] = []
        for dh in (-2, -1, 1, 2):
            for nm in (0, 30):
                v = hm_medians.get(((h + dh) % 24, nm))
                if v is not None:
                    neigh.append(v)
        return _median(neigh) if neigh else flat

    for h in range(24):
        for m in (0, 30):
            if (h, m) not in profile:
                profile[(h, m)] = _hour_aware(h, m)
                spread.setdefault((h, m), profile[(h, m)])

    calibrated_days = len(calibrated_dates - away_days)
    physics_only_days = len(set(dates) - calibrated_dates - away_days)
    logger.info(
        "residual_profile_v2: %d samples (%d no-meteo, %d negative-price), window=%dd; "
        "%d weekday / %d weekend days, %d away excluded; "
        "%d calibrated / %d physics-only days",
        len(samples), dropped_no_meteo, dropped_negative, window_days,
        len(weekday_days), len(weekend_days), len(away_days),
        calibrated_days, physics_only_days,
    )
    return _finish({
        "profile": profile,
        "hp_profile": hp_profile,
        "hp_dhw_profile": hp_dhw_profile,
        "hp_space_profile": hp_space_profile,
        "spread": spread,
        "flat": float(flat),
        "away_days": sorted(away_days),
        "day_counts": {
            "weekday": len(weekday_days),
            "weekend": len(weekend_days),
            "away_excluded": len(away_days),
            "negative_excluded": dropped_negative,
            "lwt_offset_excluded": dropped_lwt_offset,
            "total": len(set(dates)),
        },
        "calibrated_days": calibrated_days,
        "physics_only_days": physics_only_days,
    })


def lookup_residual_kwh(profile_obj: dict[str, Any], dow: int, h: int, m: int) -> float:
    """Resolve the residual kWh for a slot from a :func:`residual_load_profile_v2`
    object, applying the fallback hierarchy ONCE so every caller is identical:
    ``(dow, h, m)`` → ``(group, h, m)`` → ``(h, m)`` → ``flat``."""
    prof = profile_obj.get("profile", {})
    group = "weekend" if dow >= 5 else "weekday"
    for key in ((dow, h, m), (group, h, m), (h, m)):
        v = prof.get(key)
        if v is not None:
            return float(v)
    return float(profile_obj.get("flat", 0.0))


def lookup_hp_component_kwh(
    profile_obj: dict[str, Any], dow: int, h: int, m: int, *, component: str = "hp_profile"
) -> float:
    """Resolve a heat-pump component kWh for a slot from a
    :func:`residual_load_profile_v2` object, same fallback hierarchy as
    :func:`lookup_residual_kwh`. ``component`` selects the series:
    ``hp_profile`` (combined), ``hp_dhw_profile`` (tank), or
    ``hp_space_profile`` (space heating). Returns 0.0 when no signal was learned
    for the slot (honest — the pump idles often)."""
    hp = profile_obj.get(component, {})
    group = "weekend" if dow >= 5 else "weekday"
    for key in ((dow, h, m), (group, h, m), (h, m)):
        v = hp.get(key)
        if v is not None:
            return float(v)
    return 0.0


def lookup_hp_kwh(profile_obj: dict[str, Any], dow: int, h: int, m: int) -> float:
    """Combined heat-pump (Daikin) kWh for a slot — see
    :func:`lookup_hp_component_kwh`."""
    return lookup_hp_component_kwh(profile_obj, dow, h, m, component="hp_profile")


def lookup_residual_spread_kwh(profile_obj: dict[str, Any], dow: int, h: int, m: int) -> float:
    """The p75 spread for a slot (same hierarchy as :func:`lookup_residual_kwh`).
    Returns 0.0 when no spread is known (caller falls back to the flat scenario
    factor)."""
    sp = profile_obj.get("spread", {})
    group = "weekend" if dow >= 5 else "weekday"
    for key in ((dow, h, m), (group, h, m), (h, m)):
        v = sp.get(key)
        if v is not None:
            return float(v)
    return 0.0


def half_hourly_load_profile_kwh(
    limit: int = 2016,
    *,
    window_days: int = 30,
) -> dict[tuple[int, int], float]:
    """Return per-(hour, minute) MEDIAN load (kWh per 30-min slot) from
    ``pv_realtime_history.load_power_kw`` measurements.

    S10.9 (#176): switched from ``execution_log.consumption_kwh`` (which is
    100% ``source='estimated'`` and defaults to ~0.4 kWh/slot fallback) to the
    real Fox telemetry sampled in ``pv_realtime_history``. Audit on prod
    showed the estimated-source profile overestimating real load by ~80% on
    average — the LP was over-provisioning import/charge based on noise.

    Half-hour bucket granularity (S10.8 / #175). Median per bucket is robust
    to occasional spikes (cooking, EV charge) so a couple of cold-snap days
    don't bias the prior. Falls back to the flat mean for buckets with no
    data. Returns ``{(hour, minute): kWh_per_slot}`` where minute ∈ {0, 30}.

    The ``limit`` arg is kept for API compatibility; the window-days cap is
    the actual filter (samples older than ``window_days`` are excluded).
    """
    cutoff_iso = (datetime.now(UTC) - timedelta(days=window_days)).isoformat().replace("+00:00", "Z")
    buckets: dict[tuple[int, int], list[float]] = {}
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                """SELECT captured_at, load_power_kw
                   FROM pv_realtime_history
                   WHERE captured_at > ? AND load_power_kw IS NOT NULL""",
                (cutoff_iso,),
            )
            from datetime import datetime as _dt
            for row in cur.fetchall():
                ts_str = row["captured_at"]
                kw = float(row["load_power_kw"])
                try:
                    ts = _dt.fromisoformat(str(ts_str).replace("Z", "+00:00"))
                    local = ts.astimezone(ZoneInfo(config.BULLETPROOF_TIMEZONE))
                except (ValueError, TypeError):
                    continue
                minute_bucket = 30 if local.minute >= 30 else 0
                key = (local.hour, minute_bucket)
                # Convert instantaneous kW to kWh per 30-min slot (each sample
                # represents the instantaneous power; slot energy = mean kW * 0.5h).
                buckets.setdefault(key, []).append(kw * 0.5)
        finally:
            conn.close()

    # Robust per-bucket median; fall back to global median if a bucket is empty.
    all_vals = [v for vs in buckets.values() for v in vs]
    if all_vals:
        all_vals.sort()
        global_fallback = all_vals[len(all_vals) // 2]
    else:
        global_fallback = mean_consumption_kwh_from_execution_logs(limit=limit)

    profile: dict[tuple[int, int], float] = {}
    for h in range(24):
        for m in (0, 30):
            key = (h, m)
            vs = buckets.get(key, [])
            if vs:
                vs_sorted = sorted(vs)
                profile[key] = vs_sorted[len(vs_sorted) // 2]
            else:
                profile[key] = global_fallback
    return profile


def estimate_dhw_standing_loss_c_per_hour_p50(
    *,
    limit: int = 2016,
    max_gap_hours: float = 8.0,
) -> float | None:
    """Median tank cooldown °C/h when DHW heating is off, from execution_log (#24).

    Uses consecutive log rows (time-ordered) where ``daikin_tank_power_on == 0`` and both
    rows have ``daikin_tank_temp``. Skips gaps longer than *max_gap_hours* and samples where
    the tank is warming. Returns None if there are fewer than three usable cooldown rates.

    Requires accurate ``daikin_tank_power_on`` in logs (not a constant).
    """
    from statistics import median

    rows = get_execution_logs(limit=limit)
    if len(rows) < 2:
        return None
    chrono = list(reversed(rows))

    def _parse_ts(ts: str) -> datetime | None:
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None

    rates: list[float] = []
    for i in range(len(chrono) - 1):
        a, b = chrono[i], chrono[i + 1]
        if a.get("daikin_tank_power_on") != 0:
            continue
        if b.get("daikin_tank_power_on") != 0:
            continue
        ta = a.get("daikin_tank_temp")
        tb = b.get("daikin_tank_temp")
        if ta is None or tb is None:
            continue
        tsa = _parse_ts(a.get("timestamp") or "")
        tsb = _parse_ts(b.get("timestamp") or "")
        if tsa is None or tsb is None:
            continue
        dt_h = (tsb - tsa).total_seconds() / 3600.0
        if dt_h <= 0 or dt_h > max_gap_hours:
            continue
        dtemp = float(tb) - float(ta)
        if dtemp > 0.05:
            continue
        rate = -dtemp / dt_h
        if 0 < rate < 5.0:
            rates.append(rate)

    if len(rates) < 3:
        return None
    return float(median(rates))


def log_execution(row: dict[str, Any]) -> None:
    cols = [
        "timestamp",
        "consumption_kwh",
        "agile_price_pence",
        "svt_shadow_price_pence",
        "fixed_shadow_price_pence",
        "cost_realised_pence",
        "cost_svt_shadow_pence",
        "cost_fixed_shadow_pence",
        "delta_vs_svt_pence",
        "delta_vs_fixed_pence",
        "soc_percent",
        "fox_mode",
        "daikin_lwt_offset",
        "daikin_tank_temp",
        "daikin_tank_target",
        "daikin_tank_power_on",
        "daikin_powerful_mode",
        "daikin_room_temp",
        "daikin_outdoor_temp",
        "daikin_lwt",
        "forecast_temp_c",
        "forecast_solar_kw",
        "forecast_heating_demand",
        "slot_kind",
        "source",
    ]
    values = [row.get(c) for c in cols]
    placeholders = ",".join("?" * len(cols))
    with _lock:
        conn = get_connection()
        try:
            conn.execute(
                f"INSERT INTO execution_log ({','.join(cols)}) VALUES ({placeholders})",
                values,
            )
            conn.commit()
        finally:
            conn.close()


def log_action(
    *,
    device: str,
    action: str,
    params: dict[str, Any] | None,
    result: str,
    trigger: str,
    error_msg: str | None = None,
    slot_kind: str | None = None,
    agile_price_at_time: float | None = None,
    started_at: str | None = None,
    completed_at: str | None = None,
    duration_ms: int | None = None,
    actor: str | None = None,
) -> None:
    """Append one row to action_log.

    The canonical single-timestamp call shape is unchanged; the four
    extended args (started_at, completed_at, duration_ms, actor) are
    populated only by :func:`log_action_timed` when duration tracking
    matters. Left nullable so legacy callers + the fast-path notification
    emitter stay wire-compatible.
    """
    ts = datetime.now(UTC).isoformat()
    with _lock:
        conn = get_connection()
        try:
            conn.execute(
                """INSERT INTO action_log
                   (timestamp, device, action, params, result, error_msg, trigger,
                    slot_kind, agile_price_at_time,
                    started_at, completed_at, duration_ms, actor)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    ts,
                    device,
                    action,
                    json.dumps(params or {}),
                    result,
                    error_msg,
                    trigger,
                    slot_kind,
                    agile_price_at_time,
                    started_at,
                    completed_at,
                    duration_ms,
                    actor,
                ),
            )
            conn.commit()
        finally:
            conn.close()


@contextlib.contextmanager
def log_action_timed(
    *,
    device: str,
    action: str,
    params: dict[str, Any] | None,
    trigger: str,
    actor: str | None = None,
    slot_kind: str | None = None,
    agile_price_at_time: float | None = None,
) -> Any:
    """Context manager that times an action block and writes a single
    action_log row on exit with started_at / completed_at / duration_ms.

    Usage:

        with db.log_action_timed(
            device="foxess", action="set_work_mode",
            params={"mode": "Self Use"}, trigger="mcp", actor="mcp",
        ):
            fox.set_work_mode("Self Use")

    On a raised exception the row is still written with ``result="failure"``
    and ``error_msg`` set, then the exception re-propagates — so callers get
    duration tracking for the failure path too.
    """
    import time as _time
    started_mono = _time.monotonic()
    started_iso = datetime.now(UTC).isoformat()
    try:
        yield
    except Exception as e:
        completed_iso = datetime.now(UTC).isoformat()
        elapsed_ms = int((_time.monotonic() - started_mono) * 1000)
        log_action(
            device=device,
            action=action,
            params=params,
            result="failure",
            trigger=trigger,
            error_msg=str(e)[:500],
            slot_kind=slot_kind,
            agile_price_at_time=agile_price_at_time,
            started_at=started_iso,
            completed_at=completed_iso,
            duration_ms=elapsed_ms,
            actor=actor,
        )
        raise
    completed_iso = datetime.now(UTC).isoformat()
    elapsed_ms = int((_time.monotonic() - started_mono) * 1000)
    log_action(
        device=device,
        action=action,
        params=params,
        result="success",
        trigger=trigger,
        slot_kind=slot_kind,
        agile_price_at_time=agile_price_at_time,
        started_at=started_iso,
        completed_at=completed_iso,
        duration_ms=elapsed_ms,
        actor=actor,
    )


def get_recent_triggers(limit: int = 20, exclude_triggers: list[str] | None = None) -> list[dict[str, Any]]:
    """Recent action_log rows for the cockpit 'Recent triggers' strip.

    Default filter excludes heartbeat + notification noise so the user
    sees meaningful events: manual writes, plan proposes, scheduler cron
    fires. Returns newest-first; ``duration_ms`` is null for rows written
    via the fast-path log_action (e.g. notification dispatch).
    """
    if exclude_triggers is None:
        exclude_triggers = ["heartbeat", "notification"]
    with _lock:
        conn = get_connection()
        try:
            placeholders = ",".join("?" for _ in exclude_triggers) if exclude_triggers else ""
            where = f"WHERE trigger NOT IN ({placeholders})" if placeholders else ""
            cur = conn.execute(
                f"""SELECT id, timestamp, device, action, params, result, error_msg, trigger,
                           slot_kind, agile_price_at_time, started_at, completed_at, duration_ms, actor
                    FROM action_log
                    {where}
                    ORDER BY timestamp DESC
                    LIMIT ?""",
                (*exclude_triggers, int(limit)),
            )
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()


def log_optimizer_run(row: dict[str, Any]) -> int:
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                """INSERT INTO optimizer_log
                   (run_at, rates_count, cheap_slots, peak_slots, standard_slots, negative_slots,
                    target_vwap, actual_agile_mean, battery_warning, strategy_summary,
                    fox_schedule_uploaded, daikin_actions_count)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    row.get("run_at"),
                    row.get("rates_count"),
                    row.get("cheap_slots"),
                    row.get("peak_slots"),
                    row.get("standard_slots"),
                    row.get("negative_slots"),
                    row.get("target_vwap"),
                    row.get("actual_agile_mean"),
                    1 if row.get("battery_warning") else 0,
                    row.get("strategy_summary"),
                    1 if row.get("fox_schedule_uploaded") else 0,
                    row.get("daikin_actions_count"),
                ),
            )
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()


def save_daily_target(row: dict[str, Any]) -> None:
    with _lock:
        conn = get_connection()
        try:
            conn.execute(
                """INSERT INTO daily_targets
                   (date, target_vwap, estimated_total_kwh, estimated_cost_pence,
                    cheap_threshold, peak_threshold, forecast_min_temp_c, forecast_max_temp_c,
                    forecast_total_solar_kwh, strategy_summary)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(date) DO UPDATE SET target_vwap=excluded.target_vwap,
                     estimated_total_kwh=excluded.estimated_total_kwh,
                     estimated_cost_pence=excluded.estimated_cost_pence,
                     cheap_threshold=excluded.cheap_threshold,
                     peak_threshold=excluded.peak_threshold,
                     forecast_min_temp_c=excluded.forecast_min_temp_c,
                     forecast_max_temp_c=excluded.forecast_max_temp_c,
                     forecast_total_solar_kwh=excluded.forecast_total_solar_kwh,
                     strategy_summary=excluded.strategy_summary""",
                (
                    row["date"],
                    row.get("target_vwap"),
                    row.get("estimated_total_kwh"),
                    row.get("estimated_cost_pence"),
                    row.get("cheap_threshold"),
                    row.get("peak_threshold"),
                    row.get("forecast_min_temp_c"),
                    row.get("forecast_max_temp_c"),
                    row.get("forecast_total_solar_kwh"),
                    row.get("strategy_summary"),
                ),
            )
            conn.commit()
        finally:
            conn.close()


def get_daily_target(d: date | str) -> dict[str, Any] | None:
    key = d.isoformat() if isinstance(d, date) else str(d)
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute("SELECT * FROM daily_targets WHERE date = ?", (key,))
            r = cur.fetchone()
            return dict(r) if r else None
        finally:
            conn.close()


def get_execution_logs(
    from_ts: str | None = None,
    to_ts: str | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    with _lock:
        conn = get_connection()
        try:
            q = "SELECT * FROM execution_log WHERE 1=1"
            args: list[Any] = []
            if from_ts:
                q += " AND timestamp >= ?"
                args.append(from_ts)
            if to_ts:
                q += " AND timestamp <= ?"
                args.append(to_ts)
            q += " ORDER BY timestamp DESC LIMIT ?"
            args.append(limit)
            cur = conn.execute(q, args)
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()


def get_action_logs(
    device: str | None = None,
    trigger: str | None = None,
    limit: int = 200,
    since: str | None = None,
    action: str | None = None,
) -> list[dict[str, Any]]:
    with _lock:
        conn = get_connection()
        try:
            q = "SELECT * FROM action_log WHERE 1=1"
            args: list[Any] = []
            if device:
                q += " AND device = ?"
                args.append(device)
            if trigger:
                q += " AND trigger = ?"
                args.append(trigger)
            if action:
                q += " AND action = ?"
                args.append(action)
            if since:
                q += " AND timestamp >= ?"
                args.append(since)
            q += " ORDER BY timestamp DESC LIMIT ?"
            args.append(limit)
            cur = conn.execute(q, args)
            rows = []
            for r in cur.fetchall():
                d = dict(r)
                try:
                    d["params"] = json.loads(d["params"] or "{}")
                except json.JSONDecodeError:
                    d["params"] = {}
                rows.append(d)
            return rows
        finally:
            conn.close()


def get_optimizer_logs(limit: int = 50) -> list[dict[str, Any]]:
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                "SELECT * FROM optimizer_log ORDER BY run_at DESC LIMIT ?", (limit,)
            )
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()


def save_fox_schedule_state(groups: list[dict[str, Any]], enabled: bool = True) -> None:
    now = datetime.now(UTC).isoformat()
    with _lock:
        conn = get_connection()
        try:
            conn.execute(
                """INSERT INTO fox_schedule_state (uploaded_at, groups_json, enabled, verified_at)
                   VALUES (?, ?, ?, ?)""",
                (now, json.dumps(groups), 1 if enabled else 0, now),
            )
            conn.commit()
        finally:
            conn.close()


def get_recent_fox_schedule_states(limit: int = 6) -> list[dict[str, Any]]:
    """Last *limit* schedule-state rows, newest first, with parsed ``groups``.

    The in-flight bridge (#693) walks these to find the schedule that was in
    force at the current slot's start — looking back past a boot-time
    safe-defaults wipe row (disabled/empty) that postdates the slot start.
    """
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                "SELECT * FROM fox_schedule_state ORDER BY id DESC LIMIT ?",
                (int(limit),),
            )
            out = []
            for r in cur.fetchall():
                d = dict(r)
                try:
                    d["groups"] = json.loads(d["groups_json"])
                except json.JSONDecodeError:
                    d["groups"] = []
                out.append(d)
            return out
        finally:
            conn.close()


def get_latest_fox_schedule_state() -> dict[str, Any] | None:
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                "SELECT * FROM fox_schedule_state ORDER BY id DESC LIMIT 1"
            )
            r = cur.fetchone()
            if not r:
                return None
            d = dict(r)
            try:
                d["groups"] = json.loads(d["groups_json"])
            except json.JSONDecodeError:
                d["groups"] = []
            return d
        finally:
            conn.close()


def acknowledge_warning(warning_key: str) -> None:
    now = datetime.now(UTC).isoformat()
    with _lock:
        conn = get_connection()
        try:
            conn.execute(
                """INSERT INTO acknowledged_warnings (warning_key, acknowledged_at)
                   VALUES (?, ?)
                   ON CONFLICT(warning_key) DO UPDATE SET acknowledged_at=excluded.acknowledged_at""",
                (warning_key, now),
            )
            conn.commit()
        finally:
            conn.close()


def is_warning_acknowledged(warning_key: str) -> bool:
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                "SELECT 1 FROM acknowledged_warnings WHERE warning_key = ?",
                (warning_key,),
            )
            return cur.fetchone() is not None
        finally:
            conn.close()


def clear_warning(warning_key: str) -> None:
    """Remove a previously-acknowledged warning so it can re-fire.

    Used when the underlying condition recovers — e.g. Fox scheduler flag
    flips back to True after being False — so the next failure today gets
    a fresh notification rather than silently inheriting the prior ack.
    """
    with _lock:
        conn = get_connection()
        try:
            conn.execute(
                "DELETE FROM acknowledged_warnings WHERE warning_key = ?",
                (warning_key,),
            )
            conn.commit()
        finally:
            conn.close()


@dataclass
class OctopusFetchState:
    last_success_at: str | None
    last_attempt_at: str | None
    consecutive_failures: int
    survival_mode_since: str | None
    failure_streak_started_at: str | None = None


def get_octopus_fetch_state() -> OctopusFetchState:
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute("SELECT * FROM octopus_fetch_state WHERE id = 1")
            r = cur.fetchone()
            if not r:
                return OctopusFetchState(None, None, 0, None, None)
            streak = None
            try:
                streak = r["failure_streak_started_at"]
            except (KeyError, IndexError):
                streak = None
            return OctopusFetchState(
                r["last_success_at"],
                r["last_attempt_at"],
                int(r["consecutive_failures"] or 0),
                r["survival_mode_since"],
                streak,
            )
        finally:
            conn.close()


def update_octopus_fetch_state(
    *,
    last_success_at: str | None = None,
    last_attempt_at: str | None = None,
    consecutive_failures: int | None = None,
    survival_mode_since: str | None = None,
    failure_streak_started_at: str | None = None,
    clear_failure_streak: bool = False,
) -> None:
    with _lock:
        conn = get_connection()
        try:
            parts = []
            args: list[Any] = []
            if last_success_at is not None:
                parts.append("last_success_at = ?")
                args.append(last_success_at)
            if last_attempt_at is not None:
                parts.append("last_attempt_at = ?")
                args.append(last_attempt_at)
            if consecutive_failures is not None:
                parts.append("consecutive_failures = ?")
                args.append(consecutive_failures)
            if survival_mode_since is not None:
                parts.append("survival_mode_since = ?")
                args.append(survival_mode_since)
            if failure_streak_started_at is not None:
                parts.append("failure_streak_started_at = ?")
                args.append(failure_streak_started_at)
            if clear_failure_streak:
                parts.append("failure_streak_started_at = NULL")
            if parts:
                q = "UPDATE octopus_fetch_state SET " + ", ".join(parts) + " WHERE id = 1"
                conn.execute(q, args)
                conn.commit()
        finally:
            conn.close()


def _parse_action_time_utc(s: str) -> datetime:
    x = str(s).replace("Z", "+00:00")
    dt = datetime.fromisoformat(x)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _now_utc() -> datetime:
    """Wall clock for replan preservation tests (monkeypatch target)."""
    return datetime.now(UTC)


def _preserve_daikin_pending_ids_for_replan(plan_date: str) -> set[int]:
    """Row ids that must survive clear_actions before a replan (issue #27).

    * Pending **restore** rows referenced by an **active** main action (otherwise the
      post-shutdown restore disappears while the commanded state is still active).
    * Pending main actions in ``[start, end)`` (MPC can run before heartbeat marks them
      ``active``) plus their paired restore row, if any.
    * Issue #382 — pending **restore** rows whose ``start_time`` is within
      ``RESTORE_PRESERVE_LEAD_MINUTES`` of now, regardless of parent status.
      The 2026-05-21 incident showed the existing rules don't cover a
      restore whose parent has already failed/completed: the parent isn't
      ``active`` (rule #1 misses) and the restore window itself hasn't
      started (rule #2 misses). Without a third rule the LP-replan sweep
      deletes an imminent restore and the next solve doesn't necessarily
      re-emit one (it may see "tank already off, fine" and never command
      the recovery), leaving the tank to drain.
    """
    from .config import config as _cfg
    now_utc = _now_utc()
    lead_minutes = float(getattr(_cfg, "RESTORE_PRESERVE_LEAD_MINUTES", 10.0))
    lead_cutoff = now_utc + timedelta(minutes=lead_minutes) if lead_minutes > 0 else None
    preserve: set[int] = set()
    for r in get_actions_for_plan_date(plan_date, device="daikin"):
        st = r.get("status") or ""
        if st == "active" and r.get("restore_action_id"):
            preserve.add(int(r["restore_action_id"]))
        if st != "pending":
            continue
        try:
            start = _parse_action_time_utc(str(r["start_time"]))
            end = _parse_action_time_utc(str(r["end_time"]))
        except (ValueError, KeyError, TypeError):
            continue
        if start <= now_utc < end:
            preserve.add(int(r["id"]))
            rid = r.get("restore_action_id")
            if rid is not None:
                preserve.add(int(rid))
        if (
            lead_cutoff is not None
            and r.get("action_type") == "restore"
            and start <= lead_cutoff
        ):
            preserve.add(int(r["id"]))
    return preserve


def _preserve_daikin_pending_ids_in_range(
    start_utc_iso: str, end_utc_iso: str
) -> set[int]:
    """Range variant of :func:`_preserve_daikin_pending_ids_for_replan`.

    Active-row restore pointers are scanned globally (not range-filtered) because
    an in-flight action's *restore* row typically falls ahead of ``now`` and is
    therefore inside the rolling clear window even when the active row itself is
    not. In-flight pending preservation scans only the pending rows whose
    ``start_time`` falls in ``[start_utc_iso, end_utc_iso)``.

    Issue #382 — additionally preserves pending ``restore`` rows whose
    ``start_time`` is within ``RESTORE_PRESERVE_LEAD_MINUTES`` of now,
    regardless of parent state. See the docstring on
    :func:`_preserve_daikin_pending_ids_for_replan` for the rationale.
    """
    from .config import config as _cfg
    now_utc = _now_utc()
    lead_minutes = float(getattr(_cfg, "RESTORE_PRESERVE_LEAD_MINUTES", 10.0))
    lead_cutoff = now_utc + timedelta(minutes=lead_minutes) if lead_minutes > 0 else None
    preserve: set[int] = set()
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                """SELECT restore_action_id FROM action_schedule
                   WHERE device = 'daikin' AND status = 'active'
                         AND restore_action_id IS NOT NULL"""
            )
            for row in cur.fetchall():
                rid = row["restore_action_id"] if hasattr(row, "keys") else row[0]
                if rid is not None:
                    preserve.add(int(rid))

            cur = conn.execute(
                """SELECT id, start_time, end_time, restore_action_id, action_type
                   FROM action_schedule
                   WHERE device = 'daikin' AND status = 'pending'
                         AND start_time >= ? AND start_time < ?""",
                (start_utc_iso, end_utc_iso),
            )
            for row in cur.fetchall():
                try:
                    start = _parse_action_time_utc(str(row["start_time"]))
                    end = _parse_action_time_utc(str(row["end_time"]))
                except (ValueError, KeyError, TypeError):
                    continue
                if start <= now_utc < end:
                    preserve.add(int(row["id"]))
                    rid = row["restore_action_id"]
                    if rid is not None:
                        preserve.add(int(rid))
                if (
                    lead_cutoff is not None
                    and row["action_type"] == "restore"
                    and start <= lead_cutoff
                ):
                    preserve.add(int(row["id"]))
        finally:
            conn.close()
    return preserve


def clear_actions_for_date(plan_date: str, device: str | None = None) -> None:
    """Remove pending actions for a plan date (before re-optimizing).

    Daikin: does **not** delete pending rows still needed for in-flight execution — see
    :func:`_preserve_daikin_pending_ids_for_replan`.
    """
    preserve_ids: set[int] = set()
    if device == "daikin":
        preserve_ids = _preserve_daikin_pending_ids_for_replan(plan_date)
    with _lock:
        conn = get_connection()
        try:
            if device:
                if preserve_ids:
                    placeholders = ",".join("?" * len(preserve_ids))
                    q = (
                        f"""DELETE FROM action_schedule
                            WHERE date = ? AND device = ? AND status = 'pending'
                            AND id NOT IN ({placeholders})"""
                    )
                    conn.execute(
                        q,
                        (plan_date, device, *sorted(preserve_ids)),
                    )
                else:
                    conn.execute(
                        """DELETE FROM action_schedule
                           WHERE date = ? AND device = ? AND status = 'pending'""",
                        (plan_date, device),
                    )
            else:
                conn.execute(
                    """DELETE FROM action_schedule
                       WHERE date = ? AND status = 'pending'""",
                    (plan_date,),
                )
            conn.commit()
        finally:
            conn.close()


def clear_actions_in_range(
    start_utc_iso: str,
    end_utc_iso: str,
    device: str | None = None,
) -> None:
    """Remove pending actions whose ``start_time`` falls in ``[start, end)``.

    Used by the rolling 24 h planner: a plan written at, e.g., 18:00 today
    straddles today and tomorrow, so clearing by a single ``plan_date`` leaves
    the other date's stale rows in place. Daikin rows inherit the same
    in-flight preservation semantics as :func:`clear_actions_for_date`.
    """
    preserve_ids: set[int] = set()
    if device == "daikin":
        preserve_ids = _preserve_daikin_pending_ids_in_range(start_utc_iso, end_utc_iso)
    with _lock:
        conn = get_connection()
        try:
            if device:
                if preserve_ids:
                    placeholders = ",".join("?" * len(preserve_ids))
                    q = (
                        f"""DELETE FROM action_schedule
                            WHERE device = ? AND status = 'pending'
                            AND start_time >= ? AND start_time < ?
                            AND id NOT IN ({placeholders})"""
                    )
                    conn.execute(
                        q,
                        (device, start_utc_iso, end_utc_iso, *sorted(preserve_ids)),
                    )
                else:
                    conn.execute(
                        """DELETE FROM action_schedule
                           WHERE device = ? AND status = 'pending'
                           AND start_time >= ? AND start_time < ?""",
                        (device, start_utc_iso, end_utc_iso),
                    )
            else:
                conn.execute(
                    """DELETE FROM action_schedule
                       WHERE status = 'pending'
                       AND start_time >= ? AND start_time < ?""",
                    (start_utc_iso, end_utc_iso),
                )
            conn.commit()
        finally:
            conn.close()


def schedule_for_date(plan_date: str) -> list[dict[str, Any]]:
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                """SELECT * FROM action_schedule WHERE date = ?
                   ORDER BY device, start_time""",
                (plan_date,),
            )
            return [_row_action(r) for r in cur.fetchall()]
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# V2: meteo_forecast
# ---------------------------------------------------------------------------


def save_meteo_forecast_snapshot(
    forecast_fetch_at_utc: str,
    rows: list[dict[str, Any]],
    *,
    source: str = "open-meteo",
    model_name: str | None = None,
    model_version: str | None = None,
    raw_payload_json: str | None = None,
    mark_latest: bool = True,
) -> int:
    """Persist one canonical forecast fetch and its normalized slot rows."""
    if not rows:
        return 0
    n = 0
    with _lock:
        conn = get_connection()
        try:
            conn.execute(
                """INSERT OR IGNORE INTO meteo_forecast_snapshot
                   (forecast_fetch_at_utc, source, model_name, model_version, raw_payload_json)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    str(forecast_fetch_at_utc),
                    str(source),
                    model_name,
                    model_version,
                    raw_payload_json,
                ),
            )
            for r in rows:
                slot = r.get("slot_time")
                if not slot:
                    continue
                conn.execute(
                    """INSERT OR IGNORE INTO meteo_forecast_value
                       (forecast_fetch_at_utc, slot_time, temp_c, solar_w_m2, cloud_cover_pct, direct_pv_kw)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        str(forecast_fetch_at_utc),
                        str(slot),
                        r.get("temp_c"),
                        r.get("solar_w_m2"),
                        r.get("cloud_cover_pct"),
                        r.get("direct_pv_kw"),
                    ),
                )
                n += 1
            if mark_latest:
                conn.execute(
                    """INSERT INTO meteo_forecast_latest_state (id, forecast_fetch_at_utc)
                       VALUES (1, ?)
                       ON CONFLICT(id) DO UPDATE SET forecast_fetch_at_utc=excluded.forecast_fetch_at_utc""",
                    (str(forecast_fetch_at_utc),),
                )
            conn.commit()
        finally:
            conn.close()
    return n


def _get_latest_meteo_forecast_fetch_at(conn: sqlite3.Connection) -> str | None:
    cur = conn.execute(
        "SELECT forecast_fetch_at_utc FROM meteo_forecast_latest_state WHERE id = 1"
    )
    row = cur.fetchone()
    if row and row[0]:
        return str(row[0])
    cur = conn.execute(
        "SELECT forecast_fetch_at_utc FROM meteo_forecast_snapshot ORDER BY forecast_fetch_at_utc DESC LIMIT 1"
    )
    row = cur.fetchone()
    return str(row[0]) if row and row[0] else None


def _get_latest_meteo_forecast_rows_for_slot_date(
    conn: sqlite3.Connection,
    slot_date: str,
) -> list[dict[str, Any]]:
    """Return the latest canonical forecast row for each slot on *slot_date*.

    Canonical forecast storage keeps one snapshot per fetch plus normalized slot
    rows. For replay and live diagnostics we want the most recent row for each
    slot_time, not just the most recent fetch as a whole.
    """
    cur = conn.execute(
        """SELECT substr(mv.slot_time, 1, 10) AS forecast_date,
                  mv.slot_time, mv.temp_c, mv.solar_w_m2, mv.cloud_cover_pct,
                  mv.direct_pv_kw
             FROM meteo_forecast_value mv
             JOIN (
                 SELECT slot_time, MAX(forecast_fetch_at_utc) AS forecast_fetch_at_utc
                   FROM meteo_forecast_value
                  WHERE substr(slot_time, 1, 10) = ?
                  GROUP BY slot_time
             ) latest
               ON latest.slot_time = mv.slot_time
              AND latest.forecast_fetch_at_utc = mv.forecast_fetch_at_utc
            WHERE substr(mv.slot_time, 1, 10) = ?
            ORDER BY mv.slot_time""",
        (slot_date, slot_date),
    )
    return [dict(r) for r in cur.fetchall()]


def save_meteo_forecast(rows: list[dict[str, Any]], forecast_date: str) -> int:
    """Legacy helper that writes a latest forecast snapshot.

    ``forecast_date`` is retained for compatibility with older callers/tests,
    but canonical production writes should use
    :func:`save_meteo_forecast_snapshot`.
    """
    _ = forecast_date
    return save_meteo_forecast_snapshot(datetime.now(UTC).isoformat(), rows, mark_latest=True)


def get_meteo_forecast(forecast_date: str) -> list[dict[str, Any]]:
    """Return latest forecast rows whose target slot falls on *forecast_date*."""
    with _lock:
        conn = get_connection()
        try:
            rows = _get_latest_meteo_forecast_rows_for_slot_date(conn, forecast_date)
            if rows:
                return rows
            cur = conn.execute(
                "SELECT * FROM meteo_forecast WHERE forecast_date = ? ORDER BY slot_time",
                (forecast_date,),
            )
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()


def get_meteo_forecast_for_slot_date(slot_date: str) -> list[dict[str, Any]]:
    """Return meteo_forecast rows whose slot_time falls on *slot_date*.

    This is distinct from ``forecast_date``: the latter is the solve date used
    when rows were last upserted, while slot_time identifies the actual target
    day the LP/heartbeat needs to reason about.
    """
    with _lock:
        conn = get_connection()
        try:
            rows = _get_latest_meteo_forecast_rows_for_slot_date(conn, slot_date)
            if rows:
                return rows
            cur = conn.execute(
                """SELECT * FROM meteo_forecast
                   WHERE substr(slot_time, 1, 10) = ?
                   ORDER BY slot_time""",
                (slot_date,),
            )
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()


def get_meteo_forecast_at_time(when_utc: str) -> dict[str, Any] | None:
    """Return the forecast row active at *when_utc*.

    Prefers the latest slot at or before ``when_utc``. If the request lands
    before the first available slot, falls back to the earliest future slot so
    bootstrapping still has something to work with.
    """
    with _lock:
        conn = get_connection()
        try:
            latest_fetch_at = _get_latest_meteo_forecast_fetch_at(conn)
            if latest_fetch_at:
                cur = conn.execute(
                    """SELECT substr(slot_time, 1, 10) AS forecast_date,
                              slot_time, temp_c, solar_w_m2, cloud_cover_pct,
                              direct_pv_kw
                       FROM meteo_forecast_value
                       WHERE forecast_fetch_at_utc = ?
                         AND slot_time <= ?
                       ORDER BY slot_time DESC
                       LIMIT 1""",
                    (latest_fetch_at, when_utc),
                )
                row = cur.fetchone()
                if row is not None:
                    return dict(row)
                cur = conn.execute(
                    """SELECT substr(slot_time, 1, 10) AS forecast_date,
                              slot_time, temp_c, solar_w_m2, cloud_cover_pct,
                              direct_pv_kw
                       FROM meteo_forecast_value
                       WHERE forecast_fetch_at_utc = ?
                       ORDER BY slot_time ASC
                       LIMIT 1""",
                    (latest_fetch_at,),
                )
                row = cur.fetchone()
                if row is not None:
                    return dict(row)
            cur = conn.execute(
                """SELECT * FROM meteo_forecast
                   WHERE slot_time <= ?
                   ORDER BY slot_time DESC
                   LIMIT 1""",
                (when_utc,),
            )
            row = cur.fetchone()
            if row is not None:
                return dict(row)
            cur = conn.execute(
                """SELECT * FROM meteo_forecast
                   ORDER BY slot_time ASC
                   LIMIT 1""",
            )
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()



def get_micro_climate_offset_c(lookback: int = 96) -> float:
    """Return mean(actual - forecast) outdoor-temp offset from recent skill rows.

    Preference order:
      1. ``forecast_skill_log`` derived rows, rebuilt from canonical forecast +
         Daikin telemetry.
      2. Legacy ``execution_log`` rows, for bootstrap / backwards compatibility.

    A positive result means the local microclimate runs warmer than forecast;
    negative means colder. Returns 0.0 when no usable rows exist.
    """
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                """
                SELECT AVG(actual_temp_c - predicted_temp_c)
                FROM (
                    SELECT actual_temp_c, predicted_temp_c
                    FROM forecast_skill_log
                    WHERE actual_temp_c IS NOT NULL
                      AND predicted_temp_c IS NOT NULL
                      AND actual_temp_c != predicted_temp_c
                    ORDER BY built_at_utc DESC, date_utc DESC, hour_of_day DESC
                    LIMIT ?
                )
                """,
                (lookback,),
            )
            row = cur.fetchone()
            val = row[0] if row and row[0] is not None else None
            if val is not None:
                return float(val)

            cur = conn.execute(
                """
                SELECT AVG(daikin_outdoor_temp - forecast_temp_c)
                FROM (
                    SELECT daikin_outdoor_temp, forecast_temp_c
                    FROM execution_log
                    WHERE daikin_outdoor_temp IS NOT NULL
                      AND forecast_temp_c IS NOT NULL
                      AND daikin_outdoor_temp != forecast_temp_c
                    ORDER BY id DESC
                    LIMIT ?
                )
                """,
                (lookback,),
            )
            row = cur.fetchone()
            val = row[0] if row and row[0] is not None else 0.0
            return float(val)
        finally:
            conn.close()


def get_micro_climate_offset_by_hour_c(lookback: int = 96) -> dict[int, float]:
    """Return mean(actual - forecast) offsets grouped by UTC hour-of-day.

    The rows are drawn from ``forecast_skill_log`` because that table already
    represents the canonical forecast-vs-actual comparison. Legacy execution
    logs are intentionally not mixed into the hourly map to avoid duplicating
    the raw source of truth.
    """
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                """
                SELECT hour_of_day, AVG(actual_temp_c - predicted_temp_c) AS offset_c
                FROM (
                    SELECT hour_of_day, actual_temp_c, predicted_temp_c
                    FROM forecast_skill_log
                    WHERE actual_temp_c IS NOT NULL
                      AND predicted_temp_c IS NOT NULL
                      AND actual_temp_c != predicted_temp_c
                    ORDER BY built_at_utc DESC, date_utc DESC, hour_of_day DESC
                    LIMIT ?
                )
                GROUP BY hour_of_day
                ORDER BY hour_of_day ASC
                """,
                (lookback,),
            )
            out: dict[int, float] = {}
            for row in cur.fetchall():
                hour = row[0]
                offset = row[1]
                if hour is None or offset is None:
                    continue
                out[int(hour)] = float(offset)
            return out
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# V3: Fox ESS daily energy cache
# ---------------------------------------------------------------------------

def compute_fox_energy_daily_from_realtime(
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    max_gap_seconds: int = 1800,
) -> list[dict[str, Any]]:
    """Aggregate ``pv_realtime_history`` instantaneous-power samples into per-day
    kWh totals via trapezoidal integration.

    For each consecutive pair of samples ``(t_a, P_a)`` → ``(t_b, P_b)``, energy
    contribution is ``mean(P_a, P_b) × dt`` where ``dt`` is capped at
    ``max_gap_seconds`` (default 30 min — matches the PV telemetry job's default
    cadence so a normal pair contributes its full interval, while a multi-hour
    heartbeat outage doesn't extrapolate constant power across the gap).

    Daily bucket = UTC date of the *earlier* sample of each pair. Boundary error
    around midnight is bounded by ``max_gap_seconds`` (≤ 10 min × peak power ≈
    < 1 kWh/day worst case at full PV).

    S10.10 (#177) — replaces the broken Fox Cloud per-day API rollup with a
    local computation from telemetry the heartbeat captures every ~3 min.
    Zero Fox quota cost; uses our actual measurements (more accurate than
    Fox Cloud's possibly-rounded summary).

    Returns a list of dicts ready for :func:`upsert_fox_energy_daily`.
    """
    from collections import defaultdict
    from datetime import datetime as _dt

    if start_date is None:
        start_date = (date.today() - timedelta(days=30)).isoformat()
    if end_date is None:
        end_date = date.today().isoformat()

    # Pull samples in the window. Include one sample BEFORE start so the first
    # interval inside the window is integrated correctly.
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                """SELECT captured_at, solar_power_kw, load_power_kw,
                          grid_import_kw, grid_export_kw,
                          battery_charge_kw, battery_discharge_kw
                   FROM pv_realtime_history
                   WHERE substr(captured_at, 1, 10) BETWEEN ? AND ?
                   ORDER BY captured_at""",
                (start_date, end_date),
            )
            rows_raw = cur.fetchall()
        finally:
            conn.close()

    if len(rows_raw) < 2:
        return []

    # Per-day accumulators, kWh
    METRICS = ("solar", "load", "import", "export", "charge", "discharge")
    COL_BY_METRIC = {
        "solar": 1, "load": 2, "import": 3, "export": 4,
        "charge": 5, "discharge": 6,
    }
    daily: dict[str, dict[str, float]] = defaultdict(lambda: {m: 0.0 for m in METRICS})

    def _parse(ts_raw: str) -> _dt | None:
        try:
            ts = _dt.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
            return ts if ts.tzinfo else ts.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            return None

    prev_ts: _dt | None = None
    prev_vals: list[float] = []
    for row in rows_raw:
        ts = _parse(row[0])
        if ts is None:
            continue
        # Treat None as 0 (interpret missing telemetry as no flow at that instant)
        cur_vals = [float(row[i]) if row[i] is not None else 0.0 for i in range(1, 7)]
        if prev_ts is not None:
            dt_s = min((ts - prev_ts).total_seconds(), max_gap_seconds)
            if dt_s > 0:
                day = prev_ts.date().isoformat()
                bucket = daily[day]
                hours = dt_s / 3600.0
                for m in METRICS:
                    idx = COL_BY_METRIC[m]
                    avg_kw = (prev_vals[idx - 1] + cur_vals[idx - 1]) / 2.0
                    bucket[m] += avg_kw * hours
        prev_ts = ts
        prev_vals = cur_vals

    out: list[dict[str, Any]] = []
    for day in sorted(daily.keys()):
        # Only emit days strictly within the requested range
        if not (start_date <= day <= end_date):
            continue
        b = daily[day]
        out.append({
            "date": day,
            "solar_kwh": round(b["solar"], 3),
            "load_kwh": round(b["load"], 3),
            "import_kwh": round(b["import"], 3),
            "export_kwh": round(b["export"], 3),
            "charge_kwh": round(b["charge"], 3),
            "discharge_kwh": round(b["discharge"], 3),
        })
    return out


def _half_hourly_grid_kwh_for_day(
    day: date,
    column: str,
    *,
    max_gap_seconds: int = 1800,
) -> dict[str, float]:
    """Shared trapezoidal-integration over ``pv_realtime_history.<column>`` for
    ``day``, keyed by ISO half-hour slot start (UTC). ``column`` must be one of
    ``grid_export_kw`` / ``grid_import_kw`` / ``solar_power_kw`` /
    ``battery_discharge_kw`` / ``load_power_kw`` (caller-vetted).

    Each sample-pair's energy is assigned to the bucket containing the EARLIER
    sample (matching :func:`compute_fox_energy_daily_from_realtime`). The
    ``max_gap_seconds`` cap means a telemetry outage doesn't smear a stale value
    across the gap. Slots with no telemetry get no key — caller decides whether
    to treat missing as zero or fall back to a daily total.
    """
    from collections import defaultdict
    from datetime import datetime as _dt

    if column not in ("grid_export_kw", "grid_import_kw", "solar_power_kw", "battery_discharge_kw", "load_power_kw"):
        raise ValueError(f"unsupported column: {column}")

    day_iso = day.isoformat()
    # Pull samples for the day plus one before/after for boundary integration.
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                f"""SELECT captured_at, {column}
                   FROM pv_realtime_history
                   WHERE substr(captured_at, 1, 10) BETWEEN ? AND ?
                   ORDER BY captured_at""",
                (
                    (day - timedelta(days=1)).isoformat(),
                    (day + timedelta(days=1)).isoformat(),
                ),
            )
            rows_raw = cur.fetchall()
        finally:
            conn.close()

    if len(rows_raw) < 2:
        return {}

    def _parse(ts_raw: str) -> _dt | None:
        try:
            ts = _dt.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
            return ts if ts.tzinfo else ts.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            return None

    def _slot_key(ts: _dt) -> str:
        # Floor to 30-min boundary (UTC).
        floor_min = (ts.minute // 30) * 30
        slot = ts.replace(minute=floor_min, second=0, microsecond=0)
        return slot.astimezone(UTC).isoformat().replace("+00:00", "Z")

    buckets: dict[str, float] = defaultdict(float)
    prev_ts: _dt | None = None
    prev_val: float = 0.0
    for row in rows_raw:
        ts = _parse(row[0])
        if ts is None:
            continue
        cur_val = float(row[1]) if row[1] is not None else 0.0
        if prev_ts is not None:
            dt_s = min((ts - prev_ts).total_seconds(), max_gap_seconds)
            if dt_s > 0:
                hours = dt_s / 3600.0
                avg_kw = (prev_val + cur_val) / 2.0
                # Only count pairs whose earlier sample falls on `day`.
                if prev_ts.date().isoformat() == day_iso:
                    buckets[_slot_key(prev_ts)] += avg_kw * hours
        prev_ts = ts
        prev_val = cur_val

    return dict(buckets)


def half_hourly_grid_export_kwh_for_day(
    day: date,
    *,
    max_gap_seconds: int = 1800,
) -> dict[str, float]:
    """Per-slot grid-export kWh for ``day``, keyed by ISO half-hour slot start (UTC).

    Trapezoidal integration of ``pv_realtime_history.grid_export_kw``. See
    :func:`_half_hourly_grid_kwh_for_day` for mechanics.

    Used by :mod:`src.analytics.pnl` to value realised exports against per-slot
    Octopus Outgoing rates (issue #207).
    """
    return _half_hourly_grid_kwh_for_day(day, "grid_export_kw", max_gap_seconds=max_gap_seconds)


def half_hourly_solar_kwh_for_day(
    day: date,
    *,
    max_gap_seconds: int = 1800,
) -> dict[str, float]:
    """Per-slot realised solar (PV generation) kWh for ``day``, keyed by ISO
    half-hour slot start (UTC).

    Trapezoidal integration of ``pv_realtime_history.solar_power_kw``. See
    :func:`_half_hourly_grid_kwh_for_day` for mechanics. Powers the
    ``GET /api/v1/pv/today`` planned-vs-realised overlay.
    """
    return _half_hourly_grid_kwh_for_day(day, "solar_power_kw", max_gap_seconds=max_gap_seconds)


def half_hourly_grid_import_kwh_for_day(
    day: date,
    *,
    max_gap_seconds: int = 1800,
) -> dict[str, float]:
    """Per-slot grid-import kWh for ``day``, keyed by ISO half-hour slot start (UTC).

    Trapezoidal integration of ``pv_realtime_history.grid_import_kw``. Used by
    :mod:`src.analytics.pnl` to compute the *real* net cost — billing
    measured grid import (not household load) at per-slot Agile rates.

    Issue #306: prior to this helper, ``compute_daily_pnl`` multiplied
    ``execution_log.consumption_kwh`` (= load × Agile) which inflated absolute
    £ figures ~3-4× because PV + battery self-supply was treated as if grid-
    bought. The V13 nightly Octopus backfill was supposed to overwrite the
    estimated rows with metered import but had been failing silently for
    weeks (Octopus deprecated ``order_by=asc``).
    """
    return _half_hourly_grid_kwh_for_day(day, "grid_import_kw", max_gap_seconds=max_gap_seconds)


def half_hourly_battery_discharge_kwh_for_day(
    day: date,
    *,
    max_gap_seconds: int = 1800,
) -> dict[str, float]:
    """Per-slot battery-DISCHARGE kWh for ``day`` (UTC half-hour slots).

    Trapezoidal integration of ``pv_realtime_history.battery_discharge_kw`` —
    how much the battery contributed to covering load each slot. Used by the
    Consumption "by source" view (solar self-use + battery + grid = load).
    """
    return _half_hourly_grid_kwh_for_day(day, "battery_discharge_kw", max_gap_seconds=max_gap_seconds)


def upsert_fox_energy_daily(rows: list[dict[str, Any]]) -> int:
    """Upsert Fox daily energy rows (dicts with date, solar_kwh, load_kwh, …).

    Returns number of rows inserted/updated.
    """
    if not rows:
        return 0
    now = datetime.now(UTC).isoformat()
    with _lock:
        conn = get_connection()
        try:
            n = 0
            for r in rows:
                conn.execute(
                    """INSERT INTO fox_energy_daily
                       (date, solar_kwh, load_kwh, import_kwh, export_kwh,
                        charge_kwh, discharge_kwh, fetched_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(date) DO UPDATE SET
                         solar_kwh=excluded.solar_kwh,
                         load_kwh=excluded.load_kwh,
                         import_kwh=excluded.import_kwh,
                         export_kwh=excluded.export_kwh,
                         charge_kwh=excluded.charge_kwh,
                         discharge_kwh=excluded.discharge_kwh,
                         fetched_at=excluded.fetched_at""",
                    (
                        r.get("date"),
                        r.get("solar_kwh"),
                        r.get("load_kwh"),
                        r.get("import_kwh"),
                        r.get("export_kwh"),
                        r.get("charge_kwh"),
                        r.get("discharge_kwh"),
                        now,
                    ),
                )
                n += 1
            conn.commit()
            return n
        finally:
            conn.close()


def get_fox_energy_daily(limit: int = 90) -> list[dict[str, Any]]:
    """Return recent fox_energy_daily rows (newest first)."""
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                "SELECT * FROM fox_energy_daily ORDER BY date DESC LIMIT ?", (limit,)
            )
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()


def upsert_daikin_consumption_daily(
    *,
    date: str,
    kwh_total: float | None,
    kwh_heating: float | None = None,
    kwh_dhw: float | None = None,
    cop_daily: float | None = None,
    source: str = "unknown",
) -> None:
    """Upsert one ``daikin_consumption_daily`` row. Idempotent.

    ``source`` is the provenance flag: ``onecta`` | ``telemetry_integral`` |
    ``unknown``. Lets the cockpit show "from cloud" vs "estimated locally".

    NULL never clobbers a known split: ``sync_daikin_daily`` (the on-demand
    insights path) upserts with ``kwh_dhw=None``/``cop_daily=None``, which
    used to overwrite the nightly rollup's real heating/DHW split — silently
    starving the DHW forecast auto-scale (#534) of its input. COALESCE keeps
    the best-known value per column.
    """
    now = datetime.now(UTC).isoformat()
    with _lock:
        conn = get_connection()
        try:
            conn.execute(
                """INSERT INTO daikin_consumption_daily
                   (date, kwh_total, kwh_heating, kwh_dhw, cop_daily, source, fetched_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(date) DO UPDATE SET
                     kwh_total=COALESCE(excluded.kwh_total, kwh_total),
                     kwh_heating=COALESCE(excluded.kwh_heating, kwh_heating),
                     kwh_dhw=COALESCE(excluded.kwh_dhw, kwh_dhw),
                     cop_daily=COALESCE(excluded.cop_daily, cop_daily),
                     source=excluded.source,
                     fetched_at=excluded.fetched_at""",
                (date, kwh_total, kwh_heating, kwh_dhw, cop_daily, source, now),
            )
            conn.commit()
        finally:
            conn.close()


def upsert_daikin_consumption_2hourly(
    *,
    date: str,
    bucket_idx: int,
    kwh_total: float | None,
    kwh_heating: float | None = None,
    kwh_dhw: float | None = None,
    source: str = "onecta",
) -> None:
    """Upsert one ``daikin_consumption_2hourly`` row (#238). Idempotent.

    Re-polling the same day overwrites the previous row, which is the
    correct semantics: future buckets arrive as later polls fetch the
    payload, and earlier-bucket values can also revise as Onecta's
    aggregator settles.
    """
    if not (0 <= int(bucket_idx) <= 11):
        raise ValueError(f"bucket_idx must be in [0, 11], got {bucket_idx}")
    now = datetime.now(UTC).isoformat()
    with _lock:
        conn = get_connection()
        try:
            conn.execute(
                """INSERT INTO daikin_consumption_2hourly
                   (date, bucket_idx, kwh_total, kwh_heating, kwh_dhw, source, fetched_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(date, bucket_idx) DO UPDATE SET
                     kwh_total=excluded.kwh_total,
                     kwh_heating=excluded.kwh_heating,
                     kwh_dhw=excluded.kwh_dhw,
                     source=excluded.source,
                     fetched_at=excluded.fetched_at""",
                (date, int(bucket_idx), kwh_total, kwh_heating, kwh_dhw, source, now),
            )
            conn.commit()
        finally:
            conn.close()


def get_nonzero_lwt_offset_windows(
    start_date: str, end_date: str
) -> list[tuple[str, str]]:
    """``(start_time, end_time)`` UTC-ISO pairs of every ``lwt_preheat`` action
    with a non-zero offset whose plan ``date`` falls in the inclusive range.

    Used to EXCLUDE HEM-commanded offset windows from measured-heating
    signals (#540): both the k_per_degc regression and the pre-heat demand
    gate must observe the firmware's NATURAL behaviour, not HEM's own echo —
    the June-2026 incident showed offsets waking the compressor and the
    calibration then learning from the heating they caused.

    Status filter covers the REAL lifecycle (pending → active →
    completed/failed; review H1 on #541 — 'in_flight' is never stored):
    'active' matters most, because a multi-hour window is live exactly when
    overlapping re-plans evaluate the gate. 'failed' is included
    conservatively (may have half-applied). 'pending' never fired → clean.

    The plan ``date`` left edge is padded by one day (review H2): a rolling
    plan written in the evening carries windows that SPILL past midnight
    under the previous plan_date; filtering strictly by date made those
    windows invisible to a lookback starting the next day, re-counting their
    heating as natural demand (every-other-day gate oscillation). The pad
    only over-fetches — actual exclusion is keyed off start/end timestamps.
    """
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                """SELECT start_time, end_time, params FROM action_schedule
                   WHERE device = 'daikin' AND action_type = 'lwt_preheat'
                     AND date >= date(?, '-1 day') AND date <= ?
                     AND status IN ('completed', 'active', 'failed')""",
                (start_date, end_date),
            )
            out: list[tuple[str, str]] = []
            for r in cur.fetchall():
                try:
                    off = json.loads(r["params"] or "{}").get("lwt_offset", 0)
                except (json.JSONDecodeError, TypeError):
                    off = 1  # unparseable → treat as contaminated (conservative)
                if off:
                    out.append((str(r["start_time"]), str(r["end_time"])))
            return out
        finally:
            conn.close()


def get_dhw_boost_windows(start_date: str, end_date: str) -> list[tuple[str, str]]:
    """``(start_time, end_time)`` UTC-ISO pairs of tank windows whose measured
    DHW energy is NOT the ordinary schedule's doing, for dhw_bucket_bias
    decontamination:

    * every ``tank_negative_boost`` action — deliberate max-heating the
      forecast budgets separately (the boost ramp in dhw_policy). ``skipped``
      is included: a skipped boost leaves boost-sized COMMITTED forecast
      against ordinary actuals, poisoning the ratio the other way.
    * any tank action a user manually overrode (``overridden_by_user_at``) —
      hand-set tank state produces energy the schedule never predicted.

    Same date-pad rationale as :func:`get_nonzero_lwt_offset_windows`.
    """
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                """SELECT start_time, end_time FROM action_schedule
                   WHERE device = 'daikin'
                     AND date >= date(?, '-1 day') AND date <= ?
                     AND (
                       (action_type = 'tank_negative_boost'
                        AND status IN ('completed', 'active', 'failed', 'skipped'))
                       OR (action_type LIKE 'tank_%'
                           AND overridden_by_user_at IS NOT NULL)
                     )""",
                (start_date, end_date),
            )
            return [(str(r["start_time"]), str(r["end_time"])) for r in cur.fetchall()]
        finally:
            conn.close()


def measured_space_heating_kwh_excluding_offset_windows(
    lookback_hours: int = 48,
) -> float:
    """Trailing measured space-heating kWh from ``daikin_consumption_2hourly``,
    EXCLUDING 2-hour buckets overlapped by a HEM ``lwt_preheat`` window.

    Powers the pre-heat demand gate (#540): without the exclusion the gate
    would feed on offset-induced heating and hold itself open forever. With
    it, June converges shut within a day or two of residual decay, while a
    real winter day still shows natural heating in the non-offset buckets.
    Bucket grid is local-date × 2 h (bucket_idx 0-11, hour//2).
    """
    tz = ZoneInfo(getattr(config, "BULLETPROOF_TIMEZONE", "Europe/London"))
    now_local = datetime.now(tz)
    start_local = now_local - timedelta(hours=lookback_hours)
    start_date = start_local.date().isoformat()
    end_date = now_local.date().isoformat()

    rows = get_daikin_consumption_2hourly_range(start_date, end_date)
    if not rows:
        # No 2-hourly split cached. The daily totals CANNOT be decontaminated
        # (no intra-day resolution), so falling back to them while offset
        # windows exist would count HEM-induced heating as natural demand and
        # latch the gate open exactly when its primary signal is broken
        # (review M1 on #541) — return 0 (gate-closed direction) instead.
        # With no offsets in range, the daily totals are clean and usable.
        if get_nonzero_lwt_offset_windows(start_date, end_date):
            logger.warning(
                "measured_space_heating: 2-hourly split missing %s..%s while "
                "offset windows exist — cannot decontaminate daily totals; "
                "returning 0 (demand-gate-closed direction)",
                start_date, end_date,
            )
            return 0.0
        logger.warning(
            "measured_space_heating: 2-hourly split missing %s..%s — falling "
            "back to whole-day daily totals (coarser than the %dh lookback)",
            start_date, end_date, lookback_hours,
        )
        daily = get_daikin_consumption_daily_range(start_date, end_date)
        return float(sum(r.get("kwh_heating") or 0.0 for r in daily))

    # Thermal-lag tail: HEM-induced heat bleeds into the 2-h bucket(s) AFTER an
    # offset window closes (the live June self-loop counted those as natural
    # demand and latched the gate open). Exclude this many trailing buckets too.
    tail_buckets = max(0, int(getattr(config, "DAIKIN_LWT_PREHEAT_DECONTAM_TAIL_BUCKETS", 1)))
    excluded: set[tuple[str, int]] = set()
    for s_iso, e_iso in get_nonzero_lwt_offset_windows(start_date, end_date):
        try:
            s = datetime.fromisoformat(s_iso.replace("Z", "+00:00")).astimezone(tz)
            e = datetime.fromisoformat(e_iso.replace("Z", "+00:00")).astimezone(tz)
        except (ValueError, TypeError):
            continue
        e_padded = e + timedelta(hours=2 * tail_buckets)
        cur = s.replace(minute=0, second=0, microsecond=0)
        cur = cur.replace(hour=(cur.hour // 2) * 2)
        while cur < e_padded:
            excluded.add((cur.date().isoformat(), cur.hour // 2))
            cur += timedelta(hours=2)

    total = 0.0
    for r in rows:
        # Only buckets inside the trailing window count.
        bucket_start = datetime.combine(
            date.fromisoformat(str(r["date"])),
            datetime.min.time(),
            tzinfo=tz,
        ) + timedelta(hours=2 * int(r["bucket_idx"]))
        if bucket_start < start_local or bucket_start >= now_local:
            continue
        if (str(r["date"]), int(r["bucket_idx"])) in excluded:
            continue
        total += float(r.get("kwh_heating") or 0.0)
    return total


def get_daikin_consumption_2hourly_range(
    start_date: str, end_date: str
) -> list[dict[str, Any]]:
    """All 2-hourly Daikin consumption rows in ``[start_date, end_date]`` inclusive,
    ordered by (date ASC, bucket_idx ASC)."""
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                "SELECT * FROM daikin_consumption_2hourly "
                "WHERE date >= ? AND date <= ? "
                "ORDER BY date ASC, bucket_idx ASC",
                (start_date, end_date),
            )
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()


def get_daikin_consumption_daily_by_date(date_str: str) -> dict[str, Any] | None:
    """Single-day Daikin consumption row, or None if not cached."""
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                "SELECT * FROM daikin_consumption_daily WHERE date = ?", (date_str,)
            )
            r = cur.fetchone()
            return dict(r) if r else None
        finally:
            conn.close()


def get_daikin_consumption_daily_range(start_date: str, end_date: str) -> list[dict[str, Any]]:
    """All Daikin consumption rows in ``[start_date, end_date]`` inclusive, ASC."""
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                "SELECT * FROM daikin_consumption_daily "
                "WHERE date >= ? AND date <= ? ORDER BY date ASC",
                (start_date, end_date),
            )
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()


def get_fox_energy_daily_by_date(date_str: str) -> dict[str, Any] | None:
    """Single-day Fox energy row, or None when not cached.

    ``date_str`` is ISO ``YYYY-MM-DD``. Returned dict carries ``fetched_at``
    so callers can decide if a refresh is warranted.
    """
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                "SELECT * FROM fox_energy_daily WHERE date = ?", (date_str,)
            )
            r = cur.fetchone()
            return dict(r) if r else None
        finally:
            conn.close()


def get_fox_energy_daily_range(start_date: str, end_date: str) -> list[dict[str, Any]]:
    """All Fox energy rows within ``[start_date, end_date]`` inclusive, ordered ASC.

    Both bounds are ISO ``YYYY-MM-DD`` strings; SQLite compares them as text
    which works because the column is normalised that way.
    """
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                "SELECT * FROM fox_energy_daily WHERE date >= ? AND date <= ? "
                "ORDER BY date ASC",
                (start_date, end_date),
            )
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()


def upsert_octopus_daily_meter(
    date_str: str,
    *,
    import_kwh: float | None,
    export_kwh: float | None,
) -> None:
    """Cache Octopus smart-meter daily totals (one row per local date).

    Both kWh args are nullable — export is None for households without an
    Outgoing tariff. ``fetched_at`` is set to UTC now on every call so
    callers can detect stale rows.
    """
    now = datetime.now(UTC).isoformat()
    with _lock:
        conn = get_connection()
        try:
            conn.execute(
                """INSERT INTO octopus_daily_meter
                   (date, import_kwh, export_kwh, fetched_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(date) DO UPDATE SET
                     import_kwh = excluded.import_kwh,
                     export_kwh = excluded.export_kwh,
                     fetched_at = excluded.fetched_at""",
                (date_str, import_kwh, export_kwh, now),
            )
            conn.commit()
        finally:
            conn.close()


def get_octopus_daily_meter(date_str: str) -> dict[str, Any] | None:
    """Cached Octopus smart-meter daily totals for ``date_str`` or None.

    Returns ``{"date", "import_kwh", "export_kwh", "fetched_at"}``. The brief
    surfaces this side-by-side with ``fox_energy_daily`` for divergence audit.
    """
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                "SELECT * FROM octopus_daily_meter WHERE date = ?", (date_str,)
            )
            r = cur.fetchone()
            return dict(r) if r else None
        finally:
            conn.close()


def get_octopus_meter_last_day() -> str | None:
    """Most recent local date with a PLAUSIBLE cached Octopus daily-meter
    row (import ≥ 0.5 kWh), or None.

    Drives the brief's meter-staleness warning (#533): when this lags today
    by more than ``CONSUMPTION_METER_STALE_DAYS`` the PnL has silently been
    running on Fox CT-clamp data alone. The plausibility filter matters:
    during a meter outage Octopus can yield NULL-import rows (empty fetch)
    or near-zero garbage rows (pre-floor legacy data, e.g. 2026-05-09..20) —
    counting those would advance MAX(date) daily and mute the alarm in
    exactly the failure mode it exists for. 0.5 kWh mirrors
    PUBLISHED_FLOOR_KWH in the backfill.
    """
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                "SELECT MAX(date) AS d FROM octopus_daily_meter WHERE import_kwh >= 0.5"
            )
            r = cur.fetchone()
            return r["d"] if r and r["d"] else None
        finally:
            conn.close()


def count_metered_execution_slots(start_utc_iso: str, end_utc_iso: str) -> int:
    """Count ``execution_log`` rows already rewritten to metered truth in
    ``[start, end)``. Includes ``metered_synthetic`` (issue #199 fallback
    rows) — both mean Octopus data landed for the slot. Used by the backfill
    sweep (#533) to decide whether a past day still needs a re-attempt.
    """
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                """SELECT COUNT(*) AS n FROM execution_log
                   WHERE timestamp >= ? AND timestamp < ?
                     AND source LIKE 'metered%'""",
                (start_utc_iso, end_utc_iso),
            )
            return int(cur.fetchone()["n"])
        finally:
            conn.close()


def get_fox_energy_dates_for_month(year: int, month: int) -> set[str]:
    """Set of ``YYYY-MM-DD`` already cached for the given calendar month.

    Used by the read-through Fox cache to compute the missing days that
    actually need a cloud fetch.
    """
    prefix = f"{year:04d}-{month:02d}-"
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                "SELECT date FROM fox_energy_daily WHERE date LIKE ?",
                (f"{prefix}%",),
            )
            return {r["date"] for r in cur.fetchall()}
        finally:
            conn.close()


def save_indoor_readings(readings: list[dict[str, Any]]) -> int:
    """Batch-insert indoor temperature readings (#540 W1). Each dict:
    ``{captured_at, temp_c, room?, source?, quality?}``. Idempotent on
    (captured_at, room) so re-pushes don't double-count. Returns rows written.

    ``captured_at`` is normalised to canonical UTC Z form so a getter keyed on
    ISO string ordering is correct regardless of the pusher's offset format.
    """
    written = 0
    with _lock:
        conn = get_connection()
        try:
            for r in readings:
                ts = _parse_iso_utc(r.get("captured_at"))
                temp = r.get("temp_c")
                if ts is None or temp is None:
                    continue
                key = ts.strftime("%Y-%m-%dT%H:%M:%SZ")
                cur = conn.execute(
                    """INSERT OR IGNORE INTO room_temperature_history
                       (captured_at, room, temp_c, source, quality)
                       VALUES (?, ?, ?, ?, ?)""",
                    (key, str(r.get("room") or "home"), float(temp),
                     r.get("source"), r.get("quality")),
                )
                written += cur.rowcount
            conn.commit()
        finally:
            conn.close()
    return written


def get_latest_indoor_reading(max_age_minutes: int = 30) -> dict[str, Any] | None:
    """Freshest house indoor temperature (#540 W1), or None when no reading is
    within ``max_age_minutes`` (→ caller falls back to the estimator).

    Multi-room aware: returns the MEAN of the latest reading per room (within the
    staleness window), plus the newest ``captured_at`` and the room list, so the
    comfort guard / LP get one representative house temperature."""
    cutoff = (datetime.now(UTC) - timedelta(minutes=max_age_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                """SELECT room, temp_c, captured_at FROM room_temperature_history r
                   WHERE captured_at >= ?
                     AND captured_at = (
                       SELECT MAX(captured_at) FROM room_temperature_history
                       WHERE room = r.room AND captured_at >= ?)""",
                (cutoff, cutoff),
            )
            rows = cur.fetchall()
        finally:
            conn.close()
    if not rows:
        return None
    temps = [float(r["temp_c"]) for r in rows]
    return {
        "temp_c": round(sum(temps) / len(temps), 2),
        "captured_at": max(str(r["captured_at"]) for r in rows),
        "rooms": sorted({str(r["room"]) for r in rows}),
        "n_rooms": len(rows),
    }


def get_indoor_readings_range(start_utc_iso: str, end_utc_iso: str) -> list[dict[str, Any]]:
    """Per-reading rows in ``[start, end)`` (all rooms), oldest first. For the
    cockpit chart + the W2 thermal learner."""
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                """SELECT captured_at, room, temp_c, source, quality
                   FROM room_temperature_history
                   WHERE captured_at >= ? AND captured_at < ?
                   ORDER BY captured_at""",
                (start_utc_iso, end_utc_iso),
            )
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()


def _device_key(r: dict[str, Any]) -> str:
    """Stable per-device identity for the log: prefer MAC (globally unique +
    stable across DHCP/rename), then device_id, then source, then room."""
    for k in ("mac", "device_id", "source", "room"):
        v = r.get(k)
        if v is not None and str(v).strip():
            return str(v).strip()
    return "unknown"


# Per-reading serialized payload ceiling. Legitimate sensor readings are a few
# hundred bytes; this only trips on abuse (the ingest token is on an internet-
# exposed device, #646) — extras are then dropped, known fields kept, so one
# bad device can't amplify storage without bound.
_DEVICE_LOG_MAX_PAYLOAD_BYTES = 4096

_DEVICE_LOG_KNOWN_FIELDS = (
    "captured_at", "temp_c", "humidity_pct", "pressure_hpa",
    "room", "source", "device_id", "mac", "quality",
)


def _finite_or_none(v: Any) -> float | None:
    """Coerce to float, dropping non-finite (NaN/Inf) — those serialize as the
    invalid-JSON token ``NaN`` and would poison every reader of the log."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _sanitize_payload(r: dict[str, Any]) -> dict[str, Any]:
    """A JSON-safe copy of a raw reading: non-finite floats → None (so the
    dumped payload is always valid JSON), and if the whole thing is oversized,
    keep only the known fields + a ``_truncated`` marker."""
    clean: dict[str, Any] = {}
    for k, v in r.items():
        if isinstance(v, float) and not math.isfinite(v):
            clean[k] = None
        else:
            clean[k] = v
    blob = json.dumps(clean, default=str, sort_keys=True)
    if len(blob.encode("utf-8")) > _DEVICE_LOG_MAX_PAYLOAD_BYTES:
        clean = {k: clean.get(k) for k in _DEVICE_LOG_KNOWN_FIELDS if k in clean}
        clean["_truncated"] = True
    return clean


def save_device_reading_log(readings: list[dict[str, Any]]) -> int:
    """Lossless per-device log of EVERYTHING a sensor sends (#540 W1c). Typed
    columns for the known metrics (temp/humidity/pressure/mac/device_id) plus
    ``payload_json`` holding the full raw reading, so any extra field survives
    with no migration. Idempotent on (device_key, dedup_key) where dedup_key is
    the device timestamp (or the server clock when the device sent none) — so a
    retry doesn't duplicate even for a timestamp-less device. ``received_at`` is
    always the server clock. Returns rows written."""
    written = 0
    received = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _lock:
        conn = get_connection()
        try:
            for r in readings:
                ts = _parse_iso_utc(r.get("captured_at"))
                captured = ts.strftime("%Y-%m-%dT%H:%M:%SZ") if ts else None
                dedup = captured or received  # NEVER NULL → the PK actually dedups
                dev = _device_key(r)
                payload = _sanitize_payload(r)
                cur = conn.execute(
                    """INSERT OR IGNORE INTO device_reading_log
                       (received_at, captured_at, dedup_key, device_key, device_id,
                        mac, room, source, temp_c, humidity_pct, pressure_hpa, payload_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        received,
                        captured,
                        dedup,
                        dev,
                        (str(r["device_id"]).strip() if r.get("device_id") else None),
                        (str(r["mac"]).strip() if r.get("mac") else None),
                        (str(r["room"]) if r.get("room") else None),
                        (str(r["source"]) if r.get("source") else None),
                        _finite_or_none(r.get("temp_c")) if r.get("temp_c") is not None else None,
                        _finite_or_none(r.get("humidity_pct")) if r.get("humidity_pct") is not None else None,
                        _finite_or_none(r.get("pressure_hpa")) if r.get("pressure_hpa") is not None else None,
                        json.dumps(payload, default=str, sort_keys=True),
                    ),
                )
                written += cur.rowcount
            conn.commit()
        finally:
            conn.close()
    return written


def get_device_reading_log(
    device_key: str | None = None, hours: int = 24, limit: int = 5000
) -> list[dict[str, Any]]:
    """Recent raw device-log rows, newest first. Optionally filter to one
    device_key. ``payload_json`` is parsed back into a ``payload`` dict."""
    start = (datetime.now(UTC) - timedelta(hours=max(1, hours))).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _lock:
        conn = get_connection()
        try:
            # Newest by capture time (received_at ties within a batch); the
            # device timestamp is the honest ordering, server clock the fallback.
            if device_key:
                cur = conn.execute(
                    """SELECT * FROM device_reading_log
                       WHERE received_at >= ? AND device_key = ?
                       ORDER BY COALESCE(captured_at, received_at) DESC, received_at DESC
                       LIMIT ?""",
                    (start, device_key, int(limit)),
                )
            else:
                cur = conn.execute(
                    """SELECT * FROM device_reading_log
                       WHERE received_at >= ?
                       ORDER BY COALESCE(captured_at, received_at) DESC, received_at DESC
                       LIMIT ?""",
                    (start, int(limit)),
                )
            rows = []
            for r in cur.fetchall():
                d = dict(r)
                d.pop("dedup_key", None)   # internal dedup column, not for callers
                raw = d.pop("payload_json", None)
                try:
                    d["payload"] = json.loads(raw) if raw else None
                except (TypeError, ValueError):
                    d["payload"] = None
                rows.append(d)
            return rows
        finally:
            conn.close()


def list_sensor_devices() -> list[dict[str, Any]]:
    """One row per device ever seen: identity + last-seen + count + the latest
    value of each known metric. Powers a 'devices' overview (viewer).

    ``room``/``device_id``/``source`` are mutable labels — a device can be
    renamed in the ESPHome UI at runtime, and the same physical sensor then
    reports under a new ``room``. They therefore come from the device's LATEST
    reading, not ``MAX()``: an aggregate picks the alphabetically-largest label
    the device EVER sent, which froze the card at a retired name (a sensor
    renamed ``sala`` → ``corredor`` still displayed "sala", because 's' > 'c').

    "Latest" is ordered by ``received_at`` (server clock), NOT by the
    device-supplied ``captured_at``: a node whose SNTP hasn't synced can post a
    reading dated years ahead, and ordering on that would pin the card to
    whatever label it carried FOREVER — the same frozen-label bug, just with a
    rarer trigger. Ties (a batch shares one ``received_at``) fall through to
    ``captured_at`` so the freshest capture within a batch still wins.

    ``mac`` is aggregated only because it is degenerate within a group: when a
    device reports a MAC it IS the ``device_key`` (see ``_device_key``), so every
    row in the group carries the same one; when it doesn't, every row is NULL.
    CAVEAT: a device that sends NO mac is keyed on ``device_id``/``room``
    instead, so renaming THAT still splits it into a second group (and a second
    card). Only MAC-bearing devices are rename-proof.
    """
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                """SELECT device_key,
                          MAX(mac)          AS mac,
                          COUNT(*)          AS n_readings,
                          MIN(received_at)  AS first_seen,
                          MAX(received_at)  AS last_seen
                   FROM device_reading_log
                   GROUP BY device_key
                   ORDER BY last_seen DESC"""
            )
            devices = [dict(r) for r in cur.fetchall()]
            # Attach the latest reading's labels + metrics per device (freshest row).
            for d in devices:
                latest = conn.execute(
                    """SELECT device_id, room, source,
                              temp_c, humidity_pct, pressure_hpa, captured_at, received_at
                       FROM device_reading_log WHERE device_key = ?
                       ORDER BY received_at DESC, COALESCE(captured_at, received_at) DESC
                       LIMIT 1""",
                    (d["device_key"],),
                ).fetchone()
                # device_key came from a GROUP BY over this same table on this
                # same connection, so a row always exists.
                row = dict(latest)
                d["device_id"] = row["device_id"]
                d["room"] = row["room"]
                d["source"] = row["source"]
                d["latest"] = {
                    k: row[k]
                    for k in ("temp_c", "humidity_pct", "pressure_hpa",
                              "captured_at", "received_at")
                }
            return devices
        finally:
            conn.close()


def get_indoor_summary(stale_minutes: int = 30, lookback_hours: int = 24) -> dict[str, Any]:
    """Compact latest-per-room indoor snapshot for the cockpit's consolidated
    /cockpit/now read (#540 W1). ONE window-function query → the freshest row per
    device within the lookback; freshness is computed per row (age > stale_minutes
    → stale). The mean is over FRESH rooms only (matches the LP). Cheap enough to
    ride the 20 s cockpit poll — it returns one row per device, not the history."""
    now = datetime.now(UTC)
    cutoff = (now - timedelta(hours=max(1, lookback_hours))).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                """SELECT room, device_key, temp_c, humidity_pct, captured_at, received_at
                   FROM (
                     SELECT room, device_key, temp_c, humidity_pct, captured_at, received_at,
                            ROW_NUMBER() OVER (
                              PARTITION BY device_key
                              ORDER BY COALESCE(captured_at, received_at) DESC, received_at DESC
                            ) AS rn
                     FROM device_reading_log
                     WHERE received_at >= ?
                   ) WHERE rn = 1""",
                (cutoff,),
            )
            latest = [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

    rooms: list[dict[str, Any]] = []
    fresh_temps: list[float] = []
    fresh_hums: list[float] = []
    newest_cap: str | None = None
    newest_rec: str | None = None
    for r in latest:
        ref = r.get("captured_at") or r.get("received_at")
        t = _parse_iso_utc(ref) if ref else None
        age_min = (now - t).total_seconds() / 60.0 if t is not None else None
        stale = age_min is None or age_min > stale_minutes
        rooms.append({
            "room": r.get("room") or r.get("device_key"),
            "temp_c": r.get("temp_c"),
            "humidity_pct": r.get("humidity_pct"),
            "stale": stale,
            "age_min": round(age_min, 1) if age_min is not None else None,
        })
        if not stale and r.get("temp_c") is not None:
            fresh_temps.append(float(r["temp_c"]))
            if r.get("humidity_pct") is not None:
                fresh_hums.append(float(r["humidity_pct"]))
        if r.get("received_at") and (newest_rec is None or r["received_at"] > newest_rec):
            newest_rec = r["received_at"]
        if r.get("captured_at") and (newest_cap is None or r["captured_at"] > newest_cap):
            newest_cap = r["captured_at"]

    rooms.sort(key=lambda x: (x["room"] or ""))
    mean_c = sum(fresh_temps) / len(fresh_temps) if fresh_temps else None
    hum = sum(fresh_hums) / len(fresh_hums) if fresh_hums else None
    return {
        "mean_c": round(mean_c, 1) if mean_c is not None else None,
        "humidity_pct": round(hum, 1) if hum is not None else None,
        "n_rooms": len(rooms),
        "n_fresh": len(fresh_temps),
        "stale": len(fresh_temps) == 0,
        "newest_captured_at": newest_cap,
        "newest_received_at": newest_rec,
        "rooms": rooms,
    }


def mean_fox_load_kwh_per_slot(limit: int = 60) -> float | None:
    """Mean half-hourly load kWh from Fox daily data (load_kwh / 48).

    Returns None when no Fox data is available.
    """
    rows = get_fox_energy_daily(limit=limit)
    vals = [float(r["load_kwh"]) / 48.0 for r in rows if r.get("load_kwh") and r["load_kwh"] > 0]
    if not vals:
        return None
    return sum(vals) / len(vals)


# ---------------------------------------------------------------------------
# V4: Fox ESS realtime snapshot (SoC, solar_power_kw, load_power_kw)
# Used by MPC intra-day re-optimisation to seed the LP initial state.
# ---------------------------------------------------------------------------

def save_pv_realtime_sample(
    captured_at: str,
    *,
    solar_power_kw: float | None = None,
    soc_pct: float | None = None,
    load_power_kw: float | None = None,
    grid_import_kw: float | None = None,
    grid_export_kw: float | None = None,
    battery_charge_kw: float | None = None,
    battery_discharge_kw: float | None = None,
    source: str = "heartbeat",
) -> bool:
    """Append a PV/load/SoC sample to ``pv_realtime_history``.

    Idempotent: ``INSERT OR IGNORE`` on the ``captured_at`` PRIMARY KEY so re-runs
    of the CSV backfill or duplicate heartbeat ticks don't double-count.
    Returns True if a row was inserted, False if it was a duplicate.
    """
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                """INSERT OR IGNORE INTO pv_realtime_history
                   (captured_at, solar_power_kw, soc_pct, load_power_kw,
                    grid_import_kw, grid_export_kw, battery_charge_kw,
                    battery_discharge_kw, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    captured_at,
                    solar_power_kw,
                    soc_pct,
                    load_power_kw,
                    grid_import_kw,
                    grid_export_kw,
                    battery_charge_kw,
                    battery_discharge_kw,
                    source,
                ),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


def add_presence_period(
    start_utc: str,
    end_utc: str,
    kind: str,
    note: str | None = None,
) -> int:
    """Insert a manual presence flag (home / travel / guests). Returns the new id."""
    if kind not in ("home", "travel", "guests"):
        raise ValueError(f"invalid presence kind {kind!r}")
    from datetime import UTC as _UTC, datetime as _dt
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                """INSERT INTO presence_periods (start_utc, end_utc, kind, note, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (start_utc, end_utc, kind, note, _dt.now(_UTC).isoformat()),
            )
            conn.commit()
            return int(cur.lastrowid or 0)
        finally:
            conn.close()


def get_presence_at(when_utc: str) -> str:
    """Return the presence kind covering ``when_utc`` (most recent if overlapping). 'home' if none matches."""
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                """SELECT kind FROM presence_periods
                   WHERE start_utc <= ? AND end_utc >= ?
                   ORDER BY id DESC LIMIT 1""",
                (when_utc, when_utc),
            )
            row = cur.fetchone()
            return str(row[0]) if row else "home"
        finally:
            conn.close()


def upsert_fox_realtime_snapshot(snap: dict[str, Any]) -> None:
    """Insert or replace the single realtime snapshot row (id=1).

    Expected keys: captured_at (ISO str), soc_pct (float), solar_power_kw (float|None),
    load_power_kw (float|None).
    """
    with _lock:
        conn = get_connection()
        try:
            conn.execute(
                """INSERT INTO fox_realtime_snapshot
                   (id, captured_at, soc_pct, solar_power_kw, load_power_kw)
                   VALUES (1, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                     captured_at=excluded.captured_at,
                     soc_pct=excluded.soc_pct,
                     solar_power_kw=excluded.solar_power_kw,
                     load_power_kw=excluded.load_power_kw""",
                (
                    snap.get("captured_at"),
                    snap.get("soc_pct"),
                    snap.get("solar_power_kw"),
                    snap.get("load_power_kw"),
                ),
            )
            conn.commit()
        finally:
            conn.close()


def get_fox_realtime_snapshot() -> dict[str, Any] | None:
    """Return the most recent Fox realtime snapshot, or None if not available.

    The snapshot is considered stale if captured more than 15 minutes ago.
    """
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute("SELECT * FROM fox_realtime_snapshot WHERE id = 1")
            r = cur.fetchone()
            if not r:
                return None
            snap = dict(r)
            # Staleness check: reject if older than 15 min
            try:
                captured = datetime.fromisoformat(str(snap["captured_at"]).replace("Z", "+00:00"))
                if captured.tzinfo is None:
                    captured = captured.replace(tzinfo=UTC)
                age_s = (datetime.now(UTC) - captured).total_seconds()
                if age_s > 900:
                    return None
            except (ValueError, TypeError, OSError) as exc:
                logger.debug("fox_realtime_snapshot staleness check skipped: %s", exc)
                return None
            return snap
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# V6: notification_routes — per-AlertType routing, runtime-editable via MCP
# ---------------------------------------------------------------------------

def get_notification_route(alert_type: str) -> dict[str, Any] | None:
    """Return the notification_routes row for *alert_type*, or None if not found."""
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                "SELECT * FROM notification_routes WHERE alert_type = ?", (alert_type,)
            )
            r = cur.fetchone()
            return dict(r) if r else None
        finally:
            conn.close()


def list_notification_routes() -> list[dict[str, Any]]:
    """Return all notification_routes rows ordered by severity then alert_type."""
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                "SELECT * FROM notification_routes ORDER BY severity, alert_type"
            )
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()


def upsert_notification_route(
    alert_type: str,
    *,
    enabled: bool | None = None,
    severity: str | None = None,
    target_override: str | None = None,
    channel_override: str | None = None,
    silent: bool | None = None,
    clear_target_override: bool = False,
    clear_channel_override: bool = False,
) -> None:
    """Create or update a notification_routes row.

    Only the supplied keyword arguments are written; others are left as-is on
    an existing row, or default-seeded on a new row.
    """
    import time
    now = time.time()
    with _lock:
        conn = get_connection()
        try:
            existing = conn.execute(
                "SELECT * FROM notification_routes WHERE alert_type = ?", (alert_type,)
            ).fetchone()
            if existing:
                parts = ["updated_at = ?"]
                args: list[Any] = [now]
                if enabled is not None:
                    parts.append("enabled = ?")
                    args.append(1 if enabled else 0)
                if severity is not None:
                    parts.append("severity = ?")
                    args.append(severity)
                if target_override is not None:
                    parts.append("target_override = ?")
                    args.append(target_override)
                if clear_target_override:
                    parts.append("target_override = NULL")
                if channel_override is not None:
                    parts.append("channel_override = ?")
                    args.append(channel_override)
                if clear_channel_override:
                    parts.append("channel_override = NULL")
                if silent is not None:
                    parts.append("silent = ?")
                    args.append(1 if silent else 0)
                args.append(alert_type)
                conn.execute(
                    f"UPDATE notification_routes SET {', '.join(parts)} WHERE alert_type = ?",
                    args,
                )
            else:
                conn.execute(
                    """INSERT INTO notification_routes
                       (alert_type, enabled, severity, target_override, channel_override, silent, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        alert_type,
                        1 if (enabled is not False) else 0,
                        severity or "reports",
                        target_override,
                        channel_override,
                        1 if silent else 0,
                        now,
                    ),
                )
            conn.commit()
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# V7: plan_consent — user-approval gate for proposed plans
# ---------------------------------------------------------------------------

def upsert_plan_consent(
    plan_id: str,
    plan_date: str,
    summary: str,
    expires_at: float,
    plan_hash: str | None = None,
) -> None:
    """Insert or replace a plan_consent row with status=pending_approval."""
    import time
    now = time.time()
    with _lock:
        conn = get_connection()
        try:
            conn.execute(
                """INSERT INTO plan_consent
                   (plan_id, plan_date, status, proposed_at, expires_at, summary, plan_hash, created_at)
                   VALUES (?, ?, 'pending_approval', ?, ?, ?, ?, ?)
                   ON CONFLICT(plan_id) DO UPDATE SET
                     plan_date=excluded.plan_date,
                     status='pending_approval',
                     proposed_at=excluded.proposed_at,
                     approved_at=NULL,
                     rejected_at=NULL,
                     expires_at=excluded.expires_at,
                     summary=excluded.summary,
                     plan_hash=excluded.plan_hash""",
                (plan_id, plan_date, now, expires_at, summary, plan_hash, now),
            )
            conn.commit()
        finally:
            conn.close()


def get_plan_consent(plan_date: str) -> dict[str, Any] | None:
    """Return the most recent plan_consent row for *plan_date*, or None."""
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                """SELECT * FROM plan_consent WHERE plan_date = ?
                   ORDER BY proposed_at DESC LIMIT 1""",
                (plan_date,),
            )
            r = cur.fetchone()
            return dict(r) if r else None
        finally:
            conn.close()


def mark_plan_notified(plan_id: str, ts: float | None = None) -> bool:
    """Record that a PLAN_PROPOSED hook was delivered for *plan_id*.

    Updates `plan_consent.last_notified_at`, consulted by the debounce in
    `_write_plan_consent`. Returns True if a row was updated.
    """
    import time
    now = ts if ts is not None else time.time()
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                "UPDATE plan_consent SET last_notified_at=? WHERE plan_id=?",
                (now, plan_id),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


def approve_plan(plan_id: str) -> bool:
    """Set plan_consent status to approved. Returns True if a row was updated."""
    import time
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                """UPDATE plan_consent
                   SET status='approved', approved_at=?
                   WHERE plan_id=? AND status='pending_approval'""",
                (time.time(), plan_id),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


def reject_plan(plan_id: str) -> bool:
    """Set plan_consent status to rejected and delete pending action_schedule rows.

    Returns True if a row was updated.
    """
    import time
    with _lock:
        conn = get_connection()
        try:
            row = conn.execute(
                "SELECT plan_date FROM plan_consent WHERE plan_id=?", (plan_id,)
            ).fetchone()
            if row:
                plan_date = row["plan_date"]
                conn.execute(
                    "DELETE FROM action_schedule WHERE date=? AND status='pending'",
                    (plan_date,),
                )
            cur = conn.execute(
                """UPDATE plan_consent
                   SET status='rejected', rejected_at=?
                   WHERE plan_id=? AND status='pending_approval'""",
                (time.time(), plan_id),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# V9: daikin_telemetry — physics-estimator seed + live-fetch audit trail (#55)
# ---------------------------------------------------------------------------


def insert_daikin_telemetry(row: dict[str, Any]) -> None:
    """Insert a Daikin telemetry row (``source='live'`` or ``'estimate'``).

    Defaults ``fetched_at`` to "now" when not supplied. Missing numeric fields
    become NULL — the table's seed row for the estimator walk is the most
    recent ``source='live'`` entry, so only the tank/indoor/outdoor temps need
    to be populated for physics to work.
    """
    with _lock:
        conn = get_connection()
        try:
            conn.execute(
                """INSERT INTO daikin_telemetry
                   (fetched_at, source, tank_temp_c, indoor_temp_c, outdoor_temp_c,
                    tank_target_c, lwt_actual_c, mode, weather_regulation)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    float(row.get("fetched_at") or datetime.now(UTC).timestamp()),
                    str(row.get("source", "live")),
                    row.get("tank_temp_c"),
                    row.get("indoor_temp_c"),
                    row.get("outdoor_temp_c"),
                    row.get("tank_target_c"),
                    row.get("lwt_actual_c"),
                    row.get("mode"),
                    int(row["weather_regulation"])
                    if row.get("weather_regulation") is not None
                    else None,
                ),
            )
            conn.commit()
        finally:
            conn.close()


def insert_dhw_shadow(row: dict[str, Any]) -> None:
    """Record one LP-owned economic shadow solve (#714)."""
    with _lock:
        conn = get_connection()
        try:
            conn.execute(
                """INSERT OR REPLACE INTO dhw_lp_shadow_log
                   (run_at_utc, day, cost_pinned_p, cost_lp_owned_p, delta_p,
                    comfort_deficit_c, horizon_days, terminal_credit_p,
                    e_dhw_fixed_kwh, e_dhw_lp_kwh, n_tank_rows)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (row["run_at_utc"], row["day"], row["cost_pinned_p"],
                 row["cost_lp_owned_p"], row["delta_p"], row["comfort_deficit_c"],
                 row.get("horizon_days"), row.get("terminal_credit_p"),
                 row.get("e_dhw_fixed_kwh"), row.get("e_dhw_lp_kwh"),
                 row.get("n_tank_rows")),
            )
            conn.commit()
        finally:
            conn.close()


def get_dhw_shadow_rows(since_day: str) -> list[dict[str, Any]]:
    """Shadow rows on or after ``since_day`` (local ISO date), oldest first."""
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                "SELECT * FROM dhw_lp_shadow_log WHERE day >= ? ORDER BY run_at_utc ASC",
                (since_day,),
            )
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()


def upsert_dhw_calibration(component: str, *, status: str, payload: dict[str, Any],
                           n_samples: int | None = None, r2: float | None = None,
                           window_days: int | None = None) -> None:
    """Store one component of the DHW calibration (#714). One row per component,
    replaced each run — a skipped fit overwrites with ``status='skipped'`` and the
    reader falls back to the databook, so a stale value can never silently steer."""
    with _lock:
        conn = get_connection()
        try:
            conn.execute(
                """INSERT INTO dhw_calibration
                   (component, fitted_at_utc, status, payload_json, n_samples, r2, window_days)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(component) DO UPDATE SET
                     fitted_at_utc=excluded.fitted_at_utc, status=excluded.status,
                     payload_json=excluded.payload_json, n_samples=excluded.n_samples,
                     r2=excluded.r2, window_days=excluded.window_days""",
                (component, datetime.now(UTC).isoformat(), status,
                 json.dumps(payload, default=str), n_samples, r2, window_days),
            )
            conn.commit()
        finally:
            conn.close()


def get_dhw_calibration(component: str) -> dict[str, Any] | None:
    """One DHW calibration component, or None if never fitted. ``payload`` is the
    parsed JSON; ``status``/``fitted_at_utc`` sit alongside it."""
    with _lock:
        conn = get_connection()
        try:
            r = conn.execute(
                "SELECT status, fitted_at_utc, payload_json, n_samples, r2, window_days "
                "FROM dhw_calibration WHERE component = ?", (component,)
            ).fetchone()
            if not r:
                return None
            try:
                payload = json.loads(r["payload_json"])
            except (TypeError, ValueError):
                payload = {}
            return {
                "status": r["status"], "fitted_at_utc": r["fitted_at_utc"],
                "payload": payload, "n_samples": r["n_samples"], "r2": r["r2"],
                "window_days": r["window_days"],
            }
        finally:
            conn.close()


def get_daily_mean_outdoor_c(since_day_iso: str) -> dict[str, float]:
    """Mean LIVE outdoor temperature per UTC day since ``since_day_iso`` — the
    seasonal classifier for the DHW shadow's winter watch (#714). UTC days are fine
    for a seasonal question; live rows only for the usual echo reason."""
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                "SELECT date(fetched_at,'unixepoch') d, AVG(outdoor_temp_c) t "
                "FROM daikin_telemetry WHERE source='live' AND outdoor_temp_c IS NOT NULL "
                "AND date(fetched_at,'unixepoch') >= ? GROUP BY d",
                (since_day_iso,),
            )
            return {str(r[0]): float(r[1]) for r in cur.fetchall()}
        finally:
            conn.close()


def get_tank_temps_range(start_epoch: float, end_epoch: float) -> list[tuple[float, float]]:
    """``(fetched_at, tank_temp_c)`` LIVE rows in ``[start, end)``, ascending.

    Bounded form of :func:`get_tank_temps_since` for the DHW calibration (#714),
    which fits UA and the tank's ambient over a fixed historical window rather
    than "everything since". ``source='live'`` only, and for a reason that is the
    whole point of the rewrite: the physics-estimator rows (``source='estimate'``)
    are MODELLED from the very constants this feeds, so admitting them would teach
    the learner its own assumptions — the echo trap that produced two false
    findings on the first attempt (#719)."""
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                "SELECT fetched_at, tank_temp_c FROM daikin_telemetry "
                "WHERE fetched_at >= ? AND fetched_at < ? AND tank_temp_c IS NOT NULL "
                "AND source = 'live' "
                "ORDER BY fetched_at ASC",
                (start_epoch, end_epoch),
            )
            return [(float(r[0]), float(r[1])) for r in cur.fetchall()]
        finally:
            conn.close()


def get_tank_temp_targets_range(
    start_epoch: float, end_epoch: float
) -> list[tuple[float, float, float]]:
    """``(fetched_at, tank_temp_c, tank_target_c)`` LIVE rows in ``[start, end)``,
    ascending — rows where BOTH temperatures are present. Feeds the reheat-
    differential fit (#732): the firmware's deadband is observable exactly at
    the moments the commanded target steps while the tank temperature is known.
    ``source='live'`` only, same echo-trap discipline as get_tank_temps_range."""
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                "SELECT fetched_at, tank_temp_c, tank_target_c FROM daikin_telemetry "
                "WHERE fetched_at >= ? AND fetched_at < ? "
                "AND tank_temp_c IS NOT NULL AND tank_target_c IS NOT NULL "
                "AND source = 'live' "
                "ORDER BY fetched_at ASC",
                (start_epoch, end_epoch),
            )
            return [(float(r[0]), float(r[1]), float(r[2])) for r in cur.fetchall()]
        finally:
            conn.close()


def get_powerful_action_windows(start_iso: str, end_iso: str) -> list[tuple[str, str]]:
    """``(start_time, end_time)`` of Daikin action rows commanded with
    ``tank_powerful`` in the window (#732). Powerful forces a DHW lift at ANY
    Δ, so the reheat-differential fit must exclude target steps inside these
    windows — telemetry alone cannot see the flag. Matches boosts, guests-mode
    warmups and manual overrides alike; status doesn't matter (a cancelled row
    may still have fired its PATCH before cancellation)."""
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                "SELECT start_time, end_time FROM action_schedule "
                "WHERE device = 'daikin' AND end_time >= ? AND start_time <= ? "
                "AND params LIKE '%\"tank_powerful\": true%'",
                (start_iso, end_iso),
            )
            return [(str(r[0]), str(r[1])) for r in cur.fetchall()]
        finally:
            conn.close()


def get_deadband_force_powerful_times(start_iso: str, end_iso: str) -> list[str]:
    """Timestamps of #735 deadband-force fires that took the Powerful FALLBACK
    (``mechanism='powerful'``) in the window (#739).

    These fires mutate only the in-memory apply params — the stored
    action_schedule row deliberately keeps ``tank_powerful: false`` (no
    crosstalk with the #619 stall backoff or the #386 gesture comparison) — so
    :func:`get_powerful_action_windows` cannot see them. Without this the
    reheat-differential fit reads each one as "heated at Δ < deadband" and the
    threshold drifts down, in exactly the #735 incident direction.

    ``hp_target_lift`` fires are intentionally NOT matched: those heat via the
    firmware's real thermostat (Δ = cliff − tank cleared the deadband), so they
    are legitimate, informative episodes for the fit."""
    with _lock:
        conn = get_connection()
        try:
            # NB the LIKE pattern is coupled to log_action's json.dumps default
            # separators (', ', ': '); a move to compact separators would make
            # this match nothing, silently. test_get_deadband_force_powerful_times
            # round-trips through log_action to pin that.
            cur = conn.execute(
                "SELECT timestamp FROM action_log "
                "WHERE action = 'warmup_deadband_force' "
                "AND params LIKE '%\"mechanism\": \"powerful\"%' "
                "AND timestamp >= ? AND timestamp <= ?",
                (start_iso, end_iso),
            )
            return [str(r[0]) for r in cur.fetchall()]
        finally:
            conn.close()


def get_tank_temps_since(since_epoch: float) -> list[tuple[float, float]]:
    """``(fetched_at, tank_temp_c)`` LIVE rows with a non-null tank temperature
    at or after *since_epoch*, ascending. Feeds the evening shower-drawdown
    detector (state_machine._check_dhw_shower_drawdown): the window max is the
    pre-shower hold temperature and the newest rows confirm the drop.

    ``source='live'`` ONLY — the physics-estimator rows (``source='estimate'``,
    written when the Daikin quota is exhausted) model smooth standing-loss
    decay from a seed and by construction cannot see a shower draw; letting
    one into the confirmation pair would veto genuine detections."""
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                "SELECT fetched_at, tank_temp_c FROM daikin_telemetry "
                "WHERE fetched_at >= ? AND tank_temp_c IS NOT NULL "
                "AND source = 'live' "
                "ORDER BY fetched_at ASC",
                (since_epoch,),
            )
            return [(float(r[0]), float(r[1])) for r in cur.fetchall()]
        finally:
            conn.close()


def get_latest_daikin_telemetry(
    *, source: str | None = None
) -> dict[str, Any] | None:
    """Return the most recent daikin_telemetry row, optionally filtered by source.

    ``source='live'`` gives the seed the estimator walks forward from.
    ``source=None`` returns whichever row is newest (live or estimate).
    """
    with _lock:
        conn = get_connection()
        try:
            if source:
                cur = conn.execute(
                    "SELECT * FROM daikin_telemetry WHERE source = ? "
                    "ORDER BY fetched_at DESC LIMIT 1",
                    (source,),
                )
            else:
                cur = conn.execute(
                    "SELECT * FROM daikin_telemetry ORDER BY fetched_at DESC LIMIT 1"
                )
            r = cur.fetchone()
            return dict(r) if r else None
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# V10: runtime_settings — user-editable tunables that take effect without restart.
# Values are stored as TEXT (the service layer handles coercion). A ``None`` read
# is the signal to fall back to the env-derived default in ``src/runtime_settings.py``.
# ---------------------------------------------------------------------------


def get_runtime_setting(key: str) -> str | None:
    """Return the string value for *key*, or None if the row is absent.

    Returns None (forcing the env-default fallback) when the table hasn't been
    created yet — tests and early-boot code exercise config properties before
    ``init_db`` has run, and a missing ``runtime_settings`` table must not
    crash those paths.
    """
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                "SELECT value FROM runtime_settings WHERE key = ?", (key,)
            )
            r = cur.fetchone()
            return str(r[0]) if r else None
        except sqlite3.OperationalError:
            return None
        finally:
            conn.close()


def set_runtime_setting(key: str, value: str) -> None:
    """Upsert *value* for *key* with an UTC timestamp. ``value`` must be pre-validated
    by the caller — this layer is purely persistence."""
    ts = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    with _lock:
        conn = get_connection()
        try:
            conn.execute(
                """INSERT INTO runtime_settings (key, value, updated_at)
                   VALUES (?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET
                     value=excluded.value,
                     updated_at=excluded.updated_at""",
                (key, value, ts),
            )
            conn.commit()
        finally:
            conn.close()


def delete_runtime_setting(key: str) -> bool:
    """Remove the row for *key* so reads fall back to the env default. Returns
    True when a row was deleted."""
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute("DELETE FROM runtime_settings WHERE key = ?", (key,))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


def list_runtime_settings() -> list[dict[str, Any]]:
    """Return all runtime_settings rows as ``{key, value, updated_at}`` dicts."""
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                "SELECT key, value, updated_at FROM runtime_settings ORDER BY key"
            )
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# V11: LP snapshots + forecast history + config audit (cockpit History replay)
# ---------------------------------------------------------------------------

def save_lp_snapshots(
    run_id: int,
    inputs_row: dict[str, Any],
    solution_rows: list[dict[str, Any]],
) -> None:
    """Persist one LP run's inputs + per-slot solution in a single transaction.

    ``run_id`` is ``optimizer_log.id`` returned by :func:`log_optimizer_run`.
    ``inputs_row`` keys map 1:1 onto ``lp_inputs_snapshot`` columns.
    Each element of ``solution_rows`` maps 1:1 onto ``lp_solution_snapshot`` columns
    (excluding ``id`` and ``run_id`` which are filled here).
    """
    with _lock:
        conn = get_connection()
        try:
            conn.execute(
                """INSERT OR REPLACE INTO lp_inputs_snapshot
                   (run_id, run_at_utc, plan_date, horizon_hours,
                    soc_initial_kwh, tank_initial_c, indoor_initial_c,
                    soc_source, tank_source, indoor_source,
                    base_load_json, micro_climate_offset_c, forecast_fetch_at_utc, exogenous_snapshot_json, config_snapshot_json,
                    price_quantize_p, peak_threshold_p, cheap_threshold_p,
                    daikin_control_mode, optimization_preset, energy_strategy_mode, lp_status)
                   VALUES (:run_id, :run_at_utc, :plan_date, :horizon_hours,
                           :soc_initial_kwh, :tank_initial_c, :indoor_initial_c,
                           :soc_source, :tank_source, :indoor_source,
                           :base_load_json, :micro_climate_offset_c, :forecast_fetch_at_utc, :exogenous_snapshot_json, :config_snapshot_json,
                           :price_quantize_p, :peak_threshold_p, :cheap_threshold_p,
                           :daikin_control_mode, :optimization_preset, :energy_strategy_mode, :lp_status)""",
                {
                    "run_id": run_id,
                    "forecast_fetch_at_utc": inputs_row.get("forecast_fetch_at_utc"),
                    "exogenous_snapshot_json": inputs_row.get("exogenous_snapshot_json"),
                    "lp_status": inputs_row.get("lp_status"),
                    **inputs_row,
                },
            )
            for row in solution_rows:
                conn.execute(
                    """INSERT OR REPLACE INTO lp_solution_snapshot
                       (run_id, slot_index, slot_time_utc, price_p,
                        import_kwh, export_kwh, charge_kwh, discharge_kwh,
                        pv_use_kwh, pv_curtail_kwh, pv_forecast_kwh, dhw_kwh, space_kwh,
                        soc_kwh, tank_temp_c, indoor_temp_c, outdoor_temp_c, lwt_offset_c)
                       VALUES (:run_id, :slot_index, :slot_time_utc, :price_p,
                               :import_kwh, :export_kwh, :charge_kwh, :discharge_kwh,
                               :pv_use_kwh, :pv_curtail_kwh, :pv_forecast_kwh, :dhw_kwh, :space_kwh,
                               :soc_kwh, :tank_temp_c, :indoor_temp_c, :outdoor_temp_c, :lwt_offset_c)""",
                    # `pv_forecast_kwh` last so it's always bound (None when a
                    # caller's row omits it) without a KeyError on **row.
                    {"run_id": run_id, **row, "pv_forecast_kwh": row.get("pv_forecast_kwh")},
                )
            conn.commit()
        finally:
            conn.close()


def get_lp_solution_slots(run_id: int) -> list[dict[str, Any]]:
    """Return all slot rows for a given run_id, ordered by slot_index."""
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                "SELECT * FROM lp_solution_snapshot WHERE run_id = ? ORDER BY slot_index",
                (run_id,),
            )
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()


def _parse_iso_utc(s: str | None) -> datetime | None:
    """Parse an ISO timestamp (``...Z`` or ``+00:00``) to an aware UTC datetime."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except ValueError:
        return None


# Numeric lp_solution_snapshot columns that may be stitched per-slot. Whitelisted
# so the column name can be interpolated into SQL safely (never user-supplied).
_STITCHABLE_LP_FIELDS = frozenset({
    "pv_forecast_kwh", "import_kwh", "export_kwh", "charge_kwh", "discharge_kwh",
    "pv_use_kwh", "dhw_kwh", "space_kwh", "soc_kwh", "tank_temp_c", "lwt_offset_c",
})


def committed_lp_field_by_slot(day: date, field: str) -> dict[str, float]:
    """Per-slot committed value of an ``lp_solution_snapshot`` column for ``day``.

    The latest LP solve only covers ``run_at → horizon``, so a single snapshot
    leaves a hole over the morning by evening. This STITCHES across every solve
    of the day: for each slot it returns the column from the most recent solve
    whose ``run_at <= slot_start`` (the plan as known when the slot began); if
    none qualifies (the day's first solve fired after that slot), it falls back
    to the EARLIEST solve that covered the slot. Future slots naturally resolve
    to the latest committed plan (their start is after every run_at).

    ``field`` must be one of :data:`_STITCHABLE_LP_FIELDS` (whitelisted — the
    name is interpolated into SQL). Returns ``{slot_time_utc (stored form):
    value}``. Empty on error or unknown field.
    """
    if field not in _STITCHABLE_LP_FIELDS:
        logger.warning("committed_lp_field_by_slot: refusing unknown field %r", field)
        return {}
    day_start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    day_end = day_start + timedelta(days=1)
    with _lock:
        conn = get_connection()
        try:
            rows = conn.execute(
                f"""SELECT s.slot_time_utc, s.{field}, o.run_at
                   FROM lp_solution_snapshot s
                   JOIN optimizer_log o ON s.run_id = o.id
                   WHERE s.slot_time_utc >= ? AND s.slot_time_utc < ?
                     AND s.{field} IS NOT NULL""",
                (day_start.isoformat(), day_end.isoformat()),
            ).fetchall()
        except sqlite3.Error as e:
            logger.warning("committed_lp_field_by_slot(%s) query failed: %s", field, e)
            return {}
        finally:
            conn.close()

    # Per slot, pick: latest eligible run (run_at <= slot_start); else earliest.
    best: dict[str, tuple[datetime, float, bool]] = {}
    for slot_iso, val, run_at in rows:
        slot_dt = _parse_iso_utc(slot_iso)
        run_dt = _parse_iso_utc(run_at)
        if slot_dt is None or run_dt is None:
            continue
        eligible = run_dt <= slot_dt
        cur = best.get(slot_iso)
        if cur is None:
            best[slot_iso] = (run_dt, float(val), eligible)
            continue
        c_run, _c_val, c_elig = cur
        if eligible and not c_elig:
            best[slot_iso] = (run_dt, float(val), True)  # eligible beats not
        elif eligible and c_elig:
            if run_dt > c_run:  # most recent value known at slot start
                best[slot_iso] = (run_dt, float(val), True)
        elif (not eligible) and (not c_elig):
            if run_dt < c_run:  # earliest value ever planned for the slot
                best[slot_iso] = (run_dt, float(val), False)
        # else: keep the existing eligible row over a non-eligible newcomer
    return {k: v[1] for k, v in best.items()}


def committed_pv_forecast_by_slot(day: date) -> dict[str, float]:
    """Per-slot committed PV-generation forecast for ``day`` (issue #462).

    Thin wrapper over :func:`committed_lp_field_by_slot` for ``pv_forecast_kwh``
    — kept as a named entry point for the many existing callers (/pv/today,
    rebuild_pv_error_log_for_date). Returns ``{slot_time_utc: pv_forecast_kwh}``.
    """
    return committed_lp_field_by_slot(day, "pv_forecast_kwh")


def rebuild_pv_error_log_for_date(day: date) -> int:
    """Persist per-slot committed-forecast-vs-actual rows for ``day`` (#462).

    Joins the stitched committed forecast (:func:`committed_pv_forecast_by_slot`)
    with the realised half-hour solar roll-up. One row per slot that has a
    forecast and/or an actual; idempotent on ``slot_time_utc``. Returns rows
    written. Best-effort: callers (the nightly cron) swallow exceptions.
    """
    forecast = committed_pv_forecast_by_slot(day)
    try:
        actual = half_hourly_solar_kwh_for_day(day)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("rebuild_pv_error_log: actual roll-up failed for %s: %s", day, e)
        actual = {}
    # actual_map keys are Z-form; forecast keys are stored (+00:00) form. Index
    # both by a canonical Z key so they line up.
    def _z(s: str) -> str:
        dt = _parse_iso_utc(s)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else s
    f_by_z = {_z(k): v for k, v in forecast.items()}
    a_by_z = {_z(k): v for k, v in actual.items()}
    built_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    written = 0
    with _lock:
        conn = get_connection()
        try:
            for slot_z in sorted(set(f_by_z) | set(a_by_z)):
                f = f_by_z.get(slot_z)
                a = a_by_z.get(slot_z)
                err = (a - f) if (a is not None and f is not None) else None
                conn.execute(
                    """INSERT INTO pv_error_log
                         (slot_time_utc, run_id, forecast_kwh, actual_kwh, error_kwh, built_at_utc)
                       VALUES (?, NULL, ?, ?, ?, ?)
                       ON CONFLICT(slot_time_utc) DO UPDATE SET
                         forecast_kwh=excluded.forecast_kwh,
                         actual_kwh=excluded.actual_kwh,
                         error_kwh=excluded.error_kwh,
                         built_at_utc=excluded.built_at_utc""",
                    (slot_z, f, a, err, built_at),
                )
                written += 1
            conn.commit()
        except sqlite3.Error as e:
            logger.warning("rebuild_pv_error_log write failed for %s: %s", day, e)
        finally:
            conn.close()
    return written


def committed_dhw_forecast_by_bucket(day: date) -> dict[int, float]:
    """Committed LP DHW forecast for LOCAL ``day``, summed into 2-hour buckets.

    2026-07-02 LP audit (PR C): DHW was the largest UNMONITORED forecast stream
    (load_error_log + pv_error_log exist; DHW didn't). Granularity is the 2-hour
    bucket because that is what the Daikin consumption API provides. Stitch rule
    per slot mirrors :func:`committed_load_forecast_by_slot`: most recent solve
    whose ``run_at <= slot_start``; else the earliest that covered it.
    Returns ``{bucket_idx 0..11: kwh}``; empty on error.
    """
    tz = ZoneInfo(config.BULLETPROOF_TIMEZONE)
    day_start_local = datetime(day.year, day.month, day.day, tzinfo=tz)
    day_end_local = day_start_local + timedelta(days=1)
    q_from = day_start_local.astimezone(UTC).isoformat()
    q_to = day_end_local.astimezone(UTC).isoformat()
    with _lock:
        conn = get_connection()
        try:
            rows = conn.execute(
                """SELECT s.slot_time_utc, s.dhw_kwh, i.run_at_utc
                   FROM lp_solution_snapshot s
                   JOIN lp_inputs_snapshot i ON i.run_id = s.run_id
                   WHERE s.slot_time_utc >= ? AND s.slot_time_utc < ?
                     AND s.dhw_kwh IS NOT NULL""",
                (q_from, q_to),
            ).fetchall()
        except sqlite3.Error as e:
            logger.warning("committed_dhw_forecast_by_bucket query failed: %s", e)
            return {}
        finally:
            conn.close()

    # slot -> (eligible, run_dt, kwh); eligible = run_at <= slot_start
    best: dict[str, tuple[bool, datetime, float]] = {}
    for slot_iso, dhw, run_at in rows:
        slot_dt = _parse_iso_utc(slot_iso)
        run_dt = _parse_iso_utc(run_at)
        if slot_dt is None or run_dt is None:
            continue
        eligible = run_dt <= slot_dt
        cur = best.get(slot_iso)
        cand = (eligible, run_dt, float(dhw or 0.0))
        if cur is None:
            best[slot_iso] = cand
        elif eligible and (not cur[0] or run_dt > cur[1]):
            best[slot_iso] = cand           # latest eligible wins
        elif not eligible and not cur[0] and run_dt < cur[1]:
            best[slot_iso] = cand           # else earliest covering solve

    out: dict[int, float] = {}
    for slot_iso, (_e, _r, kwh) in best.items():
        slot_dt = _parse_iso_utc(slot_iso)
        if slot_dt is None:
            continue
        loc = slot_dt.astimezone(tz)
        if loc.date() != day:
            continue
        out[loc.hour // 2] = out.get(loc.hour // 2, 0.0) + kwh
    return out


def rebuild_dhw_error_log_for_date(day: date) -> int:
    """Persist per-2h-bucket committed-DHW-forecast-vs-actual rows for LOCAL
    ``day``. Actuals come from ``daikin_consumption_2hourly.kwh_dhw`` (written
    by the 02:35 UTC rollup); a NULL split stays NULL (missing ≠ zero — the
    bias learner must not read an absent Daikin split as "measured 0").
    Idempotent on ``(day, bucket_idx)``; returns rows written. Best-effort:
    the nightly cron swallows exceptions.

    Also stamps ``applied_factor`` (the dhw_bucket_bias normalized factor in
    force for the day's forecasts — 1.0 while disabled) and ``mode``. The
    stamp MUST happen before the same cron's bias refresh moves the table,
    and a re-run never overwrites an existing stamp (COALESCE): the factor in
    force at commit time is a historical fact, not a recomputable one.
    """
    forecast = committed_dhw_forecast_by_bucket(day)
    built_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    mode = (getattr(config, "OPTIMIZATION_PRESET", None) or "normal").strip().lower()
    # The current in-force factors describe "now-ish" only. A catch-up
    # rebuild of an OLDER day must stamp 1.0: those days were committed
    # under whatever table was live back then (usually none — the corrector
    # ships disabled), and stamping today's factors would make the learner
    # de-bias raw history by a factor that was never applied (round-2
    # finding: enable + multi-day catch-up would poison a full window).
    in_force: dict[int, float] = {}
    try:
        tz_local = ZoneInfo(getattr(config, "BULLETPROOF_TIMEZONE", "Europe/London"))
        if day >= datetime.now(tz_local).date() - timedelta(days=1):
            from .dhw_bias import factors_in_force
            in_force = factors_in_force(mode)
    except Exception:  # pragma: no cover - defensive
        in_force = {}
    written = 0
    with _lock:
        conn = get_connection()
        try:
            actual = {
                int(b): (float(k) if k is not None else None)
                for b, k in conn.execute(
                    "SELECT bucket_idx, kwh_dhw FROM daikin_consumption_2hourly WHERE date=?",
                    (day.isoformat(),),
                ).fetchall()
            }
            for b in sorted(set(forecast) | set(actual)):
                f = forecast.get(b)
                a = actual.get(b)
                err = (a - f) if (a is not None and f is not None) else None
                conn.execute(
                    """INSERT INTO dhw_error_log
                         (day, bucket_idx, forecast_kwh, actual_kwh, error_kwh,
                          built_at_utc, applied_factor, mode)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(day, bucket_idx) DO UPDATE SET
                         forecast_kwh=excluded.forecast_kwh,
                         actual_kwh=excluded.actual_kwh,
                         error_kwh=excluded.error_kwh,
                         built_at_utc=excluded.built_at_utc,
                         applied_factor=COALESCE(dhw_error_log.applied_factor,
                                                 excluded.applied_factor),
                         mode=COALESCE(dhw_error_log.mode, excluded.mode)""",
                    (day.isoformat(), b, f, a, err, built_at,
                     float(in_force.get(b, 1.0)), mode),
                )
                written += 1
            conn.commit()
        except sqlite3.Error as e:
            logger.warning("rebuild_dhw_error_log(%s) failed: %s", day, e)
        finally:
            conn.close()
    return written


def get_dhw_error_log_range(start_day_iso: str, end_day_iso: str) -> list[dict[str, Any]]:
    """Per-(day, bucket) forecast/actual/error rows with ``day`` in the
    inclusive local-date range, for the dhw_bucket_bias corrector.
    ``applied_factor``/``mode`` may be NULL on pre-migration rows."""
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                """SELECT day, bucket_idx, forecast_kwh, actual_kwh, error_kwh,
                          applied_factor, mode
                   FROM dhw_error_log
                   WHERE day >= ? AND day <= ?
                   ORDER BY day, bucket_idx""",
                (start_day_iso, end_day_iso),
            )
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()


def committed_load_forecast_by_slot(day: date) -> dict[str, tuple[float, float]]:
    """Per-slot committed (total, base) LOAD forecast for ``day``.

    ``total`` = ``base_load_json[slot_index] + dhw_kwh + space_kwh`` (the full
    household load the LP committed to). ``base`` = ``base_load_json[slot_index]``
    alone — the LP's exogenous residual-load input, i.e. the quantity the load
    profile forecasts and a future recent-bias corrector would target.

    Stitched per slot the same way as :func:`committed_lp_field_by_slot` and the
    PV path: for each slot, use the most recent solve whose ``run_at <= slot_start``
    (the plan as known when the slot began); if none qualifies, the earliest solve
    that covered it. Returns ``{slot_time_utc (stored form): (total, base)}``.
    Empty on error.
    """
    day_start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    day_end = day_start + timedelta(days=1)
    with _lock:
        conn = get_connection()
        try:
            rows = conn.execute(
                """SELECT s.slot_time_utc, s.slot_index, s.run_id,
                          s.dhw_kwh, s.space_kwh, i.base_load_json, i.run_at_utc
                   FROM lp_solution_snapshot s
                   JOIN lp_inputs_snapshot i ON i.run_id = s.run_id
                   WHERE s.slot_time_utc >= ? AND s.slot_time_utc < ?
                     AND i.base_load_json IS NOT NULL""",
                (day_start.isoformat(), day_end.isoformat()),
            ).fetchall()
        except sqlite3.Error as e:
            logger.warning("committed_load_forecast_by_slot query failed: %s", e)
            return {}
        finally:
            conn.close()

    arr_cache: dict[int, list] = {}
    # slot -> (run_dt, total, base, eligible)
    best: dict[str, tuple[datetime, float, float, bool]] = {}
    for slot_iso, slot_idx, run_id, dhw, space, base_json, run_at in rows:
        slot_dt = _parse_iso_utc(slot_iso)
        run_dt = _parse_iso_utc(run_at)
        if slot_dt is None or run_dt is None:
            continue
        arr = arr_cache.get(run_id)
        if arr is None:
            try:
                arr = json.loads(base_json or "[]")
            except (TypeError, ValueError):
                arr = []
            arr_cache[run_id] = arr
        try:
            idx = int(slot_idx)
        except (TypeError, ValueError):
            continue
        if idx < 0 or idx >= len(arr):
            continue
        try:
            base = float(arr[idx])
        except (TypeError, ValueError):
            continue
        total = base + float(dhw or 0.0) + float(space or 0.0)
        eligible = run_dt <= slot_dt
        cur = best.get(slot_iso)
        if cur is None:
            best[slot_iso] = (run_dt, total, base, eligible)
            continue
        c_run, _c_t, _c_b, c_elig = cur
        if eligible and not c_elig:
            best[slot_iso] = (run_dt, total, base, True)
        elif eligible and c_elig:
            if run_dt > c_run:
                best[slot_iso] = (run_dt, total, base, True)
        elif (not eligible) and (not c_elig):
            if run_dt < c_run:
                best[slot_iso] = (run_dt, total, base, False)
    return {k: (v[1], v[2]) for k, v in best.items()}


def rebuild_load_error_log_for_date(day: date) -> int:
    """Persist per-slot committed-LOAD-forecast-vs-actual rows for ``day``.

    Joins the stitched committed forecast (:func:`committed_load_forecast_by_slot`)
    with the realised half-hour total-load roll-up. One row per slot that has a
    forecast and/or an actual; idempotent on ``slot_time_utc``. Returns rows
    written. Best-effort: the nightly cron swallows exceptions.
    """
    forecast = committed_load_forecast_by_slot(day)
    try:
        # Trapezoidal integration with the shared gap-guarded helper (same actual
        # path as PV/grid) — an outage can't smear a stale value across the gap.
        actual = _half_hourly_grid_kwh_for_day(day, "load_power_kw")
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("rebuild_load_error_log: actual roll-up failed for %s: %s", day, e)
        actual = {}

    def _z(s: str) -> str:
        dt = _parse_iso_utc(s)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else s

    f_by_z = {_z(k): v for k, v in forecast.items()}
    a_by_z = actual  # already Z-form
    built_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    written = 0
    with _lock:
        conn = get_connection()
        try:
            for slot_z in sorted(set(f_by_z) | set(a_by_z)):
                fv = f_by_z.get(slot_z)
                total = fv[0] if fv else None
                base = fv[1] if fv else None
                a = a_by_z.get(slot_z)
                err = (a - total) if (a is not None and total is not None) else None
                conn.execute(
                    """INSERT INTO load_error_log
                         (slot_time_utc, forecast_kwh, forecast_base_kwh, actual_kwh, error_kwh, built_at_utc)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(slot_time_utc) DO UPDATE SET
                         forecast_kwh=excluded.forecast_kwh,
                         forecast_base_kwh=excluded.forecast_base_kwh,
                         actual_kwh=excluded.actual_kwh,
                         error_kwh=excluded.error_kwh,
                         built_at_utc=excluded.built_at_utc""",
                    (slot_z, total, base, a, err, built_at),
                )
                written += 1
            conn.commit()
        except sqlite3.Error as e:
            logger.warning("rebuild_load_error_log write failed for %s: %s", day, e)
        finally:
            conn.close()
    return written


def backfill_load_error_log(start_day: date, end_day: date) -> dict[str, int]:
    """Rebuild load_error_log for every day in [start_day, end_day] (inclusive).

    Used for the one-time history backfill so the log is populated immediately
    rather than accruing one day per nightly cron run. Returns
    ``{"days": N, "rows": total_rows_written}``.
    """
    days = 0
    rows = 0
    d = start_day
    while d <= end_day:
        rows += rebuild_load_error_log_for_date(d)
        days += 1
        d += timedelta(days=1)
    return {"days": days, "rows": rows}


def get_load_error_log_range(start_utc_iso: str, end_utc_iso: str) -> list[dict[str, Any]]:
    """Per-slot forecast/actual/error rows in ``[start, end)`` for load calibration."""
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                """SELECT slot_time_utc, forecast_kwh, forecast_base_kwh, actual_kwh, error_kwh
                   FROM load_error_log
                   WHERE slot_time_utc >= ? AND slot_time_utc < ?
                   ORDER BY slot_time_utc""",
                (start_utc_iso, end_utc_iso),
            )
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()


def get_load_error_log_for_date(day: date) -> list[dict[str, Any]]:
    """Persisted per-slot forecast/actual/error rows for ``day``."""
    ds = datetime(day.year, day.month, day.day, tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    de = (datetime(day.year, day.month, day.day, tzinfo=UTC) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return get_load_error_log_range(ds, de)


def upsert_export_opportunity(
    day: date, export_kwh: float, seg_pence: float, agile_pence: float
) -> None:
    """Persist a day's export opportunity cost (Agile − SEG). Idempotent on day.

    ``opportunity_pence`` = ``agile_pence − seg_pence`` (>0 = money left on the
    table by being on flat SEG instead of Outgoing Agile). Best-effort.
    """
    opp = round(agile_pence - seg_pence, 4)
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _lock:
        conn = get_connection()
        try:
            conn.execute(
                """INSERT INTO export_opportunity_log
                     (day, export_kwh, seg_pence, agile_pence, opportunity_pence, computed_at_utc)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(day) DO UPDATE SET
                     export_kwh=excluded.export_kwh,
                     seg_pence=excluded.seg_pence,
                     agile_pence=excluded.agile_pence,
                     opportunity_pence=excluded.opportunity_pence,
                     computed_at_utc=excluded.computed_at_utc""",
                (day.isoformat(), round(export_kwh, 4), round(seg_pence, 4),
                 round(agile_pence, 4), opp, now),
            )
            conn.commit()
        except sqlite3.Error as e:
            logger.warning("upsert_export_opportunity write failed for %s: %s", day, e)
        finally:
            conn.close()


def get_export_opportunity(start_day: date, end_day: date) -> list[dict[str, Any]]:
    """Daily export-opportunity rows in ``[start_day, end_day]`` (inclusive)."""
    with _lock:
        conn = get_connection()
        try:
            rows = conn.execute(
                """SELECT day, export_kwh, seg_pence, agile_pence, opportunity_pence
                   FROM export_opportunity_log
                   WHERE day >= ? AND day <= ?
                   ORDER BY day""",
                (start_day.isoformat(), end_day.isoformat()),
            ).fetchall()
        except sqlite3.Error as e:
            logger.warning("get_export_opportunity query failed: %s", e)
            return []
        finally:
            conn.close()
    return [
        {"day": r[0], "export_kwh": r[1], "seg_pence": r[2],
         "agile_pence": r[3], "opportunity_pence": r[4]}
        for r in rows
    ]


def get_pv_error_log_range(start_utc_iso: str, end_utc_iso: str) -> list[dict[str, Any]]:
    """Return per-slot forecast/actual rows in [start, end) (#486 bias loop)."""
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                """SELECT slot_time_utc, forecast_kwh, actual_kwh
                   FROM pv_error_log
                   WHERE slot_time_utc >= ? AND slot_time_utc < ?
                   ORDER BY slot_time_utc""",
                (start_utc_iso, end_utc_iso),
            )
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()


def get_pv_error_log_for_date(day: date) -> list[dict[str, Any]]:
    """Return persisted per-slot forecast/actual/error rows for ``day`` (#462)."""
    day_start = datetime(day.year, day.month, day.day, tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    day_end = (datetime(day.year, day.month, day.day, tzinfo=UTC) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                """SELECT slot_time_utc, forecast_kwh, actual_kwh, error_kwh, built_at_utc
                   FROM pv_error_log
                   WHERE slot_time_utc >= ? AND slot_time_utc < ?
                   ORDER BY slot_time_utc""",
                (day_start, day_end),
            )
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()


def get_lp_inputs(run_id: int) -> dict[str, Any] | None:
    """Return the inputs row for a run_id, or None if absent."""
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                "SELECT * FROM lp_inputs_snapshot WHERE run_id = ?", (run_id,)
            )
            r = cur.fetchone()
            return dict(r) if r else None
        finally:
            conn.close()


def insert_lp_failure(
    *,
    run_at_utc: str,
    error_class: str,
    error_msg: str | None = None,
    plan_date: str | None = None,
    stacktrace: str | None = None,
    lp_inputs_run_id: int | None = None,
) -> int:
    """Append a row to lp_failure_log; return the new id.

    Called from optimizer.py on every LP infeasible / CBC failure / exception.
    Cheap (single INSERT); never raises — callers swallow on error so the
    LP fallback path can't trip on its own audit log.
    """
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                """INSERT INTO lp_failure_log
                       (run_at_utc, plan_date, error_class, error_msg,
                        stacktrace, lp_inputs_run_id)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (run_at_utc, plan_date, error_class, error_msg, stacktrace,
                 lp_inputs_run_id),
            )
            conn.commit()
            return int(cur.lastrowid or 0)
        finally:
            conn.close()


def list_recent_lp_failures(limit: int = 10) -> list[dict[str, Any]]:
    """Return the N most recent LP failure rows, newest first.

    Used by the ``get_recent_lp_failures`` MCP tool so OpenClaw can answer
    'any LP problems lately?' without needing direct DB access.
    """
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                "SELECT * FROM lp_failure_log ORDER BY run_at_utc DESC LIMIT ?",
                (int(limit),),
            )
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()


def get_actuation_health(failed_since_iso: str) -> dict[str, Any]:
    """Raw signals for the actuation-health alert (2026-06-14, after the ~41h
    Fox-upload wedge that nothing alerted on).

    * ``fox_upload_at`` — last SUCCESSFUL Fox V3 upload (``save_fox_schedule_state``
      only runs on success), so a stale value means uploads are failing/wedged.
    * ``tank_last_at`` — last tank action that actually RAN (``completed``,
      incl. the benign ``noop`` skip which still proves the reconciler is
      firing, or ``active``). Tank fires ~2×/day under dhw_policy, so a stale
      value is a robust "reconciler stopped actuating the tank" signal.
    * ``{tank,lwt}_failed_24h`` — Daikin rows the device REJECTED (``failed``
      status) since ``failed_since_iso``. LWT has no age signal — it's
      demand-gated and legitimately dormant in summer — so failures are its
      only meaningful actuation-health signal.

    ISO timestamps compare lexicographically (all 'YYYY-MM-DDTHH:MM:SS...').
    """
    out: dict[str, Any] = {
        "fox_upload_at": None, "tank_last_at": None,
        "tank_failed_24h": 0, "lwt_failed_24h": 0,
    }
    with _lock:
        conn = get_connection()
        try:
            r = conn.execute(
                "SELECT uploaded_at FROM fox_schedule_state ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if r:
                out["fox_upload_at"] = r["uploaded_at"]
            r = conn.execute(
                "SELECT MAX(COALESCE(executed_at, created_at)) AS t FROM action_schedule "
                "WHERE device='daikin' AND action_type LIKE 'tank%' "
                "AND status IN ('completed', 'active')"
            ).fetchone()
            out["tank_last_at"] = r["t"] if r else None
            for dom, like in (("tank", "tank%"), ("lwt", "lwt%")):
                r = conn.execute(
                    "SELECT COUNT(*) AS n FROM action_schedule "
                    "WHERE device='daikin' AND action_type LIKE ? AND status='failed' "
                    "AND COALESCE(executed_at, created_at) >= ?",
                    (like, failed_since_iso),
                ).fetchone()
                out[f"{dom}_failed_24h"] = int(r["n"]) if r else 0
            return out
        finally:
            conn.close()


def find_run_for_time(when_utc: str) -> int | None:
    """Return the most recent optimizer_log.id whose run_at <= when_utc.

    Used by the History endpoint to pick which LP run's snapshot to display
    for a given past moment.
    """
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                """SELECT id FROM optimizer_log
                   WHERE run_at <= ?
                   ORDER BY run_at DESC
                   LIMIT 1""",
                (when_utc,),
            )
            r = cur.fetchone()
            return int(r[0]) if r else None
        finally:
            conn.close()


def save_meteo_forecast_history(
    forecast_fetch_at_utc: str,
    rows: list[dict[str, Any]],
) -> None:
    """Legacy wrapper for persisting a canonical forecast fetch without marking it latest."""
    save_meteo_forecast_snapshot(
        forecast_fetch_at_utc,
        rows,
        mark_latest=False,
    )


def get_meteo_forecast_at(fetch_at_utc: str) -> list[dict[str, Any]]:
    """Return the forecast rows stored for a specific fetch timestamp."""
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                """SELECT slot_time, temp_c, solar_w_m2, cloud_cover_pct, direct_pv_kw
                   FROM meteo_forecast_value
                   WHERE forecast_fetch_at_utc = ?
                   ORDER BY slot_time""",
                (fetch_at_utc,),
            )
            rows = [dict(r) for r in cur.fetchall()]
            if rows:
                return rows
            cur = conn.execute(
                """SELECT slot_time, temp_c, solar_w_m2, cloud_cover_pct
                   FROM meteo_forecast_history
                   WHERE forecast_fetch_at_utc = ?
                   ORDER BY slot_time""",
                (fetch_at_utc,),
            )
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()


def upsert_pv_calibration_hourly(
    factors: dict[int, float],
    samples: dict[int, int],
    window_days: int,
) -> int:
    """Replace the ``pv_calibration_hourly`` table with the freshly-computed factors.

    Returns the number of rows written. Idempotent ``INSERT OR REPLACE`` per hour;
    hours not present in ``factors`` are left as-is (so a partial recompute doesn't
    wipe valid data for other hours).
    """
    if not factors:
        return 0
    from datetime import UTC as _UTC, datetime as _dt
    now = _dt.now(_UTC).isoformat()
    n = 0
    with _lock:
        conn = get_connection()
        try:
            for hour, factor in factors.items():
                conn.execute(
                    """INSERT OR REPLACE INTO pv_calibration_hourly
                       (hour_utc, factor, samples, window_days, computed_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        int(hour),
                        float(factor),
                        int(samples.get(hour, 0)),
                        int(window_days),
                        now,
                    ),
                )
                n += 1
            conn.commit()
        finally:
            conn.close()
    return n


def get_pv_calibration_hourly() -> dict[int, float]:
    """Return cached per-hour-of-day calibration factors, or ``{}`` when empty."""
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                "SELECT hour_utc, factor FROM pv_calibration_hourly"
            )
            return {int(r[0]): float(r[1]) for r in cur.fetchall()}
        finally:
            conn.close()


def upsert_pv_recent_bias(
    factors: dict[int, float],
    raw_ratios: dict[int, float],
    samples: dict[int, int],
    computed_at: str,
) -> int:
    """Replace the ``pv_recent_bias`` table with freshly-computed factors (#486)."""
    n = 0
    with _lock:
        conn = get_connection()
        try:
            conn.execute("DELETE FROM pv_recent_bias")
            for h, f in factors.items():
                conn.execute(
                    """INSERT OR REPLACE INTO pv_recent_bias
                       (hour_utc, factor, raw_ratio, samples, computed_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (int(h), float(f), float(raw_ratios.get(h, f)), int(samples.get(h, 0)), computed_at),
                )
                n += 1
            conn.commit()
        finally:
            conn.close()
    return n


def get_latest_forecast_snapshot_meta() -> dict[str, Any] | None:
    """Metadata of the most recent canonical forecast fetch, or None.

    Returns ``{forecast_fetch_at_utc, source, model_name, model_version}``
    from the snapshot pointed at by ``meteo_forecast_latest_state``. Powers
    the cockpit's forecast-provenance chip (#542): after the Quartz sidecar
    migration the healthy value is ``model_name='quartz-open-site'``;
    anything else means the fetch degraded to a fallback.
    """
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                """SELECT s.forecast_fetch_at_utc, s.source, s.model_name,
                          s.model_version
                   FROM meteo_forecast_snapshot s
                   JOIN meteo_forecast_latest_state ls
                     ON ls.forecast_fetch_at_utc = s.forecast_fetch_at_utc
                   WHERE ls.id = 1"""
            )
            r = cur.fetchone()
            return dict(r) if r else None
        finally:
            conn.close()


def get_pv_recent_bias() -> dict[int, float]:
    """Return cached per-hour adaptive PV bias factors, or ``{}`` when empty."""
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute("SELECT hour_utc, factor FROM pv_recent_bias")
            return {int(r[0]): float(r[1]) for r in cur.fetchall()}
        finally:
            conn.close()


def upsert_load_recent_bias(
    biases: dict[int, float],
    raw_biases: dict[int, float],
    samples: dict[int, int],
    computed_at: str,
) -> int:
    """Replace the ``load_recent_bias`` table with freshly-computed per-local-hour
    additive corrections (Phase 2)."""
    n = 0
    with _lock:
        conn = get_connection()
        try:
            conn.execute("DELETE FROM load_recent_bias")
            for h, b in biases.items():
                conn.execute(
                    """INSERT OR REPLACE INTO load_recent_bias
                       (hour_local, bias_kwh, raw_bias_kwh, samples, computed_at)
                       VALUES (?, ?, ?, ?, ?)""",
                    (int(h), float(b), float(raw_biases.get(h, b)), int(samples.get(h, 0)), computed_at),
                )
                n += 1
            conn.commit()
        finally:
            conn.close()
    return n


def get_load_recent_bias() -> dict[int, float]:
    """Return cached per-LOCAL-hour additive load-bias corrections (kWh/slot), or
    ``{}`` when empty. The optimizer reads this only when LOAD_RECENT_BIAS_ENABLED."""
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute("SELECT hour_local, bias_kwh FROM load_recent_bias")
            return {int(r[0]): float(r[1]) for r in cur.fetchall()}
        finally:
            conn.close()


def upsert_dhw_bucket_bias(
    factors: dict[int, float],
    raw_ratios: dict[int, float],
    samples: dict[int, int],
    computed_at: str,
    days: dict[int, int] | None = None,
) -> int:
    """Replace the ``dhw_bucket_bias`` table with freshly-computed per-bucket
    multiplicative factors."""
    n = 0
    with _lock:
        conn = get_connection()
        try:
            conn.execute("DELETE FROM dhw_bucket_bias")
            for b, f in factors.items():
                conn.execute(
                    """INSERT OR REPLACE INTO dhw_bucket_bias
                       (bucket_idx, factor, raw_ratio, samples, days, computed_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (int(b), float(f), float(raw_ratios.get(b, f)),
                     int(samples.get(b, 0)), int((days or {}).get(b, 0)), computed_at),
                )
                n += 1
            conn.commit()
        finally:
            conn.close()
    return n


def get_dhw_bucket_bias(max_age_days: int | None = None) -> dict[int, float]:
    """Return cached per-local-2h-bucket multiplicative DHW factors, or ``{}``
    when empty — or when ``max_age_days`` is given and the table is staler
    than that (a vacation / Daikin outage must not leave a fossil correction
    applying silently forever). The forecast applies them only when
    DHW_BUCKET_BIAS_ENABLED."""
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute("SELECT bucket_idx, factor, computed_at FROM dhw_bucket_bias")
            rows = cur.fetchall()
        finally:
            conn.close()
    if not rows:
        return {}
    if max_age_days is not None:
        try:
            newest = max(str(r[2]) for r in rows)
            ts = datetime.fromisoformat(newest.replace("Z", "+00:00"))
            if datetime.now(UTC) - ts > timedelta(days=int(max_age_days)):
                return {}
        except (ValueError, TypeError):
            return {}
    return {int(r[0]): float(r[1]) for r in rows}


def upsert_pv_calibration_hourly_cloud(
    factors: dict[tuple[int, int], float],
    samples: dict[tuple[int, int], int],
    window_days: int,
) -> int:
    """Replace ``pv_calibration_hourly_cloud`` rows for the given (hour, bucket) pairs."""
    if not factors:
        return 0
    from datetime import UTC as _UTC, datetime as _dt
    now = _dt.now(_UTC).isoformat()
    n = 0
    with _lock:
        conn = get_connection()
        try:
            for (hour, bucket), factor in factors.items():
                conn.execute(
                    """INSERT OR REPLACE INTO pv_calibration_hourly_cloud
                       (hour_utc, cloud_bucket, factor, samples, window_days, computed_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        int(hour), int(bucket),
                        float(factor),
                        int(samples.get((hour, bucket), 0)),
                        int(window_days),
                        now,
                    ),
                )
                n += 1
            conn.commit()
        finally:
            conn.close()
    return n


def get_pv_calibration_hourly_cloud() -> dict[tuple[int, int], float]:
    """Return cached per-(hour, cloud-bucket) factors, or ``{}`` when empty.

    Bucket convention: 0=clear (0-25%), 1=partly (25-50%), 2=mostly (50-75%),
    3=overcast (75-100%). See PR #232 / pv_calibration_hourly_cloud schema.
    """
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                "SELECT hour_utc, cloud_bucket, factor FROM pv_calibration_hourly_cloud"
            )
            return {(int(r[0]), int(r[1])): float(r[2]) for r in cur.fetchall()}
        finally:
            conn.close()


def upsert_pv_calibration_3d(
    factors: dict[tuple[int, int, int], float],
    samples: dict[tuple[int, int, int], int],
    window_days: int,
) -> int:
    """Replace pv_calibration_3d rows for the given (hour, cloud, elevation) cells.

    PR L3 (2026-05-24) — 3D table extends the 2D cloud table with a solar
    elevation dimension. Lookup chain in ``get_pv_calibration_factor_for``:
    3d → 2d → 1d → flat.
    """
    if not factors:
        return 0
    from datetime import UTC as _UTC
    from datetime import datetime as _dt
    now = _dt.now(_UTC).isoformat()
    n = 0
    with _lock:
        conn = get_connection()
        try:
            for (hour, cloud, elev), factor in factors.items():
                conn.execute(
                    """INSERT OR REPLACE INTO pv_calibration_3d
                       (hour_utc, cloud_bucket, elevation_bucket,
                        factor, samples, window_days, computed_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        int(hour), int(cloud), int(elev),
                        float(factor),
                        int(samples.get((hour, cloud, elev), 0)),
                        int(window_days),
                        now,
                    ),
                )
                n += 1
            conn.commit()
        finally:
            conn.close()
    return n


def get_pv_calibration_3d() -> dict[tuple[int, int, int], float]:
    """Return cached (hour, cloud_bucket, elevation_bucket) → factor map,
    or ``{}`` when empty. PR L3."""
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                """SELECT hour_utc, cloud_bucket, elevation_bucket, factor
                   FROM pv_calibration_3d"""
            )
            return {
                (int(r[0]), int(r[1]), int(r[2])): float(r[3])
                for r in cur.fetchall()
            }
        finally:
            conn.close()


def upsert_forecast_skill_rows(rows: list[dict[str, Any]]) -> int:
    """Upsert daily per-hour forecast-skill rows.

    ``rows`` elements should contain:
    ``date_utc``, ``hour_of_day``, ``predicted_temp_c``, ``actual_temp_c``,
    ``predicted_pv_kwh``, ``actual_pv_kwh``, plus optional
    ``predicted_load_kwh`` / ``actual_load_kwh``.
    """
    if not rows:
        return 0
    built_at_utc = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    n = 0
    with _lock:
        conn = get_connection()
        try:
            for row in rows:
                conn.execute(
                    """INSERT OR REPLACE INTO forecast_skill_log
                       (date_utc, hour_of_day, predicted_temp_c, actual_temp_c,
                        predicted_pv_kwh, actual_pv_kwh,
                        predicted_load_kwh, actual_load_kwh, built_at_utc)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        str(row["date_utc"]),
                        int(row["hour_of_day"]),
                        row.get("predicted_temp_c"),
                        row.get("actual_temp_c"),
                        row.get("predicted_pv_kwh"),
                        row.get("actual_pv_kwh"),
                        row.get("predicted_load_kwh"),
                        row.get("actual_load_kwh"),
                        built_at_utc,
                    ),
                )
                n += 1
            conn.commit()
        finally:
            conn.close()
    return n


def rebuild_forecast_skill_log_for_date(date_utc: str) -> int:
    """Rebuild forecast-vs-actual skill rows for one UTC date.

    Prediction source: every canonical fetch (``meteo_forecast_snapshot``)
    strictly before each target hour, so the row reflects what the planner
    actually knew at the time. Successive same-hour fetches overwrite, so
    the *latest* prior fetch wins.

    Actuals:
    ``pv_realtime_history`` mean solar/load kW × 1h for PV/load, and mean
    ``daikin_telemetry.outdoor_temp_c`` (live source) for temperature.
    Predicted load is the latest LP run's per-slot expectation
    ``base_load + dhw + space`` summed across the hour.
    """
    from .weather import compute_pv_calibration_factor, forecast_pv_kw_from_row

    predicted_by_hour: dict[int, dict[str, float | None]] = {}
    actual_pv_by_hour: dict[int, float] = {}
    actual_load_by_hour: dict[int, float] = {}
    actual_temp_by_hour: dict[int, float] = {}
    predicted_load_by_hour: dict[int, float] = {}

    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                """SELECT mv.forecast_fetch_at_utc, mv.slot_time, mv.temp_c, mv.solar_w_m2,
                          mv.cloud_cover_pct, mv.direct_pv_kw
                   FROM meteo_forecast_value mv
                   JOIN meteo_forecast_snapshot ms
                     ON ms.forecast_fetch_at_utc = mv.forecast_fetch_at_utc
                   WHERE substr(mv.slot_time, 1, 10) = ?
                   ORDER BY mv.slot_time ASC, mv.forecast_fetch_at_utc ASC""",
                (date_utc,),
            )
            history_rows = [dict(r) for r in cur.fetchall()]

            cur = conn.execute(
                """SELECT substr(captured_at, 12, 2) AS hour_utc,
                          AVG(solar_power_kw) AS avg_kw
                   FROM pv_realtime_history
                   WHERE substr(captured_at, 1, 10) = ?
                     AND solar_power_kw IS NOT NULL
                   GROUP BY hour_utc""",
                (date_utc,),
            )
            for row in cur.fetchall():
                actual_pv_by_hour[int(row["hour_utc"])] = float(row["avg_kw"] or 0.0)

            cur = conn.execute(
                """SELECT substr(captured_at, 12, 2) AS hour_utc,
                          AVG(load_power_kw) AS avg_kw
                   FROM pv_realtime_history
                   WHERE substr(captured_at, 1, 10) = ?
                     AND load_power_kw IS NOT NULL
                   GROUP BY hour_utc""",
                (date_utc,),
            )
            for row in cur.fetchall():
                actual_load_by_hour[int(row["hour_utc"])] = float(row["avg_kw"] or 0.0)

            cur = conn.execute(
                """SELECT CAST(strftime('%H', datetime(fetched_at, 'unixepoch')) AS INTEGER) AS hour_utc,
                          AVG(outdoor_temp_c) AS avg_temp_c
                   FROM daikin_telemetry
                   WHERE date(datetime(fetched_at, 'unixepoch')) = ?
                     AND source = 'live'
                     AND outdoor_temp_c IS NOT NULL
                   GROUP BY hour_utc""",
                (date_utc,),
            )
            for row in cur.fetchall():
                actual_temp_by_hour[int(row["hour_utc"])] = float(row["avg_temp_c"])

            cur = conn.execute(
                """
                SELECT s.slot_time_utc, s.slot_index, s.dhw_kwh, s.space_kwh, i.base_load_json
                  FROM lp_solution_snapshot s
                  JOIN lp_inputs_snapshot i ON i.run_id = s.run_id
                  JOIN (
                        SELECT slot_time_utc, MAX(run_id) AS max_run
                          FROM lp_solution_snapshot
                         WHERE substr(slot_time_utc, 1, 10) = ?
                         GROUP BY slot_time_utc
                       ) latest
                    ON latest.slot_time_utc = s.slot_time_utc
                   AND latest.max_run = s.run_id
                 ORDER BY s.slot_time_utc ASC
                """,
                (date_utc,),
            )
            load_rows = [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

    cal_cloud = get_pv_calibration_hourly_cloud()
    cal_hour = get_pv_calibration_hourly()
    cal_3d = get_pv_calibration_3d()
    flat_cal = compute_pv_calibration_factor() if not cal_cloud and not cal_hour else 1.0
    # #486 follow-up — the LP weather builder applies the adaptive recent-bias
    # factor ON TOP of the calibration chain (see the matching block in
    # ``weather.build_*``). The audit must measure the SAME forecast the
    # planner uses, or the skill log shows a phantom pre-correction residual —
    # the exact skew class the PR L3 comment above fixed for the 3D table.
    # Keep this gate identical to the weather-builder gate.
    recent_bias: dict[int, float] = {}
    if getattr(config, "PV_RECENT_BIAS_ENABLED", False):
        try:
            recent_bias = get_pv_recent_bias()
        except sqlite3.OperationalError:
            recent_bias = {}

    for row in history_rows:
        slot_time = str(row.get("slot_time") or "")
        fetch_at = str(row.get("forecast_fetch_at_utc") or "")
        if not slot_time or not fetch_at or fetch_at >= slot_time:
            continue
        try:
            hour_utc = int(slot_time[11:13])
        except ValueError:
            continue
        # PR L3 — reconstruct slot_utc so the 3D lookup chain fires for
        # the bias-audit path. Without this, ``forecast_skill_log`` would
        # report bias against the 2D-calibrated forecast while the LP
        # runs against the 3D-calibrated one — phantom skew in the audit.
        try:
            slot_utc_dt = datetime.fromisoformat(slot_time.replace("Z", "+00:00"))
            if slot_utc_dt.tzinfo is None:
                slot_utc_dt = slot_utc_dt.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            slot_utc_dt = None
        cloud_raw = row.get("cloud_cover_pct")
        cloud_pct = float(cloud_raw) if cloud_raw is not None else 50.0
        rad_wm2 = float(row.get("solar_w_m2") or 0.0)
        predicted_by_hour[hour_utc] = {
            "predicted_temp_c": (
                float(row["temp_c"]) if row.get("temp_c") is not None else None
            ),
            "predicted_pv_kwh": forecast_pv_kw_from_row(
                hour_utc,
                rad_wm2,
                cloud_pct,
                direct_pv_kw=row.get("direct_pv_kw"),
                cloud_table=cal_cloud,
                hourly_table=cal_hour,
                flat=flat_cal,
                table_3d=cal_3d,
                slot_utc=slot_utc_dt,
            )
            * (recent_bias.get(hour_utc, 1.0) if recent_bias else 1.0),
        }

    for row in load_rows:
        try:
            base_loads = json.loads(row.get("base_load_json") or "[]")
            slot_index = int(row.get("slot_index") or 0)
            slot_time = str(row.get("slot_time_utc") or "")
            hour_utc = int(slot_time[11:13])
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if slot_index < 0 or slot_index >= len(base_loads):
            continue
        predicted_load_by_hour[hour_utc] = predicted_load_by_hour.get(hour_utc, 0.0) + (
            float(base_loads[slot_index])
            + float(row.get("dhw_kwh") or 0.0)
            + float(row.get("space_kwh") or 0.0)
        )

    out_rows: list[dict[str, Any]] = []
    for hour_utc in sorted(set(predicted_by_hour) | set(predicted_load_by_hour)):
        predicted = predicted_by_hour.get(hour_utc, {})
        actual_pv = actual_pv_by_hour.get(hour_utc)
        actual_load = actual_load_by_hour.get(hour_utc)
        actual_temp = actual_temp_by_hour.get(hour_utc)
        predicted_load = predicted_load_by_hour.get(hour_utc)
        if actual_pv is None and actual_temp is None and actual_load is None:
            continue
        out_rows.append(
            {
                "date_utc": date_utc,
                "hour_of_day": hour_utc,
                "predicted_temp_c": predicted.get("predicted_temp_c"),
                "actual_temp_c": actual_temp,
                "predicted_pv_kwh": predicted.get("predicted_pv_kwh"),
                "actual_pv_kwh": actual_pv,
                "predicted_load_kwh": predicted_load,
                "actual_load_kwh": actual_load,
            }
        )
    return upsert_forecast_skill_rows(out_rows)


def get_forecast_skill_rows(
    start_date_utc: str,
    end_date_utc: str,
) -> list[dict[str, Any]]:
    """Return forecast-skill rows in the inclusive UTC date range."""
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                """SELECT date_utc, hour_of_day, predicted_temp_c, actual_temp_c,
                          predicted_pv_kwh, actual_pv_kwh,
                          predicted_load_kwh, actual_load_kwh, built_at_utc
                   FROM forecast_skill_log
                   WHERE date_utc >= ? AND date_utc <= ?
                   ORDER BY date_utc ASC, hour_of_day ASC""",
                (start_date_utc, end_date_utc),
            )
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()


def get_meteo_forecast_history_latest_before(when_utc: str) -> list[dict[str, Any]]:
    """Return all rows of the most recent forecast fetch strictly before ``when_utc``.

    Used by the Waze MPC forecast-revision trigger (Epic #73 — story #144) to
    compare a freshly-pulled forecast against the previous version. Returns
    ``[]`` when no prior fetch exists.
    """
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                """SELECT forecast_fetch_at_utc
                   FROM meteo_forecast_snapshot
                   WHERE forecast_fetch_at_utc < ?
                   ORDER BY forecast_fetch_at_utc DESC
                   LIMIT 1""",
                (when_utc,),
            )
            row = cur.fetchone()
            if row and row[0]:
                cur = conn.execute(
                    """SELECT slot_time, temp_c, solar_w_m2, cloud_cover_pct, direct_pv_kw
                       FROM meteo_forecast_value
                       WHERE forecast_fetch_at_utc = ?
                       ORDER BY slot_time""",
                    (str(row[0]),),
                )
                rows = [dict(r) for r in cur.fetchall()]
                if rows:
                    return rows
            cur = conn.execute(
                """SELECT forecast_fetch_at_utc
                   FROM meteo_forecast_history
                   WHERE forecast_fetch_at_utc < ?
                   ORDER BY forecast_fetch_at_utc DESC
                   LIMIT 1""",
                (when_utc,),
            )
            row = cur.fetchone()
            if not row:
                return []
            prev_fetch_at = row[0]
            cur = conn.execute(
                """SELECT slot_time, temp_c, solar_w_m2, cloud_cover_pct
                   FROM meteo_forecast_history
                   WHERE forecast_fetch_at_utc = ?
                   ORDER BY slot_time""",
                (prev_fetch_at,),
            )
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()


def prune_meteo_forecast_snapshots(max_age_days: int) -> tuple[int, int]:
    """Prune canonical forecast fetches and their slot rows as one unit.

    Returns ``(deleted_snapshots, deleted_values)``. We prune by fetch timestamp
    because the snapshot row is the source of truth; per-slot rows are just its
    normalized expansion for replay/query efficiency.
    """
    if max_age_days <= 0:
        return 0, 0
    from datetime import UTC as _UTC
    from datetime import datetime as _dt
    from datetime import timedelta as _td

    cutoff = (_dt.now(_UTC) - _td(days=max_age_days)).isoformat()
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                """SELECT forecast_fetch_at_utc
                   FROM meteo_forecast_snapshot
                   WHERE forecast_fetch_at_utc < ?""",
                (cutoff,),
            )
            old_fetches = [str(r[0]) for r in cur.fetchall() if r and r[0]]
            if not old_fetches:
                return 0, 0

            latest_fetch_at = _get_latest_meteo_forecast_fetch_at(conn)
            if latest_fetch_at and latest_fetch_at in old_fetches:
                conn.execute(
                    "UPDATE meteo_forecast_latest_state SET forecast_fetch_at_utc = NULL WHERE id = 1"
                )

            placeholders = ",".join("?" for _ in old_fetches)
            cur = conn.execute(
                f"""DELETE FROM meteo_forecast_value
                    WHERE forecast_fetch_at_utc IN ({placeholders})""",
                old_fetches,
            )
            deleted_values = int(cur.rowcount or 0)
            cur = conn.execute(
                f"""DELETE FROM meteo_forecast_snapshot
                    WHERE forecast_fetch_at_utc IN ({placeholders})""",
                old_fetches,
            )
            deleted_snapshots = int(cur.rowcount or 0)
            conn.commit()
            return deleted_snapshots, deleted_values
        finally:
            conn.close()


def log_config_change(
    key: str,
    value: str | None,
    op: str,
    actor: str | None = None,
) -> None:
    """Append one row to ``config_audit``.

    ``op`` is ``"set"`` or ``"delete"``. Called from
    ``runtime_settings.set_setting`` / ``delete_setting``.
    """
    ts = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    with _lock:
        conn = get_connection()
        try:
            conn.execute(
                """INSERT INTO config_audit (key, value, op, actor, changed_at_utc)
                   VALUES (?, ?, ?, ?, ?)""",
                (key, value, op, actor, ts),
            )
            conn.commit()
        finally:
            conn.close()


def get_config_audit(key: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    """Return recent config_audit rows, optionally filtered by key."""
    with _lock:
        conn = get_connection()
        try:
            if key is None:
                cur = conn.execute(
                    "SELECT * FROM config_audit ORDER BY changed_at_utc DESC LIMIT ?",
                    (limit,),
                )
            else:
                cur = conn.execute(
                    "SELECT * FROM config_audit WHERE key = ? ORDER BY changed_at_utc DESC LIMIT ?",
                    (key, limit),
                )
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()


def prune_old_rows(
    table: str,
    timestamp_col: str,
    max_age_days: int,
    *,
    epoch_seconds: bool = False,
) -> int:
    """Delete rows from *table* older than *max_age_days*.

    Append-only tables (daikin_telemetry, meteo_forecast_history,
    meteo_forecast_snapshot/value, lp_solution_snapshot, lp_inputs_snapshot, config_audit) have no
    built-in purge policy. Without it they grow unbounded. This helper is
    called from :func:`prune_history_tables` on service startup + daily cron.

    ``epoch_seconds=True`` for tables that store timestamps as a REAL
    Unix epoch (daikin_telemetry.fetched_at). Everything else uses ISO
    strings, which compare lexicographically.
    Returns the number of rows deleted.
    """
    if max_age_days <= 0:
        return 0
    with _lock:
        conn = get_connection()
        try:
            if epoch_seconds:
                import time as _time
                cutoff: Any = _time.time() - max_age_days * 86400
            else:
                from datetime import UTC as _UTC
                from datetime import datetime as _dt
                from datetime import timedelta as _td
                cutoff = (_dt.now(_UTC) - _td(days=max_age_days)).isoformat()
            cur = conn.execute(
                f"DELETE FROM {table} WHERE {timestamp_col} < ?",
                (cutoff,),
            )
            conn.commit()
            return int(cur.rowcount or 0)
        finally:
            conn.close()


# Whitelist for the archive helpers below — the table/column names are
# interpolated into SQL, so only these known-safe sensor tables are allowed.
_ARCHIVABLE_TABLES: dict[str, set[str]] = {
    "room_temperature_history": {"captured_at"},
    "device_reading_log": {"received_at", "captured_at"},
}


def fetch_rows_older_than(table: str, ts_col: str, cutoff_iso: str) -> list[dict[str, Any]]:
    """Return full rows (as dicts) from a whitelisted sensor table whose
    ``ts_col`` is strictly before ``cutoff_iso`` — for archival before pruning."""
    if ts_col not in _ARCHIVABLE_TABLES.get(table, set()):
        raise ValueError(f"not archivable: {table}.{ts_col}")
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                f"SELECT * FROM {table} WHERE {ts_col} < ? ORDER BY {ts_col}",
                (cutoff_iso,),
            )
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()


def delete_rows_older_than(table: str, ts_col: str, cutoff_iso: str) -> int:
    """Delete rows from a whitelisted sensor table older than ``cutoff_iso``.
    Returns the row count. Paired with :func:`fetch_rows_older_than`."""
    if ts_col not in _ARCHIVABLE_TABLES.get(table, set()):
        raise ValueError(f"not archivable: {table}.{ts_col}")
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(f"DELETE FROM {table} WHERE {ts_col} < ?", (cutoff_iso,))
            conn.commit()
            return int(cur.rowcount or 0)
        finally:
            conn.close()


def _floor_15min(ts_iso: str) -> str | None:
    """Floor an ISO timestamp to its 15-min bucket start (UTC, ``...:00Z``)."""
    try:
        dt = datetime.fromisoformat(str(ts_iso).replace("Z", "+00:00")).astimezone(UTC)
    except (ValueError, TypeError):
        return None
    dt = dt.replace(minute=(dt.minute // 15) * 15, second=0, microsecond=0)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def refresh_indoor_rollup_15min(lookback_days: int = 3) -> int:
    """Recompute the 15-min indoor rollup for the recent window and upsert it.

    Recomputes only the last ``lookback_days`` of buckets each run (cheap, and
    buckets finalise long before the raw's retention). Returns buckets written.
    """
    start = (datetime.now(UTC) - timedelta(days=max(1, lookback_days))).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    with _lock:
        conn = get_connection()
        try:
            rows = conn.execute(
                "SELECT captured_at, room, temp_c FROM room_temperature_history "
                "WHERE captured_at >= ?",
                (start,),
            ).fetchall()
            agg: dict[tuple[str, str], list[float]] = {}
            for r in rows:
                b = _floor_15min(r["captured_at"])
                if b is None or r["temp_c"] is None:
                    continue
                agg.setdefault((b, r["room"] or "home"), []).append(float(r["temp_c"]))
            for (bucket, room), temps in agg.items():
                conn.execute(
                    """INSERT INTO room_temperature_rollup_15min
                         (bucket_utc, room, mean_c, min_c, max_c, n)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(bucket_utc, room) DO UPDATE SET
                         mean_c=excluded.mean_c, min_c=excluded.min_c,
                         max_c=excluded.max_c, n=excluded.n""",
                    (bucket, room, sum(temps) / len(temps), min(temps), max(temps), len(temps)),
                )
            conn.commit()
            return len(agg)
        finally:
            conn.close()


def get_indoor_rollup_15min(start_iso: str, end_iso: str, room: str | None = None) -> list[dict[str, Any]]:
    """WARM-tier 15-min indoor rollup rows in a range (viewer / long-term UI)."""
    with _lock:
        conn = get_connection()
        try:
            q = ("SELECT bucket_utc, room, mean_c, min_c, max_c, n "
                 "FROM room_temperature_rollup_15min WHERE bucket_utc >= ? AND bucket_utc < ?")
            args: list[Any] = [start_iso, end_iso]
            if room:
                q += " AND room = ?"
                args.append(room)
            q += " ORDER BY bucket_utc"
            return [dict(r) for r in conn.execute(q, args).fetchall()]
        finally:
            conn.close()


def prune_history_tables() -> dict[str, int]:
    """Run all configured retention policies in one pass.

    Returns per-table deletion counts. Called at app startup (best-effort,
    never fatal) and from a daily cron. Individual failures are logged at
    DEBUG and surface as ``-1`` in the result so the caller can tell which
    tables couldn't be pruned without breaking the rest.
    """
    from .config import config as _config

    policies = [
        ("daikin_telemetry", "fetched_at", _config.DAIKIN_TELEMETRY_RETENTION_DAYS, True),
        ("meteo_forecast_history", "forecast_fetch_at_utc", _config.METEO_FORECAST_HISTORY_RETENTION_DAYS, False),
        ("forecast_skill_log", "built_at_utc", _config.METEO_FORECAST_HISTORY_RETENTION_DAYS, False),
        ("pv_error_log", "slot_time_utc", _config.METEO_FORECAST_HISTORY_RETENTION_DAYS, False),
        ("load_error_log", "slot_time_utc", _config.METEO_FORECAST_HISTORY_RETENTION_DAYS, False),
        ("lp_solution_snapshot", "slot_time_utc", _config.LP_SNAPSHOT_RETENTION_DAYS, False),
        ("lp_inputs_snapshot", "run_at_utc", _config.LP_SNAPSHOT_RETENTION_DAYS, False),
        ("config_audit", "changed_at_utc", _config.CONFIG_AUDIT_RETENTION_DAYS, False),
        # Dispatch + scenario tables ride on the same retention horizon as the
        # LP snapshots they reference (orphaned rows would have nothing useful
        # to point at anyway — the snapshot is the source of truth).
        ("dispatch_decisions", "created_at", _config.LP_SNAPSHOT_RETENTION_DAYS, False),
        ("scenario_solve_log", "solved_at", _config.LP_SNAPSHOT_RETENTION_DAYS, False),
        # Per-day-keyed warning acks (e.g. fox_scheduler_disabled_<date>) are
        # useless once the date rolls over; without this they grow unbounded.
        ("acknowledged_warnings", "acknowledged_at", _config.ACKNOWLEDGED_WARNINGS_RETENTION_DAYS, False),
    ]
    results: dict[str, int] = {}
    try:
        deleted_snapshots, deleted_values = prune_meteo_forecast_snapshots(
            _config.METEO_FORECAST_HISTORY_RETENTION_DAYS
        )
        results["meteo_forecast_snapshot"] = deleted_snapshots
        results["meteo_forecast_value"] = deleted_values
    except Exception as e:
        logger.debug("prune meteo_forecast_snapshot/value failed: %s", e)
        results["meteo_forecast_snapshot"] = -1
        results["meteo_forecast_value"] = -1
    for table, col, days, epoch in policies:
        try:
            results[table] = prune_old_rows(table, col, days, epoch_seconds=epoch)
        except Exception as e:
            logger.debug("prune %s failed: %s", table, e)
            results[table] = -1
    return results


# ── Google Calendar publisher: idempotency state ───────────────────────────


def upsert_calendar_event(
    *,
    calendar_id: str,
    plan_date: str,
    slot_start_utc: str,
    slot_end_utc: str,
    tier: str,
    price_min: float,
    price_max: float,
    price_mean: float,
    google_event_id: str,
) -> None:
    """Idempotent upsert keyed on (calendar_id, slot_start_utc).

    Used by the Google Calendar publisher to remember which Google event ID
    corresponds to which Octopus slot, so subsequent re-publishes update the
    existing event rather than creating duplicates.
    """
    now_iso = datetime.now(UTC).isoformat()
    with _lock:
        conn = get_connection()
        try:
            conn.execute(
                """INSERT INTO calendar_events
                   (calendar_id, plan_date, slot_start_utc, slot_end_utc,
                    tier, price_min, price_max, price_mean, google_event_id,
                    created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(calendar_id, slot_start_utc) DO UPDATE SET
                     slot_end_utc=excluded.slot_end_utc,
                     plan_date=excluded.plan_date,
                     tier=excluded.tier,
                     price_min=excluded.price_min,
                     price_max=excluded.price_max,
                     price_mean=excluded.price_mean,
                     google_event_id=excluded.google_event_id,
                     updated_at=excluded.updated_at""",
                (
                    calendar_id, plan_date, slot_start_utc, slot_end_utc,
                    tier, price_min, price_max, price_mean, google_event_id,
                    now_iso, now_iso,
                ),
            )
            conn.commit()
        finally:
            conn.close()


def upsert_dispatch_decision(
    *,
    run_id: int,
    slot_time_utc: str,
    lp_kind: str,
    dispatched_kind: str,
    committed: bool,
    reason: str,
    scen_optimistic_exp_kwh: float | None = None,
    scen_nominal_exp_kwh: float | None = None,
    scen_pessimistic_exp_kwh: float | None = None,
    export_price_p_kwh: float | None = None,
    refill_price_p_kwh: float | None = None,
    economic_margin_p_kwh: float | None = None,
    outgoing_rate_percentile: float | None = None,
) -> None:
    """Persist one slot's dispatch decision for the audit trail.

    Idempotent on ``(run_id, slot_time_utc)``: re-runs of the same LP run for
    the same slot overwrite the row. Per-scenario export values are nullable
    so non-scenario triggers (drift, forecast_revision) can record decisions
    with only ``scen_nominal_exp_kwh`` populated.
    """
    now_iso = datetime.now(UTC).isoformat()
    with _lock:
        conn = get_connection()
        try:
            conn.execute(
                """INSERT INTO dispatch_decisions
                   (run_id, slot_time_utc, lp_kind, dispatched_kind, committed,
                    reason, scen_optimistic_exp_kwh, scen_nominal_exp_kwh,
                    scen_pessimistic_exp_kwh, export_price_p_kwh,
                    refill_price_p_kwh, economic_margin_p_kwh,
                    outgoing_rate_percentile, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(run_id, slot_time_utc) DO UPDATE SET
                     lp_kind=excluded.lp_kind,
                     dispatched_kind=excluded.dispatched_kind,
                     committed=excluded.committed,
                     reason=excluded.reason,
                     scen_optimistic_exp_kwh=excluded.scen_optimistic_exp_kwh,
                     scen_nominal_exp_kwh=excluded.scen_nominal_exp_kwh,
                     scen_pessimistic_exp_kwh=excluded.scen_pessimistic_exp_kwh,
                     export_price_p_kwh=excluded.export_price_p_kwh,
                     refill_price_p_kwh=excluded.refill_price_p_kwh,
                     economic_margin_p_kwh=excluded.economic_margin_p_kwh,
                     outgoing_rate_percentile=excluded.outgoing_rate_percentile,
                     created_at=excluded.created_at""",
                (
                    run_id, slot_time_utc, lp_kind, dispatched_kind,
                    1 if committed else 0, reason,
                    scen_optimistic_exp_kwh, scen_nominal_exp_kwh,
                    scen_pessimistic_exp_kwh, export_price_p_kwh,
                    refill_price_p_kwh, economic_margin_p_kwh,
                    outgoing_rate_percentile, now_iso,
                ),
            )
            conn.commit()
        finally:
            conn.close()


def get_dispatch_decisions(run_id: int) -> list[dict[str, Any]]:
    """All decision rows for one LP run, ordered by slot_time_utc."""
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                """SELECT run_id, slot_time_utc, lp_kind, dispatched_kind, committed,
                          reason, scen_optimistic_exp_kwh, scen_nominal_exp_kwh,
                          scen_pessimistic_exp_kwh, export_price_p_kwh,
                          refill_price_p_kwh, economic_margin_p_kwh,
                          outgoing_rate_percentile, created_at
                   FROM dispatch_decisions
                   WHERE run_id = ?
                   ORDER BY slot_time_utc ASC""",
                (run_id,),
            )
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()


def get_committed_peak_export_in_range(
    period_from_iso: str,
    period_to_iso: str,
) -> list[dict[str, Any]]:
    """Latest committed peak_export decision per slot in the UTC range.

    Slots can be re-decided across multiple LP runs (each plan re-solve writes a
    new row). For "what did we actually commit?" the most recent ``run_id`` per
    slot is the source of truth.

    Used by the daily-brief forecasted-export fallback: when telemetry export
    is 0 on a day the LP committed peak_export, surface the planned amount so
    the household sees an estimate flagged as forecasted instead of nothing.
    """
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                """SELECT slot_time_utc, lp_kind, committed,
                          scen_pessimistic_exp_kwh, scen_nominal_exp_kwh, run_id
                   FROM dispatch_decisions
                   WHERE slot_time_utc >= ? AND slot_time_utc < ?
                     AND lp_kind = 'peak_export' AND committed = 1
                     AND run_id = (
                         SELECT MAX(run_id) FROM dispatch_decisions d2
                         WHERE d2.slot_time_utc = dispatch_decisions.slot_time_utc
                     )
                   ORDER BY slot_time_utc ASC""",
                (period_from_iso, period_to_iso),
            )
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()


def list_forgone_peak_export_for_day(date_iso: str) -> list[dict[str, Any]]:
    """Per-slot battery export the LP planned but the robustness filter blocked.

    For each local-day slot, picks the LATEST dispatch_decisions row (most
    recent run_id) where the LP itself chose ``lp_kind = 'peak_export'``
    (battery → grid arbitrage) but it did NOT ship as one — i.e. the
    scenario-LP filter (``filter_robust_peak_export``) judged the export unsafe
    under the pessimistic forecast. That is the only forgone revenue there is.

    The ``lp_kind`` filter is load-bearing. Without it the query matched any
    slot with ``export_kwh > 0``, and in the normal/guests presets the LP
    constrains ``exp <= pv_use`` — so ``export_kwh`` there is PV SURPLUS, which
    Fox V3 SelfUse exports passively and which already earns money. Every sunny
    day therefore reported a large phantom "forgone" loss. (Pre-PR-C fossil:
    this used to measure what the removed ``ENERGY_STRATEGY_MODE=strict_savings``
    policy declined to sell.)

    ``date_iso`` is a local-calendar-day string (``YYYY-MM-DD``); the helper
    converts to a half-open UTC slot range matching the LP's slot grid.
    """
    # Convert local day to UTC range. The slot_time_utc strings in
    # dispatch_decisions are ISO with +00:00 — a local-date filter via
    # `substr(..., 1, 10) = date_iso` would mis-classify slots near the
    # DST/midnight boundary. Use a half-open UTC window instead.
    from datetime import datetime as _dt, date as _date, time as _time, timedelta as _td
    from zoneinfo import ZoneInfo
    try:
        d = _date.fromisoformat(date_iso)
    except ValueError:
        return []
    tz = ZoneInfo(config.BULLETPROOF_TIMEZONE)
    local_start = _dt.combine(d, _time.min, tzinfo=tz)
    local_end = local_start + _td(days=1)
    utc_start = local_start.astimezone(UTC).isoformat()
    utc_end = local_end.astimezone(UTC).isoformat()
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                """SELECT dd.slot_time_utc,
                          ls.export_kwh,
                          dd.export_price_p_kwh,
                          dd.dispatched_kind,
                          dd.reason
                   FROM dispatch_decisions dd
                   JOIN lp_solution_snapshot ls
                     ON ls.run_id = dd.run_id
                    AND ls.slot_time_utc = dd.slot_time_utc
                   WHERE dd.slot_time_utc >= ? AND dd.slot_time_utc < ?
                     AND ls.export_kwh > 0
                     AND dd.lp_kind = 'peak_export'
                     AND dd.dispatched_kind != 'peak_export'
                     AND dd.run_id = (
                         SELECT MAX(run_id) FROM dispatch_decisions d2
                         WHERE d2.slot_time_utc = dd.slot_time_utc
                     )
                   ORDER BY dd.slot_time_utc ASC""",
                (utc_start, utc_end),
            )
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()


def update_execution_log_metered(
    slot_start_utc: str,
    consumption_kwh: float,
) -> bool:
    """Rewrite (or synthesise) an ``execution_log`` row with metered consumption.

    The heartbeat writes rows with ``source="estimated"`` based on a single
    ``Fox.load_power`` sample × 0.5 h — fast enough for the live cockpit but
    too noisy for the daily-brief PnL (one heavy appliance running mid-slot
    skews the slot's apparent consumption by ~75 %). The nightly consumption
    backfill job replaces those estimates with the actual half-hourly meter
    reading from Octopus, recomputing all four cost columns from the prices
    that were already locked in at write time.

    ``slot_start_utc`` is the half-hour-aligned slot start as written by
    Octopus (no microseconds). The heartbeat row's ``timestamp`` is at full
    microsecond precision wherever the heartbeat happened to fire within the
    slot, so we match on the **half-hour bucket** rather than exact equality.

    **Missing-heartbeat fallback (issue #199):** if no heartbeat row exists
    in the bucket (service was down, restart spanned the slot, etc.), this
    inserts a synthetic row tagged ``source="metered_synthetic"`` provided
    we can resolve an ``agile_rates`` row covering the slot. SVT and fixed
    shadow prices come from the same helpers the heartbeat uses. Without
    the synthetic insert the night-brief PnL silently under-reports cost
    by exactly the missing slots' contribution.

    Returns True on either an UPDATE of an existing heartbeat row or an
    INSERT of a synthetic one. Returns False only when the slot has neither
    a heartbeat row nor a published ``agile_rates`` price (truly no data).
    Idempotent on re-run.
    """
    # Normalise the input to a half-hour boundary, then build a [start, end)
    # window the heartbeat row's microsecond-precision timestamp falls within.
    try:
        slot_dt = datetime.fromisoformat(slot_start_utc.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return False
    slot_start_iso = slot_dt.replace(microsecond=0).isoformat()
    slot_end_iso = (slot_dt + timedelta(minutes=30)).replace(microsecond=0).isoformat()
    kwh = float(consumption_kwh)

    with _lock:
        conn = get_connection()
        try:
            # Issue #306 follow-up: SELECT all rows in the slot window, not just
            # the first. The legacy LIMIT 1 left straggler heartbeat rows (e.g.
            # at 01:35:50 within a 01:30 slot) intact, double-counting in any
            # SUM-over-execution_log path. Updating one + deleting the rest
            # leaves exactly one canonical metered row per slot.
            cur = conn.execute(
                """SELECT id, agile_price_pence, svt_shadow_price_pence,
                          fixed_shadow_price_pence, source
                   FROM execution_log
                   WHERE timestamp >= ? AND timestamp < ?
                   ORDER BY timestamp ASC""",
                (slot_start_iso, slot_end_iso),
            )
            rows = cur.fetchall()
            if rows:
                primary = rows[0]
                stragglers = rows[1:]
                agile = float(primary["agile_price_pence"] or 0.0)
                svt = float(primary["svt_shadow_price_pence"] or 0.0)
                fixed = float(primary["fixed_shadow_price_pence"] or 0.0)
                # Preserve 'metered_synthetic' lineage across idempotent re-runs;
                # heartbeat-overlay rows otherwise become 'metered'.
                next_source = (
                    "metered_synthetic"
                    if primary["source"] == "metered_synthetic"
                    else "metered"
                )
                conn.execute(
                    """UPDATE execution_log SET
                           consumption_kwh = ?,
                           cost_realised_pence = ?,
                           cost_svt_shadow_pence = ?,
                           cost_fixed_shadow_pence = ?,
                           delta_vs_svt_pence = ?,
                           delta_vs_fixed_pence = ?,
                           source = ?
                       WHERE id = ?""",
                    (
                        kwh,
                        kwh * agile,
                        kwh * svt,
                        kwh * fixed,
                        kwh * (svt - agile),
                        kwh * (fixed - agile),
                        next_source,
                        int(primary["id"]),
                    ),
                )
                if stragglers:
                    placeholders = ",".join("?" for _ in stragglers)
                    conn.execute(
                        f"DELETE FROM execution_log WHERE id IN ({placeholders})",
                        tuple(int(r["id"]) for r in stragglers),
                    )
                    logger.debug(
                        "consumption_backfill: deduped %d straggler row(s) for slot %s",
                        len(stragglers), slot_start_iso,
                    )
                conn.commit()
                return True

            # No heartbeat row in this bucket — synthesise one so the night
            # brief's PnL is complete. Look up the Agile rate first; if no
            # tariff slot covers this timestamp we genuinely have no price
            # data and bail.
            from .config import config as _config
            tariff = (_config.OCTOPUS_TARIFF_CODE or "").strip()
            if not tariff:
                return False
            # agile_rates stores timestamps with the 'Z' suffix (see
            # get_rates_for_period); normalise our slot start to match so the
            # lexicographic compare works.
            slot_start_z = slot_dt.replace(microsecond=0).isoformat().replace(
                "+00:00", "Z"
            )
            rate_cur = conn.execute(
                """SELECT value_inc_vat
                   FROM agile_rates
                   WHERE tariff_code = ?
                     AND valid_from <= ?
                     AND valid_to > ?
                   ORDER BY valid_from DESC
                   LIMIT 1""",
                (tariff, slot_start_z, slot_start_z),
            )
            rate_row = rate_cur.fetchone()
            if not rate_row:
                return False
            agile = float(rate_row["value_inc_vat"])
            from .analytics.shadow_pricing import (
                fixed_shadow_rate_pence,
                svt_rate_pence,
            )
            svt = svt_rate_pence()
            fixed = fixed_shadow_rate_pence()
            conn.execute(
                """INSERT INTO execution_log
                   (timestamp, consumption_kwh, agile_price_pence,
                    svt_shadow_price_pence, fixed_shadow_price_pence,
                    cost_realised_pence, cost_svt_shadow_pence,
                    cost_fixed_shadow_pence, delta_vs_svt_pence,
                    delta_vs_fixed_pence, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'metered_synthetic')""",
                (
                    slot_start_iso, kwh, agile, svt, fixed,
                    kwh * agile, kwh * svt, kwh * fixed,
                    kwh * (svt - agile), kwh * (fixed - agile),
                ),
            )
            conn.commit()
            return True
        finally:
            conn.close()


def find_latest_optimizer_run_id() -> int | None:
    """Return the id of the most recent ``optimizer_log`` row, or None.

    Helper used by the API/MCP layer when callers ask for the "latest" run
    rather than naming a specific run_id.
    """
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                "SELECT id FROM optimizer_log ORDER BY run_at DESC LIMIT 1"
            )
            r = cur.fetchone()
            return int(r[0]) if r else None
        finally:
            conn.close()


def upsert_scenario_solve_log(
    *,
    batch_id: int,
    nominal_run_id: int,
    scenario_kind: str,
    lp_status: str,
    objective_pence: float | None,
    perturbation_temp_delta_c: float,
    perturbation_load_factor: float,
    perturbation_pv_factor: float = 1.0,
    peak_export_slot_count: int | None = None,
    duration_ms: int | None = None,
    error: str | None = None,
) -> None:
    """Persist one scenario's solve summary.

    Idempotent on ``(batch_id, scenario_kind)``: re-runs of the same batch
    overwrite. ``batch_id`` is conventionally equal to ``nominal_run_id`` —
    every successful 3-pass solve logs three rows sharing those two ids.
    Look up by ``batch_id`` to retrieve the full batch.
    """
    now_iso = datetime.now(UTC).isoformat()
    with _lock:
        conn = get_connection()
        try:
            conn.execute(
                """INSERT INTO scenario_solve_log
                   (batch_id, nominal_run_id, scenario_kind, lp_status,
                    objective_pence, perturbation_temp_delta_c,
                    perturbation_load_factor, perturbation_pv_factor,
                    peak_export_slot_count, duration_ms, error, solved_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(batch_id, scenario_kind) DO UPDATE SET
                     nominal_run_id=excluded.nominal_run_id,
                     lp_status=excluded.lp_status,
                     objective_pence=excluded.objective_pence,
                     perturbation_temp_delta_c=excluded.perturbation_temp_delta_c,
                     perturbation_load_factor=excluded.perturbation_load_factor,
                     perturbation_pv_factor=excluded.perturbation_pv_factor,
                     peak_export_slot_count=excluded.peak_export_slot_count,
                     duration_ms=excluded.duration_ms,
                     error=excluded.error,
                     solved_at=excluded.solved_at""",
                (
                    batch_id, nominal_run_id, scenario_kind, lp_status,
                    objective_pence, perturbation_temp_delta_c,
                    perturbation_load_factor, perturbation_pv_factor,
                    peak_export_slot_count, duration_ms, error, now_iso,
                ),
            )
            conn.commit()
        finally:
            conn.close()


def get_scenario_solve_batch(batch_id: int) -> list[dict[str, Any]]:
    """All scenarios in one batch, ordered optimistic → nominal → pessimistic."""
    order = "CASE scenario_kind WHEN 'optimistic' THEN 0 WHEN 'nominal' THEN 1 WHEN 'pessimistic' THEN 2 ELSE 3 END"
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                f"""SELECT batch_id, nominal_run_id, scenario_kind, lp_status,
                          objective_pence, perturbation_temp_delta_c,
                          perturbation_load_factor, peak_export_slot_count,
                          duration_ms, error, solved_at
                   FROM scenario_solve_log
                   WHERE batch_id = ?
                   ORDER BY {order}""",
                (batch_id,),
            )
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Smart appliance scheduling — DAL (V16)
# ---------------------------------------------------------------------------

_APPLIANCE_UPDATABLE_FIELDS = {
    "name", "device_type", "default_duration_minutes",
    "deadline_local_time", "typical_kw", "enabled",
}

_APPLIANCE_JOB_UPDATABLE_FIELDS = {
    "status", "planned_start_utc", "planned_end_utc",
    "avg_price_pence", "actual_start_utc", "error_msg",
    "last_replan_at_utc", "duration_minutes", "deadline_utc",
    "completed_at_utc",
    "energy_start_wh",
    "actual_kwh",
}


def add_appliance(
    *,
    vendor: str,
    vendor_device_id: str,
    name: str,
    device_type: str,
    default_duration_minutes: int = 120,
    deadline_local_time: str = "07:00",
    typical_kw: float = 0.5,
    enabled: bool = True,
) -> int:
    """Insert a managed appliance row. Returns the new id.

    Idempotent on (vendor, vendor_device_id) — re-inserting the same device
    raises sqlite3.IntegrityError so the caller can decide between update
    or "already registered".
    """
    now = datetime.now(UTC).isoformat()
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                """INSERT INTO appliances
                   (vendor, vendor_device_id, name, device_type,
                    default_duration_minutes, deadline_local_time, typical_kw,
                    enabled, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (vendor, vendor_device_id, name, device_type,
                 int(default_duration_minutes), deadline_local_time,
                 float(typical_kw), 1 if enabled else 0, now),
            )
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()


def list_appliances(*, enabled_only: bool = False) -> list[dict[str, Any]]:
    """Return all appliances ordered by id ASC."""
    with _lock:
        conn = get_connection()
        try:
            sql = "SELECT * FROM appliances"
            if enabled_only:
                sql += " WHERE enabled = 1"
            sql += " ORDER BY id ASC"
            cur = conn.execute(sql)
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()


def get_appliance(appliance_id: int) -> dict[str, Any] | None:
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                "SELECT * FROM appliances WHERE id = ?", (appliance_id,)
            )
            r = cur.fetchone()
            return dict(r) if r else None
        finally:
            conn.close()


def appliance_learned_typical_kw(
    appliance_id: int, *, lookback_n: int = 10, min_samples: int = 3
) -> tuple[float, int] | None:
    """Rolling-mean power (kW) learned from measured cycle energy (#222).

    From the most recent ``lookback_n`` completed jobs with a real
    ``actual_kwh`` (SmartThings energy counter, #235), per-job
    ``kW = actual_kwh / (duration_minutes/60)``. Returns ``(mean_kw,
    n_samples)`` once ``n >= min_samples``, else ``None`` so the caller falls
    back to the static registration ``typical_kw``.
    """
    with _lock:
        conn = get_connection()
        try:
            rows = conn.execute(
                """SELECT actual_kwh, duration_minutes FROM appliance_jobs
                   WHERE appliance_id = ? AND status = 'completed'
                     AND actual_kwh IS NOT NULL AND actual_kwh > 0
                     AND duration_minutes IS NOT NULL AND duration_minutes > 0
                   ORDER BY id DESC LIMIT ?""",
                (int(appliance_id), max(1, int(lookback_n))),
            ).fetchall()
        finally:
            conn.close()
    kws: list[float] = []
    for r in rows:
        try:
            a = float(r["actual_kwh"])
            d = float(r["duration_minutes"])
            if a > 0 and d > 0:
                kws.append(a / (d / 60.0))
        except (TypeError, ValueError, KeyError):
            continue
    if len(kws) < int(min_samples):
        return None
    return (round(sum(kws) / len(kws), 4), len(kws))


def update_appliance(appliance_id: int, **fields: Any) -> bool:
    """Update one or more fields on an appliance. Returns True if a row was
    updated. Silently ignores keys not in :data:`_APPLIANCE_UPDATABLE_FIELDS`."""
    sets = []
    vals: list[Any] = []
    for k, v in fields.items():
        if k not in _APPLIANCE_UPDATABLE_FIELDS:
            continue
        sets.append(f"{k} = ?")
        if k == "enabled":
            vals.append(1 if v else 0)
        elif k == "default_duration_minutes":
            vals.append(int(v))
        elif k == "typical_kw":
            vals.append(float(v))
        else:
            vals.append(v)
    if not sets:
        return False
    vals.append(appliance_id)
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                f"UPDATE appliances SET {', '.join(sets)} WHERE id = ?", vals
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


def set_appliance_rearm_block(appliance_id: int, blocked: bool) -> None:
    """Set/clear the re-arm latch (see the ``rearm_block_until_off`` migration).

    True after a cycle reaches a terminal state with Smart Control still on;
    cleared the moment Smart Control is observed off, so a fresh off→on manual
    arm is required before the appliance runs the same load again.
    """
    with _lock:
        conn = get_connection()
        try:
            conn.execute(
                "UPDATE appliances SET rearm_block_until_off = ? WHERE id = ?",
                (1 if blocked else 0, appliance_id),
            )
            conn.commit()
        finally:
            conn.close()


def is_appliance_rearm_blocked(appliance_id: int) -> bool:
    """True when the appliance must see a fresh off→on toggle before re-arming."""
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                "SELECT rearm_block_until_off FROM appliances WHERE id = ?",
                (appliance_id,),
            )
            r = cur.fetchone()
            return bool(r[0]) if r else False
        finally:
            conn.close()


def delete_appliance(appliance_id: int) -> bool:
    with _lock:
        conn = get_connection()
        try:
            # Cascade: drop any existing jobs first so the FK doesn't bite.
            conn.execute(
                "DELETE FROM appliance_jobs WHERE appliance_id = ?",
                (appliance_id,),
            )
            cur = conn.execute(
                "DELETE FROM appliances WHERE id = ?", (appliance_id,)
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


def get_active_appliance_job(appliance_id: int) -> dict[str, Any] | None:
    """Return the row in status ('scheduled', 'running') for this appliance,
    or None. The partial unique index guarantees at most one such row."""
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                """SELECT * FROM appliance_jobs
                   WHERE appliance_id = ? AND status IN ('scheduled', 'running')
                   LIMIT 1""",
                (appliance_id,),
            )
            r = cur.fetchone()
            return dict(r) if r else None
        finally:
            conn.close()


def create_appliance_job(
    *,
    appliance_id: int,
    armed_at_utc: str,
    deadline_utc: str,
    duration_minutes: int,
    planned_start_utc: str,
    planned_end_utc: str,
    avg_price_pence: float | None = None,
    last_replan_at_utc: str | None = None,
    status: str = "scheduled",
) -> int:
    now = datetime.now(UTC).isoformat()
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                """INSERT INTO appliance_jobs
                   (appliance_id, status, armed_at_utc, deadline_utc,
                    duration_minutes, planned_start_utc, planned_end_utc,
                    avg_price_pence, last_replan_at_utc, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    appliance_id, status, armed_at_utc, deadline_utc,
                    int(duration_minutes), planned_start_utc, planned_end_utc,
                    avg_price_pence,
                    last_replan_at_utc or armed_at_utc, now, now,
                ),
            )
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()


def get_appliance_job(job_id: int) -> dict[str, Any] | None:
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                "SELECT * FROM appliance_jobs WHERE id = ?", (job_id,)
            )
            r = cur.fetchone()
            return dict(r) if r else None
        finally:
            conn.close()


def update_appliance_job(job_id: int, **fields: Any) -> bool:
    """Update one or more mutable fields on an appliance job. ``updated_at``
    is set automatically. Returns True if a row was updated."""
    sets = ["updated_at = ?"]
    vals: list[Any] = [datetime.now(UTC).isoformat()]
    for k, v in fields.items():
        if k not in _APPLIANCE_JOB_UPDATABLE_FIELDS:
            continue
        sets.append(f"{k} = ?")
        if k in ("duration_minutes",):
            vals.append(int(v) if v is not None else None)
        elif k == "avg_price_pence":
            vals.append(float(v) if v is not None else None)
        else:
            vals.append(v)
    vals.append(job_id)
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                f"UPDATE appliance_jobs SET {', '.join(sets)} WHERE id = ?",
                vals,
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


def get_appliance_jobs(
    *,
    from_utc: str | None = None,
    to_utc: str | None = None,
    status: str | None = None,
    appliance_id: int | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Query appliance_jobs by status/window. ``from_utc`` and ``to_utc`` bound
    ``planned_start_utc`` (inclusive lower, exclusive upper). Sorted descending
    by planned_start_utc so the most recent / next-up are first."""
    where: list[str] = []
    vals: list[Any] = []
    if status is not None:
        where.append("status = ?")
        vals.append(status)
    if appliance_id is not None:
        where.append("appliance_id = ?")
        vals.append(int(appliance_id))
    if from_utc is not None:
        where.append("planned_start_utc >= ?")
        vals.append(from_utc)
    if to_utc is not None:
        where.append("planned_start_utc < ?")
        vals.append(to_utc)
    sql = "SELECT * FROM appliance_jobs"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY planned_start_utc DESC LIMIT ?"
    vals.append(int(limit))
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(sql, vals)
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()


def get_active_appliance_jobs_overlapping(
    *, from_utc: str, to_utc: str
) -> list[dict[str, Any]]:
    """Return rows in status ('scheduled', 'running') whose
    [planned_start_utc, planned_end_utc] overlaps [from_utc, to_utc).

    Used by the LP residual-load profile to bump load on slots covered by
    armed sessions.
    """
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                """SELECT j.*, a.typical_kw AS appliance_typical_kw
                   FROM appliance_jobs j
                   JOIN appliances a ON a.id = j.appliance_id
                   WHERE j.status IN ('scheduled', 'running')
                     AND j.planned_start_utc < ?
                     AND j.planned_end_utc > ?
                   ORDER BY j.planned_start_utc""",
                (to_utc, from_utc),
            )
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Daikin LWT → kW calibration (replaces hardcoded _KW_PER_DEGC_LWT in physics)
# ---------------------------------------------------------------------------

def get_daikin_lwt_kw_calibration() -> dict[str, Any] | None:
    """Return the latest Daikin LWT→kW calibration row, or ``None`` when empty.

    Loader keeps this cheap so :func:`src.physics.get_kw_per_degc_lwt` can call
    it on every LP slot evaluation without DB pressure (single-row table,
    primary-key seek, < 0.1 ms).
    """
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                """SELECT id, k_per_degc, samples, window_days,
                          rmse_kwh, bias_kwh, computed_at
                   FROM daikin_lwt_kw_calibration WHERE id = 1"""
            )
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


def upsert_daikin_lwt_kw_calibration(
    *,
    k_per_degc: float,
    samples: int,
    window_days: int,
    rmse_kwh: float | None = None,
    bias_kwh: float | None = None,
) -> None:
    """Replace the single row in ``daikin_lwt_kw_calibration``."""
    from datetime import UTC as _UTC, datetime as _dt
    now = _dt.now(_UTC).isoformat()
    with _lock:
        conn = get_connection()
        try:
            conn.execute(
                """INSERT OR REPLACE INTO daikin_lwt_kw_calibration
                   (id, k_per_degc, samples, window_days,
                    rmse_kwh, bias_kwh, computed_at)
                   VALUES (1, ?, ?, ?, ?, ?, ?)""",
                (
                    float(k_per_degc),
                    int(samples),
                    int(window_days),
                    float(rmse_kwh) if rmse_kwh is not None else None,
                    float(bias_kwh) if bias_kwh is not None else None,
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()


def compute_daikin_lwt_kw_calibration(
    *,
    window_days: int = 30,
    min_samples: int = 7,
) -> dict[str, Any]:
    """Regress observed kwh_heating against Σ max(0, LWT(t_out) − 18) · Δt.

    For each day in the rolling window, joins ``daikin_consumption_daily``
    (kwh_heating, source-of-truth post-Phase-A backfill) with the per-hour
    outdoor-temperature series from ``meteo_forecast_value`` (most-recent
    snapshot covering each UTC hour of that day). The LWT curve in
    :func:`src.physics.get_lwt_base_c` converts each hourly outdoor temp into
    an LWT; the daily integrand ``X_day = Σ_h max(0, LWT_h − 18)`` (one-hour
    slots, so units are °C·h) regresses linearly against ``y_day = kwh_heating``.

    Least squares through the origin: ``k = Σ(X · y) / Σ(X²)`` — the LP's
    space-heating constraint is also through-origin (zero LWT delta → zero
    draw), so a fitted intercept would not be physically meaningful.

    Returns a status dict so the caller (cron) can log how the recompute went.
    Skips silently (status='skipped' with a reason) on missing data; never
    raises. The caller decides whether to upsert based on ``status == 'ok'``.
    """
    from datetime import UTC as _UTC, datetime as _dt, date as _date, timedelta as _td
    from .physics import get_lwt_base_c

    end_day = _date.today() - _td(days=1)  # yesterday — today's heating row is incomplete
    start_day = end_day - _td(days=window_days - 1)

    # Pull observed kwh_heating per day in window. Skip rows lacking heating
    # data (kwh_heating IS NULL or 0 with kwh_total > 0 — usually a backfill
    # gap; not a real "no heating today" signal).
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                """SELECT date, kwh_heating, kwh_total
                   FROM daikin_consumption_daily
                   WHERE date BETWEEN ? AND ?
                     AND kwh_heating IS NOT NULL""",
                (start_day.isoformat(), end_day.isoformat()),
            )
            obs_rows = cur.fetchall()
        finally:
            conn.close()

    if not obs_rows:
        return {
            "status": "skipped",
            "reason": "no daikin_consumption_daily rows in window",
            "window_days": window_days,
            "samples": 0,
        }

    # Per-day outdoor integral. For each historical day we want the freshest
    # value per slot_time across BOTH meteo tables:
    #   - meteo_forecast_value retains ~7 days (current canonical lookups)
    #   - meteo_forecast_history retains ≥ 14 days (long-term audit)
    # UNIONing them lets the rolling window go beyond _value's pruning cutoff.
    # The MAX-fetched-value-per-slot subquery (correlated on slot_time, NOT on
    # the day-of-slot) is what makes the outer query yield ALL covered slots
    # of the day — earlier versions correlated on the day, which collapsed
    # to whichever single snapshot had the latest fetch with any slot in
    # that day, dropping coverage to a handful of hours for older days.
    # Decontamination (#540): days where HEM itself commanded a non-zero LWT
    # offset must not train k. X_day is computed from the NATURAL curve while
    # y_day (measured heating) includes the offset-induced draw, so offset
    # days bias k upward — observed June 2026: k drifted 0.033 → 0.067
    # learning from the very heating the offsets caused.
    contaminated_days: set[str] = set()
    try:
        for s_iso, e_iso in get_nonzero_lwt_offset_windows(
            start_day.isoformat(), end_day.isoformat()
        ):
            contaminated_days.add(s_iso[:10])
            contaminated_days.add(e_iso[:10])  # midnight-crossing windows
    except Exception:  # pragma: no cover — decontamination is best-effort
        logger.exception("lwt k-calibration: offset-window lookup failed")

    pairs: list[tuple[float, float, str]] = []  # (X_day, y_day, date)
    skipped_no_meteo: list[str] = []
    skipped_outlier: list[str] = []
    skipped_hem_offset: list[str] = []
    with _lock:
        conn = get_connection()
        try:
            for row in obs_rows:
                d = str(row["date"])
                if d in contaminated_days:
                    skipped_hem_offset.append(d)
                    continue
                kwh_heating = float(row["kwh_heating"])
                cur = conn.execute(
                    """SELECT slot_time, temp_c FROM (
                           SELECT slot_time, temp_c, forecast_fetch_at_utc
                             FROM meteo_forecast_history
                            WHERE substr(slot_time, 1, 10) = ?
                              AND temp_c IS NOT NULL
                           UNION ALL
                           SELECT slot_time, temp_c, forecast_fetch_at_utc
                             FROM meteo_forecast_value
                            WHERE substr(slot_time, 1, 10) = ?
                              AND temp_c IS NOT NULL
                       ) AS u
                       WHERE forecast_fetch_at_utc = (
                           SELECT MAX(f) FROM (
                               SELECT forecast_fetch_at_utc AS f
                                 FROM meteo_forecast_history
                                WHERE slot_time = u.slot_time AND temp_c IS NOT NULL
                               UNION ALL
                               SELECT forecast_fetch_at_utc AS f
                                 FROM meteo_forecast_value
                                WHERE slot_time = u.slot_time AND temp_c IS NOT NULL
                           )
                       )
                       GROUP BY slot_time""",
                    (d, d),
                )
                slot_temps = cur.fetchall()
                # Need ≥ 12 hours = ≥ 24 half-hour slots for the day to count.
                # Half-hourly cadence is the canonical resolution; older fetches
                # are sometimes hourly only (= 12 rows = 12 hours), so accept
                # ≥ 12 rows as a fallback.
                if len(slot_temps) < 12:
                    skipped_no_meteo.append(d)
                    continue
                # X_day in °C·h. Δt per row depends on the snapshot cadence:
                #   24 rows  ⇒ hourly,    Δt = 1 h
                #   48 rows  ⇒ half-hour, Δt = 0.5 h
                # Use the inverse of the row count over 24 hours so we honour
                # whichever cadence the snapshot used.
                slot_h = 24.0 / max(1, len(slot_temps))
                x_day = sum(
                    max(0.0, get_lwt_base_c(float(rr["temp_c"])) - 18.0)
                    for rr in slot_temps
                ) * slot_h
                if x_day <= 0.0 and kwh_heating > 0.5:
                    # Warm day predicted but real heating consumption — likely
                    # DHW mis-attribution or anomalous setpoint. Drop.
                    skipped_outlier.append(d)
                    continue
                pairs.append((x_day, kwh_heating, d))
        finally:
            conn.close()

    if len(pairs) < min_samples:
        return {
            "status": "skipped",
            "reason": f"only {len(pairs)} usable day(s); need ≥ {min_samples}",
            "window_days": window_days,
            "samples": len(pairs),
            "skipped_no_meteo": len(skipped_no_meteo),
            "skipped_outlier": len(skipped_outlier),
        }

    # Outlier filter: drop days where observed/predicted ratio (using the
    # default k as anchor) is > 3× or < 1/3×. The remaining set fits k
    # robustly without leverage from anomalous days (open windows, party
    # events, sensor glitches).
    from .physics import _KW_PER_DEGC_LWT_DEFAULT
    anchored = [
        (x, y, d) for x, y, d in pairs
        if x > 0 and 1.0 / 3.0 <= (y / max(1e-9, x * _KW_PER_DEGC_LWT_DEFAULT)) <= 3.0
    ]
    if len(anchored) < min_samples:
        return {
            "status": "skipped",
            "reason": f"after outlier filter only {len(anchored)} day(s); need ≥ {min_samples}",
            "window_days": window_days,
            "samples": len(anchored),
            "raw_pairs": len(pairs),
        }

    # Least-squares through origin: k = Σ(x·y) / Σ(x²)
    sxy = sum(x * y for x, y, _ in anchored)
    sxx = sum(x * x for x, _, _ in anchored)
    if sxx <= 0:
        return {"status": "skipped", "reason": "zero variance in X"}
    k = sxy / sxx

    # Residual stats for audit
    residuals = [y - k * x for x, y, _ in anchored]
    rmse = (sum(r * r for r in residuals) / len(residuals)) ** 0.5
    bias = sum(residuals) / len(residuals)

    return {
        "status": "ok",
        "k_per_degc": float(k),
        "samples": len(anchored),
        "window_days": window_days,
        "rmse_kwh": float(rmse),
        "bias_kwh": float(bias),
        "raw_pairs": len(pairs),
        "skipped_no_meteo": len(skipped_no_meteo),
        "skipped_outlier_pre": len(skipped_outlier),
        "skipped_hem_offset": len(skipped_hem_offset),
        "outliers_filtered": len(pairs) - len(anchored),
    }


def get_building_thermal_calibration() -> dict[str, Any] | None:
    """Latest W2 building thermal calibration row, or ``None`` when empty.
    Single-row PK seek — cheap enough for the estimator's per-call readers."""
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                "SELECT * FROM building_thermal_calibration WHERE id = 1"
            )
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()


_THERMAL_CAL_COLS = (
    "tau_hours", "tau_r2_median", "tau_episodes", "tau_window_days",
    "tau_computed_at", "ua_w_per_k", "ua_r2", "ua_samples", "ua_window_days",
    "ua_assumed_cop", "ua_source", "ua_computed_at", "c_kwh_per_k", "c_source",
)


def set_thermal_learning_progress(result: dict[str, Any]) -> None:
    """Persist the learner's last-run summary (W2 observability, #540)."""
    with _lock:
        conn = get_connection()
        try:
            conn.execute(
                """INSERT OR REPLACE INTO thermal_learning_progress
                   (id, updated_at, result_json) VALUES (1, ?, ?)""",
                (datetime.now(UTC).isoformat(), json.dumps(result, default=str)),
            )
            conn.commit()
        finally:
            conn.close()


def get_thermal_learning_progress() -> dict[str, Any] | None:
    """The learner's last-run summary + when it ran (None before the first run)."""
    with _lock:
        conn = get_connection()
        try:
            r = conn.execute(
                "SELECT updated_at, result_json FROM thermal_learning_progress WHERE id = 1"
            ).fetchone()
            if not r:
                return None
            try:
                res = json.loads(r["result_json"])
            except (TypeError, ValueError):
                res = None
            return {"updated_at": r["updated_at"], "result": res}
        finally:
            conn.close()


def upsert_building_thermal_calibration(fields: dict[str, Any]) -> None:
    """Replace the single ``building_thermal_calibration`` row. ``fields`` may
    carry any subset of the columns (the W2 refresh merges skipped components
    from the prior row before calling); unknown keys are ignored."""
    from datetime import UTC as _UTC, datetime as _dt
    vals = [fields.get(c) for c in _THERMAL_CAL_COLS]
    with _lock:
        conn = get_connection()
        try:
            conn.execute(
                f"""INSERT OR REPLACE INTO building_thermal_calibration
                    (id, {', '.join(_THERMAL_CAL_COLS)}, computed_at)
                    VALUES (1, {', '.join('?' * len(_THERMAL_CAL_COLS))}, ?)""",
                (*vals, _dt.now(_UTC).isoformat()),
            )
            conn.commit()
        finally:
            conn.close()


def get_meteo_temps_range(start_day_iso: str, end_day_iso: str) -> list[tuple[str, float]]:
    """Freshest outdoor ``(slot_time, temp_c)`` per slot over an inclusive
    UTC-day range, from the ``meteo_forecast_value ∪ meteo_forecast_history``
    union (same freshest-per-slot rule as
    :func:`compute_daikin_lwt_kw_calibration`, for the W2 thermal learner's
    outdoor series). ONE range scan + a Python last-wins pass — the per-day
    correlated-subquery variant cost ~0.4 s/day under the global lock, which
    would have made a 30-day nightly refresh a multi-second lock hold."""
    start = start_day_iso
    end_excl = (date.fromisoformat(end_day_iso) + timedelta(days=1)).isoformat()
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                """SELECT slot_time, temp_c, forecast_fetch_at_utc
                     FROM meteo_forecast_history
                    WHERE slot_time >= ? AND slot_time < ? AND temp_c IS NOT NULL
                   UNION ALL
                   SELECT slot_time, temp_c, forecast_fetch_at_utc
                     FROM meteo_forecast_value
                    WHERE slot_time >= ? AND slot_time < ? AND temp_c IS NOT NULL
                   ORDER BY slot_time, forecast_fetch_at_utc""",
                (start, end_excl, start, end_excl),
            )
            freshest: dict[str, float] = {}
            for r in cur.fetchall():
                freshest[str(r["slot_time"])] = float(r["temp_c"])  # last = freshest
            return sorted(freshest.items())
        finally:
            conn.close()


def refresh_daikin_lwt_kw_calibration(*, log_min_delta_pct: float = 1.0) -> dict[str, Any]:
    """Recompute and upsert the calibration; called from the LP solve path.

    Cheap enough to run on every LP solve (~30 daily rows, single regression).
    Logging is gated to fires of substance: status changes (ok ↔ skipped) or
    a >``log_min_delta_pct``% shift in ``k_per_degc`` from the prior row.
    Other solves stay quiet so the journald stream isn't drowned in identical
    "calibration unchanged" lines across 24+ daily solves.
    """
    prev = get_daikin_lwt_kw_calibration()
    result = compute_daikin_lwt_kw_calibration()
    if result.get("status") != "ok":
        if prev is None:
            # First run, no data yet — log once at INFO so cold-start is visible
            logger.info(
                "daikin_lwt_calibration: %s (reason=%s, samples=%s) — "
                "loader will use module default until enough data accumulates",
                result.get("status"), result.get("reason"), result.get("samples"),
            )
        return result
    new_k = float(result["k_per_degc"])
    upsert_daikin_lwt_kw_calibration(
        k_per_degc=new_k,
        samples=int(result["samples"]),
        window_days=int(result["window_days"]),
        rmse_kwh=float(result.get("rmse_kwh") or 0.0),
        bias_kwh=float(result.get("bias_kwh") or 0.0),
    )
    prev_k = float(prev["k_per_degc"]) if prev else None
    if prev_k is None:
        logger.info(
            "daikin_lwt_calibration: first fit k=%.5f kW/°C (default 0.0333); "
            "samples=%d window=%dd rmse=%.2f kWh bias=%+0.2f kWh outliers_filtered=%d",
            new_k, result["samples"], result["window_days"],
            result.get("rmse_kwh") or 0.0, result.get("bias_kwh") or 0.0,
            result.get("outliers_filtered", 0),
        )
    else:
        delta_pct = abs(new_k - prev_k) / max(1e-9, prev_k) * 100.0
        if delta_pct >= log_min_delta_pct:
            logger.info(
                "daikin_lwt_calibration: k=%.5f kW/°C (was %.5f, %+0.1f%%); "
                "samples=%d rmse=%.2f kWh bias=%+0.2f kWh",
                new_k, prev_k, (new_k - prev_k) / max(1e-9, prev_k) * 100.0,
                result["samples"], result.get("rmse_kwh") or 0.0,
                result.get("bias_kwh") or 0.0,
            )
    return result
