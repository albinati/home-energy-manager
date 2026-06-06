"""#222 — learn appliance typical_kw from measured actual_kwh history.

The rolling mean of recent completed runs' cycle energy replaces the static
registration default once enough samples exist; below the threshold, callers
fall back to the registered typical_kw.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from src.config import config


def _seed_job(conn, *, appliance_id: int, actual_kwh, duration_min: int, status="completed"):
    conn.execute(
        """INSERT INTO appliance_jobs
           (appliance_id, status, armed_at_utc, deadline_utc, duration_minutes,
            planned_start_utc, planned_end_utc, actual_kwh, created_at, updated_at)
           VALUES (?, ?, '2026-06-06T00:00:00Z', '2026-06-06T06:00:00Z', ?,
                   '2026-06-06T01:00:00Z', '2026-06-06T03:00:00Z', ?,
                   '2026-06-06T00:00:00Z', '2026-06-06T00:00:00Z')""",
        (appliance_id, status, duration_min, actual_kwh),
    )


def test_rolling_mean_kw(monkeypatch):
    import src.db as db
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setattr(config, "DB_PATH", str(Path(td) / "t.db"), raising=False)
        db.init_db()
        conn = db.get_connection()
        try:
            # 4 completed runs: 0.4 kWh over 120 min → 0.2 kW each.
            for _ in range(4):
                _seed_job(conn, appliance_id=1, actual_kwh=0.4, duration_min=120)
            conn.commit()
        finally:
            conn.close()
        out = db.appliance_learned_typical_kw(1, min_samples=3)
    assert out is not None
    mean_kw, n = out
    assert n == 4
    assert abs(mean_kw - 0.2) < 1e-6


def test_below_min_samples_returns_none(monkeypatch):
    import src.db as db
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setattr(config, "DB_PATH", str(Path(td) / "t.db"), raising=False)
        db.init_db()
        conn = db.get_connection()
        try:
            _seed_job(conn, appliance_id=1, actual_kwh=0.4, duration_min=120)
            _seed_job(conn, appliance_id=1, actual_kwh=0.5, duration_min=120)
            conn.commit()
        finally:
            conn.close()
        assert db.appliance_learned_typical_kw(1, min_samples=3) is None


def test_ignores_incomplete_and_zero(monkeypatch):
    import src.db as db
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setattr(config, "DB_PATH", str(Path(td) / "t.db"), raising=False)
        db.init_db()
        conn = db.get_connection()
        try:
            for _ in range(3):
                _seed_job(conn, appliance_id=1, actual_kwh=0.6, duration_min=120)  # 0.3 kW
            # noise that must be excluded:
            _seed_job(conn, appliance_id=1, actual_kwh=None, duration_min=120)       # no measurement
            _seed_job(conn, appliance_id=1, actual_kwh=0.0, duration_min=120)        # zero
            _seed_job(conn, appliance_id=1, actual_kwh=0.6, duration_min=120, status="scheduled")
            conn.commit()
        finally:
            conn.close()
        out = db.appliance_learned_typical_kw(1, min_samples=3)
    assert out is not None
    assert abs(out[0] - 0.3) < 1e-6 and out[1] == 3


def test_effective_kw_prefers_learned_else_static(monkeypatch):
    import src.db as db
    from src.scheduler.appliance_dispatch import _effective_typical_kw
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setattr(config, "DB_PATH", str(Path(td) / "t.db"), raising=False)
        monkeypatch.setattr(config, "APPLIANCE_LEARNED_KW_MIN_SAMPLES", 3, raising=False)
        db.init_db()
        # No history → static fallback.
        kw, n = _effective_typical_kw(1, static_kw=0.5)
        assert kw == 0.5 and n == 0
        # With history → learned.
        conn = db.get_connection()
        try:
            for _ in range(3):
                _seed_job(conn, appliance_id=1, actual_kwh=0.4, duration_min=120)  # 0.2 kW
            conn.commit()
        finally:
            conn.close()
        kw, n = _effective_typical_kw(1, static_kw=0.5)
    assert abs(kw - 0.2) < 1e-6 and n == 3
