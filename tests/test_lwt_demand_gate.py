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
    # Anchor to the SAME date basis the function uses (compute_daikin_lwt_kw_
    # calibration windows off date.today(), system-local) — not Europe/London —
    # else at the UTC↔BST day boundary the most-recent offset day lands a day
    # outside the calibration window and skipped_hem_offset reads 2 not 3 (flake).
    today = datetime.now().date()
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


# ---------------------------------------------------------------------------
# Review regressions (#541): H1 active-status windows, H2 plan-date spill,
# M1 contaminated daily fallback
# ---------------------------------------------------------------------------

def test_active_window_is_excluded_h1():
    """A window that is LIVE right now sits at status='active' — exactly when
    overlapping re-plans evaluate the gate. It must be excluded ('in_flight'
    is never a stored status; lifecycle is pending→active→completed/failed)."""
    from src import db
    y = _yesterday_local()
    _seed_2h(y.date().isoformat(), 4, 1.5)
    start_utc = y.replace(hour=8).astimezone(UTC)
    db.upsert_action(
        plan_date=y.date().isoformat(),
        start_time=start_utc.isoformat().replace("+00:00", "Z"),
        end_time=(start_utc + timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
        device="daikin", action_type="lwt_preheat",
        params={"lwt_offset": 3}, status="active",
    )
    assert db.measured_space_heating_kwh_excluding_offset_windows(48) == 0.0


def test_midnight_spill_window_is_excluded_h2():
    """A rolling plan written in the evening files midnight-spilling windows
    under the PREVIOUS plan_date. A lookback whose start date is after that
    plan_date must still see the window (left-edge pad), or its heating
    re-counts as natural demand → every-other-day gate oscillation."""
    from src import db
    y = _yesterday_local()                       # lookback covers yesterday+today
    plan_day = (y - timedelta(days=1)).date()    # filed under the day BEFORE lookback start
    _seed_2h(y.date().isoformat(), 0, 2.0)       # 00-02 local yesterday
    start_utc = y.replace(hour=0).astimezone(UTC)
    db.upsert_action(
        plan_date=plan_day.isoformat(),
        start_time=start_utc.isoformat().replace("+00:00", "Z"),
        end_time=(start_utc + timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
        device="daikin", action_type="lwt_preheat",
        params={"lwt_offset": 3}, status="completed",
    )
    assert db.measured_space_heating_kwh_excluding_offset_windows(48) == 0.0


def test_daily_fallback_refuses_contaminated_data_m1():
    """2-hourly split missing + offset windows present → daily totals cannot
    be decontaminated; must return 0 (gate-closed direction), never the
    offset-induced daily heating."""
    from src import db
    y = _yesterday_local()
    db.upsert_daikin_consumption_daily(
        date=y.date().isoformat(), kwh_total=6.0, kwh_heating=6.0, source="onecta",
    )
    _seed_offset_action(y.replace(hour=2), 2.0, 3)
    assert db.measured_space_heating_kwh_excluding_offset_windows(48) == 0.0


def test_daily_fallback_clean_data_still_counts():
    """2-hourly missing but NO offsets in range → daily totals are clean and
    keep the gate responsive (winter cold-start case)."""
    from src import db
    y = _yesterday_local()
    db.upsert_daikin_consumption_daily(
        date=y.date().isoformat(), kwh_total=6.0, kwh_heating=6.0, source="onecta",
    )
    assert db.measured_space_heating_kwh_excluding_offset_windows(48) == pytest.approx(6.0)


# ---------------------------------------------------------------------------
# Outdoor cutoff on POSITIVE offsets (Tracked by #540) — the exogenous gate
# the measured-demand self-loop cannot fool (June-2026 phantom heating).
# ---------------------------------------------------------------------------

_THR = dict(cheap_thr=8.0, peak_thr=30.0)


@pytest.fixture
def _preheat_cfg(monkeypatch):
    monkeypatch.setattr(app_config, "DAIKIN_LWT_PREHEAT_ENABLED", True, raising=False)
    monkeypatch.setattr(app_config, "DAIKIN_LWT_PREHEAT_BOOST_C", 3, raising=False)
    monkeypatch.setattr(app_config, "DAIKIN_LWT_PREHEAT_NEGATIVE_BOOST_C", 10, raising=False)
    monkeypatch.setattr(app_config, "DAIKIN_LWT_PREHEAT_PEAK_SETBACK_C", -2, raising=False)
    monkeypatch.setattr(app_config, "DAIKIN_LWT_PREHEAT_COMFORT_BAND_C", 1.0, raising=False)
    monkeypatch.setattr(app_config, "OPTIMIZATION_LWT_OFFSET_MIN", -10, raising=False)
    monkeypatch.setattr(app_config, "OPTIMIZATION_LWT_OFFSET_MAX", 10, raising=False)
    monkeypatch.setattr(app_config, "DAIKIN_LWT_PREHEAT_OUTDOOR_CUTOFF_C", 15.0, raising=False)


def test_positive_boost_suppressed_when_warm(_preheat_cfg):
    from src.scheduler.lp_dispatch import _preheat_lwt_offset as f
    assert f(2.0, 19.0, **_THR) == 0


def test_positive_boost_allowed_when_cold(_preheat_cfg):
    from src.scheduler.lp_dispatch import _preheat_lwt_offset as f
    assert f(2.0, 5.0, **_THR) == 3


def test_negprice_boost_suppressed_when_warm(_preheat_cfg):
    from src.scheduler.lp_dispatch import _preheat_lwt_offset as f
    assert f(-2.0, 19.0, **_THR) == 0


def test_negprice_boost_allowed_when_cold(_preheat_cfg):
    from src.scheduler.lp_dispatch import _preheat_lwt_offset as f
    assert f(-2.0, 2.0, **_THR) == 10


def test_peak_setback_allowed_when_warm(_preheat_cfg):
    """The −2 setback can only let the unit coast — never cut by the cutoff."""
    from src.scheduler.lp_dispatch import _preheat_lwt_offset as f
    assert f(35.0, 25.0, **_THR) == -2


def test_peak_setback_allowed_when_cold(_preheat_cfg):
    from src.scheduler.lp_dispatch import _preheat_lwt_offset as f
    assert f(35.0, 0.0, **_THR) == -2


def test_cutoff_boundary_is_inclusive(_preheat_cfg):
    """outdoor == cutoff suppresses (>=)."""
    from src.scheduler.lp_dispatch import _preheat_lwt_offset as f
    assert f(2.0, 15.0, **_THR) == 0


def test_cutoff_disabled_high_value(_preheat_cfg, monkeypatch):
    monkeypatch.setattr(app_config, "DAIKIN_LWT_PREHEAT_OUTDOOR_CUTOFF_C", 99.0, raising=False)
    from src.scheduler.lp_dispatch import _preheat_lwt_offset as f
    assert f(2.0, 30.0, **_THR) == 3


def test_pairs_no_positive_window_when_warm(_preheat_cfg, monkeypatch):
    """Integration: a warm forecast yields NO positive lwt_preheat windows even
    when prices are cheap — closes the self-loop at the source."""
    monkeypatch.setattr(app_config, "DAIKIN_LWT_PREHEAT_MIN_BLOCK_SLOTS", 4, raising=False)
    from src.scheduler import lp_dispatch
    from src.scheduler.lp_optimizer import LpPlan
    starts = [datetime(2026, 6, 12, 9, tzinfo=UTC) + i * timedelta(minutes=30) for i in range(6)]
    warm = LpPlan(
        ok=True, status="Optimal", objective_pence=0.0, slot_starts_utc=starts,
        price_pence=[2.0] * 6, temp_outdoor_c=[19.0] * 6,
        cheap_threshold_pence=8.0, peak_threshold_pence=30.0,
    )
    assert lp_dispatch._lwt_preheat_pairs(warm, []) == []
    cold = LpPlan(
        ok=True, status="Optimal", objective_pence=0.0, slot_starts_utc=starts,
        price_pence=[2.0] * 6, temp_outdoor_c=[4.0] * 6,
        cheap_threshold_pence=8.0, peak_threshold_pence=30.0,
    )
    assert len(lp_dispatch._lwt_preheat_pairs(cold, [])) == 1  # one boost window


# ---------------------------------------------------------------------------
# Thermal-lag tail exclusion in the decontamination
# ---------------------------------------------------------------------------

def test_decontam_excludes_tail_bucket(monkeypatch):
    """Heat bleeding into the bucket AFTER an offset window closes must not
    count as natural demand (default tail = 1 bucket)."""
    monkeypatch.setattr(app_config, "DAIKIN_LWT_PREHEAT_DECONTAM_TAIL_BUCKETS", 1, raising=False)
    from src import db
    y = _yesterday_local()
    _seed_2h(y.date().isoformat(), 5, 1.5)          # 10-12 local — just after window
    _seed_offset_action(y.replace(hour=8), 2.0, 3)  # window 08-10 (bucket 4)
    assert db.measured_space_heating_kwh_excluding_offset_windows(48) == 0.0


def test_decontam_tail_zero_keeps_old_behaviour(monkeypatch):
    monkeypatch.setattr(app_config, "DAIKIN_LWT_PREHEAT_DECONTAM_TAIL_BUCKETS", 0, raising=False)
    from src import db
    y = _yesterday_local()
    _seed_2h(y.date().isoformat(), 5, 1.5)
    _seed_offset_action(y.replace(hour=8), 2.0, 3)
    assert db.measured_space_heating_kwh_excluding_offset_windows(48) == pytest.approx(1.5)


# ---------------------------------------------------------------------------
# space_heating_gate_state surfaces the outdoor cutoff
# ---------------------------------------------------------------------------

def test_gate_state_reports_outdoor_cutoff(monkeypatch):
    """Demand gate OPEN (natural heat seeded) but warm outside → POSITIVE offsets
    are flagged suppressed-by-outdoor, while preheat_suppressed stays scoped to
    the demand gate (the −2 setback still fires, so it is NOT 'all off')."""
    from src import db
    from src.scheduler import lp_dispatch
    monkeypatch.setattr(app_config, "DAIKIN_LWT_PREHEAT_ENABLED", True, raising=False)
    monkeypatch.setattr(app_config, "DAIKIN_LWT_PREHEAT_OUTDOOR_CUTOFF_C", 15.0, raising=False)
    y = _yesterday_local()
    _seed_2h(y.date().isoformat(), 4, 1.5)  # natural demand → demand gate open
    monkeypatch.setattr(db, "get_latest_daikin_telemetry", lambda *, source=None: {"outdoor_temp_c": 19.0})
    st = lp_dispatch.space_heating_gate_state()
    assert st["demand_present"] is True
    assert st["outdoor_cutoff_c"] == 15.0
    assert st["current_outdoor_c"] == 19.0
    assert st["positive_offset_suppressed_by_outdoor"] is True
    # Demand gate is OPEN → not "all off"; the setback still fires.
    assert st["preheat_suppressed"] is False


def test_gate_state_preheat_suppressed_when_demand_absent(monkeypatch):
    """No measured demand → preheat_suppressed True (the demand gate shuts ALL
    offset rows, setback included)."""
    from src import db
    from src.scheduler import lp_dispatch
    monkeypatch.setattr(app_config, "DAIKIN_LWT_PREHEAT_ENABLED", True, raising=False)
    monkeypatch.setattr(db, "get_latest_daikin_telemetry", lambda *, source=None: {"outdoor_temp_c": 5.0})
    st = lp_dispatch.space_heating_gate_state()
    assert st["demand_present"] is False
    assert st["preheat_suppressed"] is True
