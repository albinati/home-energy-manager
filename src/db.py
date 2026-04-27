"""SQLite persistence for Bulletproof Energy Manager (thread-safe).

Uses stdlib sqlite3 for compatibility with APScheduler and sync device clients.
"""
from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import threading
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
    UNIQUE(slot_time)
);

CREATE TABLE IF NOT EXISTS pnl_execution_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slot_time TEXT NOT NULL,
    kwh_consumed REAL,
    agile_price_pence REAL,
    svt_price_pence REAL,
    delta_pence REAL
);

CREATE INDEX IF NOT EXISTS idx_pnl_execution_log_slot ON pnl_execution_log(slot_time);

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

    # Phase 4.3: user-override marker on action_schedule rows.
    cur = conn.execute("PRAGMA table_info(action_schedule)")
    as_cols = {str(r[1]) for r in cur.fetchall()}
    if "overridden_by_user_at" not in as_cols:
        conn.execute("ALTER TABLE action_schedule ADD COLUMN overridden_by_user_at TEXT")

    # V2: meteo_forecast and pnl_execution_log tables (may already exist via SCHEMA)
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
    conn.execute(
        """CREATE TABLE IF NOT EXISTS pnl_execution_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slot_time TEXT NOT NULL,
            kwh_consumed REAL,
            agile_price_pence REAL,
            svt_price_pence REAL,
            delta_pence REAL
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_pnl_execution_log_slot ON pnl_execution_log(slot_time)"
    )

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
            config_snapshot_json     TEXT,
            price_quantize_p         REAL,
            peak_threshold_p         REAL,
            cheap_threshold_p        REAL,
            daikin_control_mode      TEXT,
            optimization_preset      TEXT,
            energy_strategy_mode     TEXT
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_lp_inputs_snapshot_plan_date ON lp_inputs_snapshot(plan_date)"
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


def get_agile_rates_daily_summary(
    tariff_code: str,
    start_date: str,
    end_date: str,
) -> list[dict[str, Any]]:
    """Per-day aggregate stats for the given tariff and date range (UTC).

    One SQL pass over ``agile_rates``. Returns a list ordered by date with:
      ``date``, ``slot_count``, ``min_p``, ``max_p``, ``mean_p``,
      ``vwap_p`` (volume-weighted equals mean here since slots are uniform
      30 min — kept as a separate column so the front-end can show both).

    Use ``YYYY-MM-DD`` strings for ``start_date`` / ``end_date``. Bounds are
    UTC-local (we group by ``DATE(valid_from)`` which is the UTC component
    of the ISO timestamp). Good enough for year-scale audit views.
    """
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                """SELECT
                       SUBSTR(valid_from, 1, 10) AS date,
                       COUNT(*) AS slot_count,
                       MIN(value_inc_vat) AS min_p,
                       MAX(value_inc_vat) AS max_p,
                       AVG(value_inc_vat) AS mean_p,
                       AVG(value_inc_vat) AS vwap_p
                   FROM agile_rates
                   WHERE tariff_code = ?
                     AND SUBSTR(valid_from, 1, 10) >= ?
                     AND SUBSTR(valid_from, 1, 10) <= ?
                   GROUP BY SUBSTR(valid_from, 1, 10)
                   ORDER BY SUBSTR(valid_from, 1, 10) ASC""",
                (tariff_code, start_date, end_date),
            )
            return [dict(r) for r in cur.fetchall()]
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


def get_pending_actions(plan_date: str | None = None) -> list[dict[str, Any]]:
    with _lock:
        conn = get_connection()
        try:
            if plan_date:
                cur = conn.execute(
                    """SELECT * FROM action_schedule WHERE status = 'pending' AND date = ?
                       ORDER BY start_time""",
                    (plan_date,),
                )
            else:
                cur = conn.execute(
                    """SELECT * FROM action_schedule
                       WHERE status = 'pending'
                       ORDER BY date, start_time"""
                )
            rows = [_row_action(r) for r in cur.fetchall()]
        finally:
            conn.close()
    return rows


def get_active_actions(plan_date: str | None = None) -> list[dict[str, Any]]:
    with _lock:
        conn = get_connection()
        try:
            if plan_date:
                cur = conn.execute(
                    """SELECT * FROM action_schedule
                       WHERE status = 'active' AND date = ?
                       ORDER BY start_time""",
                    (plan_date,),
                )
            else:
                cur = conn.execute(
                    """SELECT * FROM action_schedule
                       WHERE status = 'active'
                       ORDER BY date, start_time"""
                )
            rows = [_row_action(r) for r in cur.fetchall()]
        finally:
            conn.close()
    return rows


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


def hourly_load_profile_kwh(limit: int = 2016) -> dict[int, float]:
    """
    Return per-hour-of-day (0–23) mean consumption kWh from execution_log.

    Uses the last ``limit`` rows (default ~6 weeks of half-hour slots).
    Falls back to the flat mean for hours with no data.
    Returns a dict mapping hour-of-day → expected kWh per half-hour slot.

    Kept for analytics / human-facing aggregates. The LP path uses
    :func:`half_hourly_load_profile_kwh` for finer granularity (S10.8 / #175).
    """
    rows = get_execution_logs(limit=limit)
    buckets: dict[int, list[float]] = {h: [] for h in range(24)}
    for r in rows:
        if r.get("consumption_kwh") is None:
            continue
        ts_str = r.get("timestamp") or ""
        try:
            from datetime import datetime as _dt
            ts = _dt.fromisoformat(ts_str.replace("Z", "+00:00"))
            hour = ts.astimezone(ZoneInfo(config.BULLETPROOF_TIMEZONE)).hour
        except (ValueError, TypeError):
            continue
        buckets[hour].append(float(r["consumption_kwh"]))

    flat_mean = mean_consumption_kwh_from_execution_logs(limit=limit)
    profile: dict[int, float] = {}
    for h in range(24):
        if buckets[h]:
            profile[h] = sum(buckets[h]) / len(buckets[h])
        else:
            profile[h] = flat_mean
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


def actions_for_device_at(
    device: str,
    when_utc: datetime,
    plan_date: str,
) -> list[dict[str, Any]]:
    """Actions where when_utc is in [start_time, end_time)."""
    ts = when_utc.isoformat().replace("+00:00", "Z")
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                """SELECT * FROM action_schedule
                   WHERE device = ? AND date = ? AND start_time <= ? AND end_time > ?
                     AND status IN ('pending', 'active')
                   ORDER BY start_time""",
                (device, plan_date, ts, ts),
            )
            rows = [_row_action(r) for r in cur.fetchall()]
        finally:
            conn.close()
    return rows


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
    """
    now_utc = _now_utc()
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
    """
    now_utc = _now_utc()
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
                """SELECT id, start_time, end_time, restore_action_id
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

def save_meteo_forecast(rows: list[dict[str, Any]], forecast_date: str) -> int:
    """Upsert hourly Open-Meteo forecast rows for *forecast_date*.

    Each dict must have: slot_time (ISO), temp_c (float), solar_w_m2 (float).
    """
    if not rows:
        return 0
    n = 0
    with _lock:
        conn = get_connection()
        try:
            for r in rows:
                slot = r.get("slot_time")
                if not slot:
                    continue
                conn.execute(
                    """INSERT INTO meteo_forecast (forecast_date, slot_time, temp_c, solar_w_m2)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(slot_time) DO UPDATE SET
                         forecast_date=excluded.forecast_date,
                         temp_c=excluded.temp_c,
                         solar_w_m2=excluded.solar_w_m2""",
                    (
                        forecast_date,
                        str(slot),
                        r.get("temp_c"),
                        r.get("solar_w_m2"),
                    ),
                )
                n += 1
            conn.commit()
        finally:
            conn.close()
    return n


def get_meteo_forecast(forecast_date: str) -> list[dict[str, Any]]:
    """Return all meteo_forecast rows for *forecast_date* ordered by slot_time."""
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                "SELECT * FROM meteo_forecast WHERE forecast_date = ? ORDER BY slot_time",
                (forecast_date,),
            )
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()


def get_micro_climate_offset_c(lookback: int = 96) -> float:
    """Return mean(daikin_outdoor_temp - forecast_temp_c) from the most recent *lookback* rows.

    A positive result means the local microclimate runs warmer than the Open-Meteo forecast;
    negative means colder (e.g. -1.5 means the garden is ~1.5 °C colder than forecast).
    Returns 0.0 during the bootstrapping period when values are identical or missing.
    """
    with _lock:
        conn = get_connection()
        try:
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


# ---------------------------------------------------------------------------
# V2: pnl_execution_log
# ---------------------------------------------------------------------------

def log_pnl_execution(row: dict[str, Any]) -> int:
    """Insert one row into pnl_execution_log. Returns new row id."""
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                """INSERT INTO pnl_execution_log
                   (slot_time, kwh_consumed, agile_price_pence, svt_price_pence, delta_pence)
                   VALUES (?, ?, ?, ?, ?)""",
                (
                    row.get("slot_time"),
                    row.get("kwh_consumed"),
                    row.get("agile_price_pence"),
                    row.get("svt_price_pence"),
                    row.get("delta_pence"),
                ),
            )
            conn.commit()
            return int(cur.lastrowid)
        finally:
            conn.close()


def get_pnl_execution_logs(
    from_slot: str | None = None,
    to_slot: str | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Return pnl_execution_log rows in descending slot_time order."""
    with _lock:
        conn = get_connection()
        try:
            q = "SELECT * FROM pnl_execution_log WHERE 1=1"
            args: list[Any] = []
            if from_slot:
                q += " AND slot_time >= ?"
                args.append(from_slot)
            if to_slot:
                q += " AND slot_time <= ?"
                args.append(to_slot)
            q += " ORDER BY slot_time DESC LIMIT ?"
            args.append(limit)
            cur = conn.execute(q, args)
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()


def get_pnl_summary_for_date(date_str: str) -> dict[str, Any]:
    """Return PnL summary metrics (VWAP, total kWh, total saving) for one day."""
    rows = get_pnl_execution_logs(
        from_slot=f"{date_str}T00:00:00Z",
        to_slot=f"{date_str}T23:59:59Z",
        limit=10000,
    )
    if not rows:
        return {"date": date_str, "slots": 0, "total_kwh": 0.0, "total_cost_pence": 0.0,
                "total_saving_pence": 0.0, "vwap_pence": 0.0, "slippage_pence": 0.0}

    total_kwh = sum(float(r.get("kwh_consumed") or 0) for r in rows)
    total_cost = sum(
        float(r.get("kwh_consumed") or 0) * float(r.get("agile_price_pence") or 0) for r in rows
    )
    total_svt = sum(
        float(r.get("kwh_consumed") or 0) * float(r.get("svt_price_pence") or 0) for r in rows
    )
    total_saving = sum(float(r.get("delta_pence") or 0) for r in rows)
    vwap = total_cost / total_kwh if total_kwh else 0.0
    svt_vwap = total_svt / total_kwh if total_kwh else 0.0
    slippage = svt_vwap - vwap

    return {
        "date": date_str,
        "slots": len(rows),
        "total_kwh": round(total_kwh, 3),
        "total_cost_pence": round(total_cost, 2),
        "total_saving_pence": round(total_saving, 2),
        "vwap_pence": round(vwap, 3),
        "svt_vwap_pence": round(svt_vwap, 3),
        "slippage_pence": round(slippage, 3),
    }


# ---------------------------------------------------------------------------
# V3: Fox ESS daily energy cache
# ---------------------------------------------------------------------------

def compute_fox_energy_daily_from_realtime(
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    max_gap_seconds: int = 600,
) -> list[dict[str, Any]]:
    """Aggregate ``pv_realtime_history`` instantaneous-power samples into per-day
    kWh totals via trapezoidal integration.

    For each consecutive pair of samples ``(t_a, P_a)`` → ``(t_b, P_b)``, energy
    contribution is ``mean(P_a, P_b) × dt`` where ``dt`` is capped at
    ``max_gap_seconds`` (default 10 min). Capping prevents multi-hour heartbeat
    gaps (e.g. service down) from extrapolating a constant-power assumption
    across a missing window.

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
                     kwh_total=excluded.kwh_total,
                     kwh_heating=excluded.kwh_heating,
                     kwh_dhw=excluded.kwh_dhw,
                     cop_daily=excluded.cop_daily,
                     source=excluded.source,
                     fetched_at=excluded.fetched_at""",
                (date, kwh_total, kwh_heating, kwh_dhw, cop_daily, source, now),
            )
            conn.commit()
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


def expire_plan(plan_id: str) -> bool:
    """Set plan_consent status to expired. Returns True if a row was updated."""
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                """UPDATE plan_consent
                   SET status='expired'
                   WHERE plan_id=? AND status='pending_approval'""",
                (plan_id,),
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
                    base_load_json, micro_climate_offset_c, config_snapshot_json,
                    price_quantize_p, peak_threshold_p, cheap_threshold_p,
                    daikin_control_mode, optimization_preset, energy_strategy_mode)
                   VALUES (:run_id, :run_at_utc, :plan_date, :horizon_hours,
                           :soc_initial_kwh, :tank_initial_c, :indoor_initial_c,
                           :soc_source, :tank_source, :indoor_source,
                           :base_load_json, :micro_climate_offset_c, :config_snapshot_json,
                           :price_quantize_p, :peak_threshold_p, :cheap_threshold_p,
                           :daikin_control_mode, :optimization_preset, :energy_strategy_mode)""",
                {"run_id": run_id, **inputs_row},
            )
            for row in solution_rows:
                conn.execute(
                    """INSERT OR REPLACE INTO lp_solution_snapshot
                       (run_id, slot_index, slot_time_utc, price_p,
                        import_kwh, export_kwh, charge_kwh, discharge_kwh,
                        pv_use_kwh, pv_curtail_kwh, dhw_kwh, space_kwh,
                        soc_kwh, tank_temp_c, indoor_temp_c, outdoor_temp_c, lwt_offset_c)
                       VALUES (:run_id, :slot_index, :slot_time_utc, :price_p,
                               :import_kwh, :export_kwh, :charge_kwh, :discharge_kwh,
                               :pv_use_kwh, :pv_curtail_kwh, :dhw_kwh, :space_kwh,
                               :soc_kwh, :tank_temp_c, :indoor_temp_c, :outdoor_temp_c, :lwt_offset_c)""",
                    {"run_id": run_id, **row},
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
    """Append a forecast fetch's per-slot rows to the history table.

    Companion to :func:`save_meteo_forecast` (which is latest-per-slot). This
    table preserves every fetch so the History view can show forecasts as they
    were at past LP runs.
    """
    if not rows:
        return
    with _lock:
        conn = get_connection()
        try:
            for r in rows:
                conn.execute(
                    """INSERT OR IGNORE INTO meteo_forecast_history
                       (forecast_fetch_at_utc, slot_time, temp_c, solar_w_m2)
                       VALUES (?, ?, ?, ?)""",
                    (
                        forecast_fetch_at_utc,
                        r.get("slot_time"),
                        r.get("temp_c"),
                        r.get("solar_w_m2"),
                    ),
                )
            conn.commit()
        finally:
            conn.close()


def get_meteo_forecast_at(fetch_at_utc: str) -> list[dict[str, Any]]:
    """Return the forecast rows stored for a specific fetch timestamp."""
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute(
                """SELECT slot_time, temp_c, solar_w_m2
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
                """SELECT slot_time, temp_c, solar_w_m2
                   FROM meteo_forecast_history
                   WHERE forecast_fetch_at_utc = ?
                   ORDER BY slot_time""",
                (prev_fetch_at,),
            )
            return [dict(r) for r in cur.fetchall()]
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
    lp_solution_snapshot, lp_inputs_snapshot, config_audit) have no
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
        ("lp_solution_snapshot", "slot_time_utc", _config.LP_SNAPSHOT_RETENTION_DAYS, False),
        ("lp_inputs_snapshot", "run_at_utc", _config.LP_SNAPSHOT_RETENTION_DAYS, False),
        ("config_audit", "changed_at_utc", _config.CONFIG_AUDIT_RETENTION_DAYS, False),
    ]
    results: dict[str, int] = {}
    for table, col, days, epoch in policies:
        try:
            results[table] = prune_old_rows(table, col, days, epoch_seconds=epoch)
        except Exception as e:
            logger.debug("prune %s failed: %s", table, e)
            results[table] = -1
    return results

