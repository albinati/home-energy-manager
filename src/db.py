"""SQLite persistence for Bulletproof Energy Manager (thread-safe).

Uses stdlib sqlite3 for compatibility with APScheduler and sync device clients.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

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


def save_agile_rates(rates: list[dict[str, Any]], tariff_code: str) -> int:
    """Upsert Agile rate rows. Each rate dict: value_inc_vat, valid_from, valid_to (ISO)."""
    if not rates or not tariff_code:
        return 0
    now = datetime.now(timezone.utc).isoformat()
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
                conn.execute(
                    """INSERT INTO agile_rates (valid_from, valid_to, value_inc_vat, tariff_code, fetched_at)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(valid_from, tariff_code) DO UPDATE SET
                         valid_to=excluded.valid_to,
                         value_inc_vat=excluded.value_inc_vat,
                         fetched_at=excluded.fetched_at""",
                    (str(vf), str(vt), float(v), tariff_code, now),
                )
                n += 1
            conn.commit()
        finally:
            conn.close()
    return n


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
    params: Optional[dict[str, Any]] = None,
    status: str = "pending",
    restore_action_id: Optional[int] = None,
) -> int:
    created = datetime.now(timezone.utc).isoformat()
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


def get_pending_actions(plan_date: Optional[str] = None) -> list[dict[str, Any]]:
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


def get_active_actions(plan_date: Optional[str] = None) -> list[dict[str, Any]]:
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
    error_msg: Optional[str] = None,
    executed_at: Optional[str] = None,
) -> None:
    ex = executed_at or datetime.now(timezone.utc).isoformat()
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


def get_action_by_id(action_id: int) -> Optional[dict[str, Any]]:
    with _lock:
        conn = get_connection()
        try:
            cur = conn.execute("SELECT * FROM action_schedule WHERE id = ?", (action_id,))
            r = cur.fetchone()
            return _row_action(r) if r else None
        finally:
            conn.close()


def get_actions_for_plan_date(plan_date: str, device: Optional[str] = None) -> list[dict[str, Any]]:
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
    params: Optional[dict[str, Any]],
    result: str,
    trigger: str,
    error_msg: Optional[str] = None,
    slot_kind: Optional[str] = None,
    agile_price_at_time: Optional[float] = None,
) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    with _lock:
        conn = get_connection()
        try:
            conn.execute(
                """INSERT INTO action_log
                   (timestamp, device, action, params, result, error_msg, trigger, slot_kind, agile_price_at_time)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                ),
            )
            conn.commit()
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


def get_daily_target(d: date | str) -> Optional[dict[str, Any]]:
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
    from_ts: Optional[str] = None,
    to_ts: Optional[str] = None,
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
    device: Optional[str] = None,
    trigger: Optional[str] = None,
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
    now = datetime.now(timezone.utc).isoformat()
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


def get_latest_fox_schedule_state() -> Optional[dict[str, Any]]:
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
    now = datetime.now(timezone.utc).isoformat()
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
    last_success_at: Optional[str]
    last_attempt_at: Optional[str]
    consecutive_failures: int
    survival_mode_since: Optional[str]
    failure_streak_started_at: Optional[str] = None


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
    last_success_at: Optional[str] = None,
    last_attempt_at: Optional[str] = None,
    consecutive_failures: Optional[int] = None,
    survival_mode_since: Optional[str] = None,
    failure_streak_started_at: Optional[str] = None,
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


def clear_actions_for_date(plan_date: str, device: Optional[str] = None) -> None:
    """Remove pending actions for a plan date (before re-optimizing)."""
    with _lock:
        conn = get_connection()
        try:
            if device:
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
    from_slot: Optional[str] = None,
    to_slot: Optional[str] = None,
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
