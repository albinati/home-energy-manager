"""When FORECAST_SOURCE=quartz, the radiation-trained PV calibration tables
(``pv_calibration_hourly`` + ``pv_calibration_hourly_cloud``) are unused at
apply time (PR #279). The Octopus fetch job must skip the daily rebuild
to avoid wasted Open-Meteo archive requests.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from src import db
from src.config import config as app_config


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    db_path = str(tmp_path / "t.db")
    monkeypatch.setattr(app_config, "DB_PATH", db_path, raising=False)
    db.init_db()


_FAKE_RATES = [
    # one minimal Agile rate row in the shape save_agile_rates expects
    {"slot_time": "2026-05-09T00:00:00Z", "value_inc_vat": 5.0},
]


def test_cal_rebuild_skipped_on_quartz(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_config, "FORECAST_SOURCE", "quartz", raising=False)
    monkeypatch.setattr(app_config, "OCTOPUS_TARIFF_CODE", "TEST-AGILE", raising=False)
    monkeypatch.setattr(app_config, "USE_BULLETPROOF_ENGINE", False, raising=False)

    from src.scheduler import octopus_fetch as of

    with patch.object(of, "fetch_agile_rates", return_value=_FAKE_RATES), \
         patch("src.db.save_agile_rates", return_value=len(_FAKE_RATES)), \
         patch("src.weather.compute_pv_calibration_hourly_table") as cal_h, \
         patch("src.weather.compute_pv_calibration_hourly_cloud_table") as cal_c:
        of.fetch_and_store_rates()

    assert not cal_h.called, "compute_pv_calibration_hourly_table called with FORECAST_SOURCE=quartz"
    assert not cal_c.called, "compute_pv_calibration_hourly_cloud_table called with FORECAST_SOURCE=quartz"


def test_cal_rebuild_runs_on_open_meteo(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sanity: when source is open-meteo, the rebuild still runs (no regression)."""
    monkeypatch.setattr(app_config, "FORECAST_SOURCE", "open-meteo", raising=False)
    monkeypatch.setattr(app_config, "OCTOPUS_TARIFF_CODE", "TEST-AGILE", raising=False)
    monkeypatch.setattr(app_config, "USE_BULLETPROOF_ENGINE", False, raising=False)

    from src.scheduler import octopus_fetch as of

    with patch.object(of, "fetch_agile_rates", return_value=_FAKE_RATES), \
         patch("src.db.save_agile_rates", return_value=len(_FAKE_RATES)), \
         patch("src.weather.compute_pv_calibration_hourly_table", return_value={"status": "ok"}) as cal_h, \
         patch("src.weather.compute_pv_calibration_hourly_cloud_table", return_value={"status": "ok"}) as cal_c:
        of.fetch_and_store_rates()

    assert cal_h.called, "open-meteo path must still rebuild per-hour calibration"
    assert cal_c.called, "open-meteo path must still rebuild cloud-aware calibration"
