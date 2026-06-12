"""LWT pre-heat demand gate + k-calibration decontamination (#540 quick wins).

June-2026 incident: positive LWT offsets WAKE the compressor the firmware
would have left off (measured heating 0 → 3-8 kWh/day within days of
enabling pre-heat), and the k_per_degc regression then learned from the
heating the offsets caused (k 0.033 → 0.067). These tests pin:
  - the gate closes when natural (non-offset-window) heating is absent,
  - the gate cannot feed on offset-induced heating,
  - contaminated days are excluded from the k regression.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src.config import config as app_config

TZ = ZoneInfo("Europe/London")


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch, tmp_path):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setattr(app_config, "DB_PATH", db_path, raising=False)
    from src import db as _db
    _db.init_db()
    yield


def _seed_2h(date_str: str, bucket: int, kwh_heating: float) -> None:
    from src import db
    db.upsert_daikin_consumption_2hourly(
        date=date_str, bucket_idx=bucket, kwh_total=kwh_heating,
        kwh_heating=kwh_heating, kwh_dhw=0.0, source="onecta",
    )


def _seed_offset_action(start_local: datetime, hours: float, offset: int) -> None:
    from src import db
    start_utc = start_local.astimezone(UTC)
    end_utc = start_utc + timedelta(hours=hours)
    db.upsert_action(
        plan_date=start_local.date().isoformat(),
        start_time=start_utc.isoformat().replace("+00:00", "Z"),
        end_time=end_utc.isoformat().replace("+00:00", "Z"),
        device="daikin", action_type="lwt_preheat",
        params={"lwt_offset": offset, "lp_optimizer": True},
        status="completed",
    )


def _yesterday_local() -> datetime:
    now = datetime.now(TZ)
    return (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


# ---------------------------------------------------------------------------
# measured_space_heating_kwh_excluding_offset_windows
# ---------------------------------------------------------------------------

def test_natural_heating_counts():
    from src import db
    y = _yesterday_local()
    _seed_2h(y.date().isoformat(), 4, 1.5)   # 08-10 local, no offsets anywhere
    assert db.measured_space_heating_kwh_excluding_offset_windows(48) == pytest.approx(1.5)


def test_offset_window_heating_is_excluded():
    """Heating inside a HEM offset window must NOT count as natural demand —
    otherwise the gate feeds on its own output and never closes."""
    from src import db
    y = _yesterday_local()
    _seed_2h(y.date().isoformat(), 4, 1.5)          # 08-10 local
    _seed_offset_action(y.replace(hour=8), 2.0, 3)  # offset covering that bucket
    assert db.measured_space_heating_kwh_excluding_offset_windows(48) == 0.0


def test_mixed_day_keeps_non_offset_buckets():
    """Winter shape: offsets in the cheap window, natural heating elsewhere —
    the natural buckets keep the gate open."""
    from src import db
    y = _yesterday_local()
    _seed_2h(y.date().isoformat(), 1, 2.0)          # 02-04 local — offset window
    _seed_2h(y.date().isoformat(), 9, 1.8)          # 18-20 local — natural
    _seed_offset_action(y.replace(hour=2), 2.0, 3)
    assert db.measured_space_heating_kwh_excluding_offset_windows(48) == pytest.approx(1.8)


def test_zero_offset_actions_do_not_exclude():
    """Restore rows / offset=0 rows are not contamination."""
    from src import db
    y = _yesterday_local()
    _seed_2h(y.date().isoformat(), 4, 1.5)
    _seed_offset_action(y.replace(hour=8), 2.0, 0)
    assert db.measured_space_heating_kwh_excluding_offset_windows(48) == pytest.approx(1.5)


# ---------------------------------------------------------------------------
# _space_heating_demand_present (dispatch gate)
# ---------------------------------------------------------------------------

def test_gate_closed_without_demand(monkeypatch):
    from src.scheduler import lp_dispatch
    assert lp_dispatch._space_heating_demand_present() is False


def test_gate_open_with_natural_demand():
    from src.scheduler import lp_dispatch
    y = _yesterday_local()
    _seed_2h(y.date().isoformat(), 4, 1.5)
    assert lp_dispatch._space_heating_demand_present() is True


def test_gate_disabled_by_zero_floor(monkeypatch):
    from src.scheduler import lp_dispatch
    monkeypatch.setattr(
        app_config, "DAIKIN_LWT_PREHEAT_MIN_TRAILING_HEATING_KWH", 0.0, raising=False
    )
    assert lp_dispatch._space_heating_demand_present() is True


# ---------------------------------------------------------------------------
# k_per_degc decontamination
# ---------------------------------------------------------------------------

def test_k_regression_skips_hem_offset_days(monkeypatch):
    """Days with non-zero lwt_preheat actions are excluded from the sample."""
    from src import db

    # Seed 10 days of heating + meteo coverage; mark 3 as offset days.
    today = datetime.now(TZ).date()
    for back in range(1, 11):
        d = today - timedelta(days=back)
        db.upsert_daikin_consumption_daily(
            date=d.isoformat(), kwh_total=12.0, kwh_heating=10.0, source="onecta",
        )
        fetch = f"{(d - timedelta(days=1)).isoformat()}T12:00:00+00:00"
        db.save_meteo_forecast_history(fetch, [
            {"slot_time": f"{d.isoformat()}T{h:02d}:00:00+00:00",
             "temp_c": 5.0, "solar_w_m2": 0.0, "cloud_cover_pct": 50.0}
            for h in range(24)
        ])
        if back <= 3:
            start_local = datetime.combine(d, datetime.min.time(), tzinfo=TZ).replace(hour=2)
            _seed_offset_action(start_local, 2.0, 3)

    result = db.compute_daikin_lwt_kw_calibration(window_days=12, min_samples=5)
    assert result["status"] == "ok", result
    assert result["skipped_hem_offset"] == 3
    assert result["samples"] <= 7  # 10 seeded − 3 contaminated
