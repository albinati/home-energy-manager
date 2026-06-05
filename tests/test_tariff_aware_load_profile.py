"""Tariff-aware residual load profile (Phase B2 / #306 follow-up).

Bucket-by-(hour, minute, slot_kind) so the LP captures household behaviour
that shifts with tariff windows (cooking pulled into cheap quartile, evening
peak avoidance). Falls back to plain (hour, minute) when a kind-specific
bucket has fewer than ``min_samples_per_kind`` samples.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src import db
from src.config import config as app_config


@pytest.fixture(autouse=True)
def _init_db() -> None:
    db.init_db()


def _save_pv_load(ts: datetime, load_kw: float) -> None:
    db.save_pv_realtime_sample(
        captured_at=ts.isoformat().replace("+00:00", "Z"),
        solar_power_kw=0.0,
        soc_pct=50.0,
        load_power_kw=load_kw,
        grid_import_kw=0.0,
        grid_export_kw=0.0,
        battery_charge_kw=0.0,
        battery_discharge_kw=0.0,
        source="test",
    )


def _save_exec_with_kind(ts: datetime, kind: str, agile_p: float = 20.0) -> None:
    db.log_execution(
        {
            "timestamp": ts.isoformat().replace("+00:00", "Z"),
            "consumption_kwh": 0.5,
            "agile_price_pence": agile_p,
            "slot_kind": kind,
        }
    )


def _save_meteo(ts: datetime, temp_c: float = 12.0) -> None:
    """Persist a forecast snapshot covering ts so the residual subtraction
    has an outdoor-temp anchor."""
    fetch_at = (ts - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
    rows = [
        {
            "slot_time": ts.replace(minute=0, second=0, microsecond=0).isoformat().replace("+00:00", "Z"),
            "temp_c": temp_c,
            "solar_w_m2": 0.0,
            "cloud_cover_pct": 50.0,
        }
    ]
    db.save_meteo_forecast_snapshot(fetch_at, rows, mark_latest=True)


def test_tariff_aware_profile_separates_cheap_vs_peak_for_same_clock_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """13:00 cheap-day samples → 0.4 kWh; 13:00 peak-day samples → 1.2 kWh.
    The (h, m, kind) bucket should reflect the difference; (h, m) median
    blends both."""
    monkeypatch.setattr(app_config, "BULLETPROOF_TIMEZONE", "Europe/London")
    base = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)  # 13:00 BST

    # Seed 6 cheap-day samples with low load (cooking moved to cheap window
    # so 13:00 is *more* expensive than usual? wait — let's invert:
    # we'll model "13:00 is cheap; cooking pulls in here, so load is HIGH".
    # 6 cheap-tagged samples with high load.
    for i in range(6):
        ts = base + timedelta(days=i)
        _save_meteo(ts, temp_c=20.0)  # warm so Daikin draw ~ 0
        _save_pv_load(ts, load_kw=2.4)  # 2.4 kW × 0.5 h = 1.2 kWh
        _save_exec_with_kind(ts, kind="cheap", agile_p=4.0)

    # 6 peak-tagged samples with low load (peak avoidance).
    for i in range(6):
        ts = base + timedelta(days=i + 7)
        _save_meteo(ts, temp_c=20.0)
        _save_pv_load(ts, load_kw=0.8)  # 0.8 kW × 0.5 h = 0.4 kWh
        _save_exec_with_kind(ts, kind="peak", agile_p=30.0)

    # Wide window so the FIXED May-2026 seed dates always fall inside the rolling
    # lookback regardless of the wall-clock date the suite runs on (#464 — the
    # default 30-day window made these fail once "today" drifted past June 2026).
    # The fixed May dates are deliberate: May is BST, so 12:00 UTC → 13:00 local
    # deterministically, with no DST-boundary ambiguity across the 13-day span.
    profile = db.tariff_aware_residual_load_profile_kwh(min_samples_per_kind=5, window_days=100_000)

    # Expect both kind-aware buckets present (6 samples each ≥ 5)
    cheap_key = (13, 0, "cheap")
    peak_key = (13, 0, "peak")
    plain_key = (13, 0)
    assert cheap_key in profile, f"missing {cheap_key}"
    assert peak_key in profile, f"missing {peak_key}"
    assert plain_key in profile

    assert profile[cheap_key] == pytest.approx(1.2, abs=0.05), profile[cheap_key]
    assert profile[peak_key] == pytest.approx(0.4, abs=0.05), profile[peak_key]
    # The kind-aware buckets meaningfully diverge — that's the point: LP
    # forecast for "13:00 cheap" uses 1.2 kWh, "13:00 peak" uses 0.4 kWh,
    # capturing the household behavioural shift. Plain median (n=12, lower-
    # median index n//2 = 6th sorted value) equals the high-load group.
    assert profile[cheap_key] != profile[peak_key]
    assert profile[cheap_key] - profile[peak_key] > 0.5  # meaningful divergence


def test_tariff_aware_profile_drops_buckets_below_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Buckets with <5 samples are dropped from the kind-aware index so the
    LP gracefully falls back to the plain (h, m) median rather than over-
    fitting to noise."""
    monkeypatch.setattr(app_config, "BULLETPROOF_TIMEZONE", "Europe/London")
    base = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    # Only 3 cheap samples — below threshold
    for i in range(3):
        ts = base + timedelta(days=i)
        _save_meteo(ts, temp_c=20.0)
        _save_pv_load(ts, load_kw=2.4)
        _save_exec_with_kind(ts, kind="cheap", agile_p=4.0)

    # Wide window — see the note in the sibling test (#464: fixed dates vs the
    # default 30-day rolling lookback).
    profile = db.tariff_aware_residual_load_profile_kwh(min_samples_per_kind=5, window_days=100_000)

    assert (13, 0, "cheap") not in profile
    assert (13, 0) in profile  # plain fallback always available


def test_tariff_aware_profile_returns_empty_when_no_samples() -> None:
    """No history → empty dict. Caller falls back to legacy profile."""
    profile = db.tariff_aware_residual_load_profile_kwh()
    assert profile == {}
