"""DHW standing loss estimate from execution_log (#24)."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from src import db


def test_estimate_dhw_standing_loss_p50(monkeypatch: pytest.MonkeyPatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "t.db"
        monkeypatch.setattr("src.config.config.DB_PATH", str(path))
        db.init_db()
        conn = db.get_connection()
        try:
            # 1 °C/h cooldown while power off: four hourly samples → three pair rates
            for h, temp in enumerate([50.0, 49.0, 48.0, 47.0]):
                conn.execute(
                    """
                    INSERT INTO execution_log (
                        timestamp, daikin_tank_temp, daikin_tank_power_on
                    ) VALUES (?, ?, 0)
                    """,
                    (f"2026-01-10T{8+h:02d}:00:00Z", temp),
                )
            conn.commit()
        finally:
            conn.close()

        est = db.estimate_dhw_standing_loss_c_per_hour_p50(limit=100)
        assert est == pytest.approx(1.0, abs=0.01)


def test_estimate_dhw_standing_loss_insufficient_data(monkeypatch: pytest.MonkeyPatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "t.db"
        monkeypatch.setattr("src.config.config.DB_PATH", str(path))
        db.init_db()
        conn = db.get_connection()
        try:
            conn.execute(
                """
                INSERT INTO execution_log (
                    timestamp, daikin_tank_temp, daikin_tank_power_on
                ) VALUES (?, ?, 0)
                """,
                ("2026-01-10T08:00:00Z", 50.0),
            )
            conn.commit()
        finally:
            conn.close()

        assert db.estimate_dhw_standing_loss_c_per_hour_p50(limit=50) is None
