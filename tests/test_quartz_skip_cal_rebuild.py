"""PR L1.1 (2026-05-24) — the cal-rebuild skip on FORECAST_SOURCE=quartz
is REMOVED. Calibration tables are now Quartz-trained (compute uses
``meteo_forecast_value.direct_pv_kw`` as baseline instead of
``estimate_pv_kw(open_meteo_rad)``), so the rebuild produces useful
factors for both Quartz and Open-Meteo paths.

This file replaces the legacy PR #279 skip-on-quartz semantics.
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
    {"slot_time": "2026-05-09T00:00:00Z", "value_inc_vat": 5.0},
]


def test_cal_rebuild_runs_on_quartz_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """PR L1.1 — when FORECAST_SOURCE=quartz, calibration tables MUST
    still rebuild because they're now Quartz-trained (not Open-Meteo
    radiation-trained). The old skip (PR #279) was based on the
    "Quartz self-calibrates" assumption that proved wrong for the
    W4 1DZ east-facing array."""
    monkeypatch.setattr(app_config, "FORECAST_SOURCE", "quartz", raising=False)
    monkeypatch.setattr(app_config, "OCTOPUS_TARIFF_CODE", "TEST-AGILE", raising=False)
    monkeypatch.setattr(app_config, "USE_BULLETPROOF_ENGINE", False, raising=False)

    from src.scheduler import octopus_fetch as of

    with patch.object(of, "fetch_agile_rates", return_value=_FAKE_RATES), \
         patch("src.db.save_agile_rates", return_value=len(_FAKE_RATES)), \
         patch("src.weather.compute_pv_calibration_hourly_table",
               return_value={"status": "ok"}) as cal_h, \
         patch("src.weather.compute_pv_calibration_hourly_cloud_table",
               return_value={"status": "ok"}) as cal_c:
        of.fetch_and_store_rates()

    assert cal_h.called, (
        "PR L1.1: hourly calibration MUST rebuild on Quartz source "
        "(table is now Quartz-trained)"
    )
    assert cal_c.called, (
        "PR L1.1: cloud-aware calibration MUST rebuild on Quartz source"
    )


def test_cal_rebuild_runs_on_open_meteo_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sanity: Open-Meteo source also rebuilds. The compute function
    finds direct_pv_kw=NULL rows on this path and yields fewer samples
    (or none, falling back to the flat factor); that's expected
    graceful degradation."""
    monkeypatch.setattr(app_config, "FORECAST_SOURCE", "open-meteo", raising=False)
    monkeypatch.setattr(app_config, "OCTOPUS_TARIFF_CODE", "TEST-AGILE", raising=False)
    monkeypatch.setattr(app_config, "USE_BULLETPROOF_ENGINE", False, raising=False)

    from src.scheduler import octopus_fetch as of

    with patch.object(of, "fetch_agile_rates", return_value=_FAKE_RATES), \
         patch("src.db.save_agile_rates", return_value=len(_FAKE_RATES)), \
         patch("src.weather.compute_pv_calibration_hourly_table",
               return_value={"status": "ok"}) as cal_h, \
         patch("src.weather.compute_pv_calibration_hourly_cloud_table",
               return_value={"status": "ok"}) as cal_c:
        of.fetch_and_store_rates()

    assert cal_h.called
    assert cal_c.called
