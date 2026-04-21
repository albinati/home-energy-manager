"""Micro-climate offset from execution_log (#20)."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from src import db


def test_micro_climate_offset_mean_divergence(monkeypatch: pytest.MonkeyPatch) -> None:
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "t.db"
        monkeypatch.setattr("src.config.config.DB_PATH", str(path))
        db.init_db()
        conn = db.get_connection()
        try:
            for i in range(3):
                conn.execute(
                    """
                    INSERT INTO execution_log (
                        timestamp, consumption_kwh, agile_price_pence,
                        daikin_outdoor_temp, forecast_temp_c
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        f"2026-01-{10+i:02d}T12:00:00Z",
                        0.0,
                        10.0,
                        10.0,
                        12.0,
                    ),
                )
            conn.commit()
        finally:
            conn.close()

        off = db.get_micro_climate_offset_c(lookback=96)
        assert off == pytest.approx(-2.0, abs=0.01)
