"""Tests for ``src/analytics/audit_report.py`` (Story A1, Epic 13a).

Verifies the structured shape, classification logic, and rendering rules
of the lifted audit module. The prod-side script at
``/srv/hem/data/audit_held_schedule.py`` becomes a thin shim around
:func:`build_audit_report` + :func:`render_audit_markdown` once A1 lands.
"""
from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

import pytest

from src.analytics import audit_report


# ---------------------------------------------------------------------------
# Schema bootstrap — minimal DDL covering only the columns the audit reads
# ---------------------------------------------------------------------------

def _make_db(tmp_path: Path) -> Path:
    """Create a tmp sqlite DB with the schema slice the audit touches."""
    db_path = tmp_path / "audit.db"
    db = sqlite3.connect(str(db_path))
    db.executescript(
        """
        CREATE TABLE optimizer_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at TEXT NOT NULL,
            strategy_summary TEXT
        );
        CREATE TABLE pv_realtime_history (
            captured_at TEXT PRIMARY KEY,
            solar_power_kw REAL,
            soc_pct REAL,
            load_power_kw REAL,
            grid_import_kw REAL,
            grid_export_kw REAL,
            battery_charge_kw REAL,
            battery_discharge_kw REAL,
            source TEXT NOT NULL DEFAULT 'test'
        );
        CREATE TABLE daikin_telemetry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fetched_at REAL NOT NULL,
            source TEXT NOT NULL,
            tank_temp_c REAL
        );
        CREATE TABLE lp_inputs_snapshot (
            run_id INTEGER PRIMARY KEY,
            run_at_utc TEXT NOT NULL
        );
        CREATE TABLE lp_solution_snapshot (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            slot_index INTEGER NOT NULL,
            slot_time_utc TEXT NOT NULL,
            import_kwh REAL,
            export_kwh REAL,
            charge_kwh REAL,
            discharge_kwh REAL,
            pv_use_kwh REAL,
            dhw_kwh REAL,
            space_kwh REAL
        );
        CREATE TABLE dispatch_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            slot_time_utc TEXT NOT NULL,
            lp_kind TEXT NOT NULL,
            dispatched_kind TEXT NOT NULL,
            reason TEXT
        );
        CREATE TABLE fox_schedule_state (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uploaded_at TEXT NOT NULL
        );
        CREATE TABLE agile_rates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            valid_from TEXT NOT NULL,
            value_inc_vat REAL NOT NULL
        );
        CREATE TABLE agile_export_rates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            valid_from TEXT NOT NULL,
            value_inc_vat REAL NOT NULL
        );
        """
    )
    db.commit()
    db.close()
    return db_path


def _now() -> dt.datetime:
    """Fixed anchor — 2026-05-20 10:00 UTC. Audit window = preceding 24 h
    (2026-05-19 10:00 → 2026-05-20 10:00 UTC)."""
    return dt.datetime(2026, 5, 20, 10, 0, 0, tzinfo=dt.timezone.utc)


def _ago(hours: float) -> str:
    """ISO timestamp for the half-hour slot floor of (now - *hours*).

    Stays inside the audit window for ``0 < hours < 24``. Floor to the
    30-min grid so slot_time_utc strings round-trip exactly against the
    audit's slot-iterator."""
    t = _now() - dt.timedelta(hours=hours)
    minute = 0 if t.minute < 30 else 30
    return t.replace(minute=minute, second=0, microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# Shape / empty-DB
# ---------------------------------------------------------------------------

def test_build_audit_report_returns_complete_shape_on_empty_db(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    report = audit_report.build_audit_report(now_utc=_now(), db_path=db)

    assert report["window_hours"] == 24
    assert report["now_utc"] == _now().isoformat()

    held = report["held_schedule"]
    assert held["total"] == 0
    assert held["soc_below_reserve"] == 0
    assert held["soc_above_reserve"] == 0
    assert held["soc_unknown"] == 0
    assert held["events"] == []

    pve = report["plan_vs_execution"]
    assert pve["lp_runs"] == 0
    assert pve["paired_uploads"] == 0
    assert pve["coverage_pct"] == 100.0
    assert pve["plan_kwh"] == {"import": 0.0, "export_effective": 0.0}
    assert pve["real_kwh"] == {"import": 0.0, "export": 0.0}
    assert pve["cost_delta_p"] == 0.0
    assert pve["disparities"] == []

    forgone = report["forgone_export"]
    assert forgone == {"kwh": 0.0, "pence": 0.0, "slot_count": 0, "reasons": {}}


def test_empty_audit_renders_silent(tmp_path: Path) -> None:
    """No held events + small cost delta + 100% coverage → markdown is None."""
    db = _make_db(tmp_path)
    report = audit_report.build_audit_report(now_utc=_now(), db_path=db)
    assert audit_report.render_audit_markdown(report) is None


# ---------------------------------------------------------------------------
# Section A — held-schedule classification
# ---------------------------------------------------------------------------

def _seed_held_event(
    db_path: Path,
    *,
    run_at: str,
    soc_pct: float | None,
    tank_temp_c: float | None = None,
) -> None:
    db = sqlite3.connect(str(db_path))
    db.execute(
        "INSERT INTO optimizer_log (run_at, strategy_summary) VALUES (?, ?)",
        (run_at, "Infeasible — held previous schedule"),
    )
    if soc_pct is not None:
        # Insert one realtime row whose timestamp is >= run_at so the lookup
        # ``captured_at >= run_at ORDER BY captured_at LIMIT 1`` finds it.
        db.execute(
            "INSERT INTO pv_realtime_history (captured_at, soc_pct, source) VALUES (?, ?, 'test')",
            (run_at, soc_pct),
        )
    if tank_temp_c is not None:
        # Daikin uses unix-epoch seconds; pick a value slightly BEFORE the
        # run_at timestamp so the < cutoff lookup picks it up.
        ts = dt.datetime.fromisoformat(run_at.replace("Z", "+00:00")).timestamp() - 60
        db.execute(
            "INSERT INTO daikin_telemetry (fetched_at, source, tank_temp_c) VALUES (?, 'test', ?)",
            (ts, tank_temp_c),
        )
    db.commit()
    db.close()


def test_held_event_classified_below_reserve(tmp_path: Path) -> None:
    """SoC at 5% on a 10 kWh battery with 15% reserve → below_reserve True."""
    db = _make_db(tmp_path)
    _seed_held_event(db, run_at=_ago(3), soc_pct=5.0, tank_temp_c=42.5)
    report = audit_report.build_audit_report(now_utc=_now(), db_path=db)
    held = report["held_schedule"]
    assert held["total"] == 1
    assert held["soc_below_reserve"] == 1
    assert held["soc_above_reserve"] == 0
    e = held["events"][0]
    assert e["soc_pct"] == 5.0
    assert e["tank_temp_c"] == 42.5
    assert e["below_reserve"] is True


def test_held_event_classified_above_reserve(tmp_path: Path) -> None:
    """SoC at 55% → comfortably above the 15% reserve → goes to follow-up class."""
    db = _make_db(tmp_path)
    _seed_held_event(db, run_at=_ago(12), soc_pct=55.0, tank_temp_c=40.0)
    report = audit_report.build_audit_report(now_utc=_now(), db_path=db)
    held = report["held_schedule"]
    assert held["soc_below_reserve"] == 0
    assert held["soc_above_reserve"] == 1
    assert held["events"][0]["below_reserve"] is False


def test_held_event_with_unknown_soc_counted_separately(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    _seed_held_event(db, run_at=_ago(5), soc_pct=None)
    report = audit_report.build_audit_report(now_utc=_now(), db_path=db)
    held = report["held_schedule"]
    assert held["soc_unknown"] == 1
    assert held["soc_below_reserve"] == 0
    assert held["soc_above_reserve"] == 0


def test_event_outside_window_not_included(tmp_path: Path) -> None:
    """A held event 36 h ago is older than the 24 h window — must be excluded."""
    db = _make_db(tmp_path)
    _seed_held_event(db, run_at=_ago(36), soc_pct=10.0)
    report = audit_report.build_audit_report(now_utc=_now(), db_path=db)
    assert report["held_schedule"]["total"] == 0


def test_held_events_render_into_markdown(tmp_path: Path) -> None:
    """Any held event forces a Telegram render regardless of cost delta."""
    db = _make_db(tmp_path)
    _seed_held_event(db, run_at=_ago(12), soc_pct=55.0, tank_temp_c=39.5)
    report = audit_report.build_audit_report(now_utc=_now(), db_path=db)
    md = audit_report.render_audit_markdown(report)
    assert md is not None
    assert "Held-schedule events" in md
    assert "55.0%" in md
    assert "39.5" in md   # tank temperature value (no degree sign assertion — encoding)


# ---------------------------------------------------------------------------
# Section B — plan vs execution + forgone export
# ---------------------------------------------------------------------------

def _seed_lp_pair(
    db_path: Path,
    *,
    run_id: int,
    run_at_utc: str,
    slot_time_utc: str,
    plan_import_kwh: float = 0.0,
    plan_export_kwh: float = 0.0,
    dispatched_kind: str = "standard",
    lp_kind: str = "standard",
) -> None:
    db = sqlite3.connect(str(db_path))
    db.execute(
        "INSERT INTO lp_inputs_snapshot (run_id, run_at_utc) VALUES (?, ?)",
        (run_id, run_at_utc),
    )
    db.execute(
        "INSERT INTO lp_solution_snapshot "
        "(run_id, slot_index, slot_time_utc, import_kwh, export_kwh) "
        "VALUES (?, ?, ?, ?, ?)",
        (run_id, 0, slot_time_utc, plan_import_kwh, plan_export_kwh),
    )
    db.execute(
        "INSERT INTO dispatch_decisions (run_id, slot_time_utc, lp_kind, dispatched_kind) "
        "VALUES (?, ?, ?, ?)",
        (run_id, slot_time_utc, lp_kind, dispatched_kind),
    )
    db.commit()
    db.close()


def _seed_realised(
    db_path: Path, *, captured_at: str, grid_import_kw: float, grid_export_kw: float = 0.0
) -> None:
    db = sqlite3.connect(str(db_path))
    db.execute(
        "INSERT INTO pv_realtime_history "
        "(captured_at, grid_import_kw, grid_export_kw, source) VALUES (?, ?, ?, 'test')",
        (captured_at, grid_import_kw, grid_export_kw),
    )
    db.commit()
    db.close()


def _seed_rate(db_path: Path, *, valid_from: str, import_p: float, export_p: float = 5.0) -> None:
    db = sqlite3.connect(str(db_path))
    db.execute(
        "INSERT INTO agile_rates (valid_from, value_inc_vat) VALUES (?, ?)",
        (valid_from, import_p),
    )
    db.execute(
        "INSERT INTO agile_export_rates (valid_from, value_inc_vat) VALUES (?, ?)",
        (valid_from, export_p),
    )
    db.commit()
    db.close()


def test_disparity_surfaces_when_real_imports_above_plan(tmp_path: Path) -> None:
    """LP planned 0 import @ 20p/kWh; realised mean 2 kW × 0.5 h = 1 kWh
    imported. Δcost ≈ +20p — surfaces in the per-slot disparities list."""
    db = _make_db(tmp_path)
    slot = _ago(4)
    _seed_lp_pair(db, run_id=1, run_at_utc=_ago(4.5), slot_time_utc=slot,
                  plan_import_kwh=0.0)
    # One heartbeat sample inside the slot — mean is the value itself.
    _seed_realised(db, captured_at=slot, grid_import_kw=2.0)
    _seed_rate(db, valid_from=slot, import_p=20.0)

    report = audit_report.build_audit_report(now_utc=_now(), db_path=db)
    pve = report["plan_vs_execution"]
    assert pve["plan_kwh"]["import"] == 0.0
    assert pve["real_kwh"]["import"] == 1.0
    assert pve["cost_delta_p"] == pytest.approx(20.0, abs=0.5)
    assert len(pve["disparities"]) == 1
    d = pve["disparities"][0]
    assert d["delta_cost_p"] == pytest.approx(20.0, abs=0.5)
    assert d["plan_import_kwh"] == 0.0
    assert d["real_import_kwh"] == 1.0


def test_blocked_peak_export_counted_as_forgone(tmp_path: Path) -> None:
    """LP chose peak_export (battery→grid) for 1 kWh @ 25p, but the pessimistic
    scenario filter downgraded it to standard → that IS forgone revenue."""
    db = _make_db(tmp_path)
    slot = _ago(21)
    _seed_lp_pair(
        db, run_id=1, run_at_utc=_ago(21.2), slot_time_utc=slot,
        plan_import_kwh=0.0, plan_export_kwh=1.0,
        lp_kind="peak_export", dispatched_kind="standard",
    )
    _seed_rate(db, valid_from=slot, import_p=15.0, export_p=25.0)

    report = audit_report.build_audit_report(now_utc=_now(), db_path=db)
    forgone = report["forgone_export"]
    assert forgone["slot_count"] == 1
    assert forgone["kwh"] == 1.0
    assert forgone["pence"] == pytest.approx(25.0, abs=0.5)


def test_pv_surplus_export_is_not_forgone(tmp_path: Path) -> None:
    """The phantom-loss regression. In normal/guests the LP constrains
    ``exp <= pv_use``, so export_kwh on a non-peak_export slot is PV SURPLUS —
    it ships via Fox SelfUse and already earns. It must NOT be reported as a
    loss. (Prod: 220 slots / 97.9 kWh of phantom "forgone" over 14 days.)"""
    db = _make_db(tmp_path)
    slot = _ago(21)
    _seed_lp_pair(
        db, run_id=1, run_at_utc=_ago(21.2), slot_time_utc=slot,
        plan_import_kwh=0.0, plan_export_kwh=1.0,
        lp_kind="solar_charge", dispatched_kind="solar_charge",
    )
    _seed_rate(db, valid_from=slot, import_p=15.0, export_p=25.0)

    report = audit_report.build_audit_report(now_utc=_now(), db_path=db)
    assert report["forgone_export"]["slot_count"] == 0
    assert report["forgone_export"]["kwh"] == 0.0


def test_peak_export_dispatch_does_not_count_as_forgone(tmp_path: Path) -> None:
    """When dispatched_kind=peak_export, the export was honoured → 0 forgone."""
    db = _make_db(tmp_path)
    slot = _ago(21)
    _seed_lp_pair(
        db, run_id=1, run_at_utc=_ago(21.2), slot_time_utc=slot,
        plan_export_kwh=1.0, dispatched_kind="peak_export",
    )
    _seed_rate(db, valid_from=slot, import_p=15.0, export_p=25.0)

    report = audit_report.build_audit_report(now_utc=_now(), db_path=db)
    assert report["forgone_export"]["slot_count"] == 0
    # And plan_kwh.export_effective DOES include this 1 kWh (it shipped).
    assert report["plan_vs_execution"]["plan_kwh"]["export_effective"] == 1.0


def test_robustness_downgrade_excluded_from_effective_plan_export(tmp_path: Path) -> None:
    """Same LP export, but downgraded to standard → effective plan export = 0,
    so it can NOT contribute a false-positive disparity vs zero real export."""
    db = _make_db(tmp_path)
    slot = _ago(21)
    _seed_lp_pair(
        db, run_id=1, run_at_utc=_ago(21.2), slot_time_utc=slot,
        plan_export_kwh=1.0, dispatched_kind="standard",
    )
    _seed_rate(db, valid_from=slot, import_p=15.0, export_p=25.0)

    report = audit_report.build_audit_report(now_utc=_now(), db_path=db)
    assert report["plan_vs_execution"]["plan_kwh"]["export_effective"] == 0.0
    # The robustness-filter downgrade should not generate a disparity row by
    # itself — real export is 0, effective plan export is 0, Δ = 0.
    no_export_disp = [
        d for d in report["plan_vs_execution"]["disparities"]
        if abs(d["delta_export_kwh"]) > 0
    ]
    assert no_export_disp == []


def test_coverage_pct_reflects_lp_run_to_upload_pairing(tmp_path: Path) -> None:
    """2 LP runs, 1 paired upload → 50% coverage."""
    db = _make_db(tmp_path)
    run_at_1 = _ago(4)
    run_at_2 = _ago(3)
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO lp_inputs_snapshot (run_id, run_at_utc) VALUES (1, ?)", (run_at_1,)
    )
    conn.execute(
        "INSERT INTO lp_inputs_snapshot (run_id, run_at_utc) VALUES (2, ?)", (run_at_2,)
    )
    conn.execute(
        "INSERT INTO fox_schedule_state (uploaded_at) VALUES (?)", (run_at_1,)
    )
    conn.commit()
    conn.close()

    report = audit_report.build_audit_report(now_utc=_now(), db_path=db)
    pve = report["plan_vs_execution"]
    assert pve["lp_runs"] == 2
    assert pve["paired_uploads"] == 1
    assert pve["coverage_pct"] == 50.0


def test_render_flags_coverage_when_below_threshold(tmp_path: Path) -> None:
    """Coverage 50% < 90% threshold → render emits the ⚠️ flag (forces a push
    even on a 0p cost-delta day)."""
    db = _make_db(tmp_path)
    run_at_1 = _ago(4)
    run_at_2 = _ago(3)
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO lp_inputs_snapshot (run_id, run_at_utc) VALUES (1, ?)", (run_at_1,)
    )
    conn.execute(
        "INSERT INTO lp_inputs_snapshot (run_id, run_at_utc) VALUES (2, ?)", (run_at_2,)
    )
    conn.execute(
        "INSERT INTO fox_schedule_state (uploaded_at) VALUES (?)", (run_at_1,)
    )
    conn.commit()
    conn.close()

    report = audit_report.build_audit_report(now_utc=_now(), db_path=db)
    md = audit_report.render_audit_markdown(report)
    assert md is not None  # coverage warn forces a push
    assert "50% coverage" in md
    assert "⚠️" in md
