"""Actuation-health alert (2026-06-14): is the plan reaching the hardware?

The ~41h Fox-upload wedge ran silently because nothing watched actuation
freshness — drift detection only compares live-vs-stored (both stale-consistent
during the wedge). This block adds: Fox upload staleness, Daikin tank
actuation staleness + failure count, Daikin LWT failure count (demand-gated,
no age alarm).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

import src.api.routers.status as st
from src import db


def _iso(hours_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours_ago)).isoformat()


@pytest.fixture(autouse=True)
def _knobs(monkeypatch):
    monkeypatch.setattr(st.config, "FOX_UPLOAD_STALE_HOURS", 30.0, raising=False)
    monkeypatch.setattr(st.config, "DAIKIN_TANK_STALE_HOURS", 30.0, raising=False)
    monkeypatch.setattr(st.config, "DAIKIN_FAILED_ALERT_THRESHOLD", 3, raising=False)


def _patch_raw(monkeypatch, **kw):
    raw = {"fox_upload_at": None, "tank_last_at": None,
           "tank_failed_24h": 0, "lwt_failed_24h": 0, **kw}
    monkeypatch.setattr(db, "get_actuation_health", lambda since: raw)


def test_healthy_all_quiet(monkeypatch):
    _patch_raw(monkeypatch, fox_upload_at=_iso(0.7), tank_last_at=_iso(5.7))
    b = st._actuation_block()
    assert b["fox"]["stale"] is False
    assert b["daikin_tank"]["stale"] is False
    assert b["daikin_tank"]["failing"] is False
    assert b["daikin_lwt"]["failing"] is False


def test_fox_upload_wedge_flags_stale(monkeypatch):
    """The exact incident: last successful upload 41h ago → stale (would have
    surfaced the wedge in <30h instead of going unnoticed for 41h)."""
    _patch_raw(monkeypatch, fox_upload_at=_iso(41), tank_last_at=_iso(5))
    b = st._actuation_block()
    assert b["fox"]["stale"] is True
    assert b["fox"]["age_hours"] == pytest.approx(41, abs=0.5)


def test_fox_never_uploaded_is_stale(monkeypatch):
    _patch_raw(monkeypatch, fox_upload_at=None, tank_last_at=_iso(1))
    assert st._actuation_block()["fox"]["stale"] is True


def test_tank_stale_when_no_recent_actuation(monkeypatch):
    _patch_raw(monkeypatch, fox_upload_at=_iso(1), tank_last_at=_iso(31))
    b = st._actuation_block()
    assert b["daikin_tank"]["stale"] is True


def test_vacation_mode_suppresses_tank_stale_but_not_failures(monkeypatch):
    """Vacation mode writes ZERO tank rows by design, so an old tank_last_at is
    NOT a fault — the age alarm must stay quiet (the alert-noise the user hates).
    A rejected write is still meaningful, so `failing` stays live."""
    monkeypatch.setattr(st.config, "OPTIMIZATION_PRESET", "vacation", raising=False)
    _patch_raw(monkeypatch, fox_upload_at=_iso(1), tank_last_at=_iso(99),
               tank_failed_24h=3)
    b = st._actuation_block()
    assert b["daikin_tank"]["stale"] is False     # suppressed in vacation
    assert b["daikin_tank"]["failing"] is True     # failures still surface


def test_failed_threshold_zero_clamps_to_one(monkeypatch):
    """A misconfigured threshold of 0 must NOT make everything 'failing'."""
    monkeypatch.setattr(st.config, "DAIKIN_FAILED_ALERT_THRESHOLD", 0, raising=False)
    _patch_raw(monkeypatch, fox_upload_at=_iso(1), tank_last_at=_iso(1),
               tank_failed_24h=0, lwt_failed_24h=0)
    b = st._actuation_block()
    assert b["daikin_tank"]["failing"] is False
    assert b["daikin_lwt"]["failing"] is False


def test_failed_writes_flag_failing_at_threshold(monkeypatch):
    _patch_raw(monkeypatch, fox_upload_at=_iso(1), tank_last_at=_iso(1),
               tank_failed_24h=3, lwt_failed_24h=4)
    b = st._actuation_block()
    assert b["daikin_tank"]["failing"] is True
    assert b["daikin_lwt"]["failing"] is True


def test_below_threshold_is_not_failing(monkeypatch):
    _patch_raw(monkeypatch, fox_upload_at=_iso(1), tank_last_at=_iso(1),
               tank_failed_24h=2, lwt_failed_24h=2)
    b = st._actuation_block()
    assert b["daikin_tank"]["failing"] is False
    assert b["daikin_lwt"]["failing"] is False


def test_stale_hours_zero_disables_age_alarm(monkeypatch):
    monkeypatch.setattr(st.config, "FOX_UPLOAD_STALE_HOURS", 0.0, raising=False)
    _patch_raw(monkeypatch, fox_upload_at=_iso(999), tank_last_at=None)
    assert st._actuation_block()["fox"]["stale"] is False


def test_block_never_raises_on_db_error(monkeypatch):
    def boom(since):
        raise RuntimeError("db down")
    monkeypatch.setattr(db, "get_actuation_health", boom)
    b = st._actuation_block()
    assert b == {"fox": None, "daikin_tank": None, "daikin_lwt": None}


# ── db helper against real rows ───────────────────────────────────────────────

def test_get_actuation_health_counts_failed_by_domain(monkeypatch):
    db.init_db()
    now = datetime.now(UTC)
    recent = (now - timedelta(hours=2)).isoformat()
    old = (now - timedelta(hours=48)).isoformat()
    with db._lock:
        conn = db.get_connection()
        try:
            conn.execute("DELETE FROM action_schedule")
            rows = [
                ("daikin", "tank_warmup", "failed", recent),
                ("daikin", "tank_setback", "failed", recent),
                ("daikin", "tank_warmup", "failed", old),       # outside 24h window
                ("daikin", "lwt_preheat", "failed", recent),
                ("daikin", "tank_warmup", "completed", recent),  # a successful tank fire
            ]
            for dev, at, status, ts in rows:
                conn.execute(
                    "INSERT INTO action_schedule (date, start_time, end_time, device, "
                    "action_type, status, executed_at, created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (ts[:10], ts, ts, dev, at, status, ts, ts),
                )
            conn.commit()
        finally:
            conn.close()
    raw = db.get_actuation_health((now - timedelta(hours=24)).isoformat())
    assert raw["tank_failed_24h"] == 2          # old one excluded
    assert raw["lwt_failed_24h"] == 1
    assert raw["tank_last_at"] is not None        # the completed tank fire
