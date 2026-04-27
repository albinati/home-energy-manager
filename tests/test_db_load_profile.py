"""db.half_hourly_load_profile_kwh — half-hour median load from real Fox telemetry.

S10.9 (#176) switched the source from execution_log (100% estimated, ~0.4 default)
to pv_realtime_history (real Fox load_power_kw samples). Tests pin the data
source + half-hour bucketing.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src import db


@pytest.fixture(autouse=True)
def _init_db() -> None:
    db.init_db()


def _seed_pv_realtime(t: datetime, load_kw: float) -> None:
    db.save_pv_realtime_sample(
        t.isoformat().replace("+00:00", "Z"),
        solar_power_kw=0.0,
        soc_pct=50.0,
        load_power_kw=load_kw,
    )


def test_load_profile_reads_from_pv_realtime_history() -> None:
    """Profile must reflect real Fox samples, not the estimator default."""
    base = datetime.now(UTC) - timedelta(days=2)
    # Hour 14 UTC: 3 samples averaging to a clear non-default value
    for offset_min, kw in [(0, 1.0), (10, 1.0), (20, 1.0)]:
        _seed_pv_realtime(base.replace(hour=14, minute=0) + timedelta(minutes=offset_min), kw)

    prof = db.half_hourly_load_profile_kwh(window_days=14)
    # 1.0 kW × 0.5h = 0.5 kWh/slot — must reflect real samples, not 0.4 fallback
    # Note: bucket is in BULLETPROOF_TIMEZONE (Europe/London = BST = UTC+1 in summer)
    # so UTC 14:00 maps to local 15:00 in summer
    found = False
    for (h, m), v in prof.items():
        if abs(v - 0.5) < 0.01:
            found = True
            break
    assert found, f"expected a slot bucket near 0.5 kWh from seeded samples; got {sorted(prof.values())[:8]}..."


def test_load_profile_distinguishes_half_hours() -> None:
    """Samples at HH:00 and HH:30 should produce different bucket values."""
    base = datetime.now(UTC) - timedelta(days=2)
    # Hour 10 UTC at minute 0: 1 sample at 0.4 kW
    _seed_pv_realtime(base.replace(hour=10, minute=0), 0.4)
    # Hour 10 UTC at minute 30: 1 sample at 1.2 kW
    _seed_pv_realtime(base.replace(hour=10, minute=30), 1.2)

    prof = db.half_hourly_load_profile_kwh(window_days=14)
    # Find the two buckets that contain our distinct values (0.2 and 0.6 kWh/slot)
    expected = {0.2, 0.6}
    found_distinct = sum(1 for v in prof.values() if any(abs(v - e) < 0.01 for e in expected))
    assert found_distinct >= 2, (
        f"expected separate buckets for HH:00 and HH:30 with values 0.2 and 0.6 kWh/slot; "
        f"got profile values {sorted(set(round(v,3) for v in prof.values()))[:10]}"
    )


def test_load_profile_falls_back_to_global_median_when_bucket_empty() -> None:
    """Buckets with no samples get the global median rather than a zero/None."""
    base = datetime.now(UTC) - timedelta(days=2)
    # Seed only 1 bucket; all 47 others must fall back to global median (= 0.5 here)
    _seed_pv_realtime(base.replace(hour=10, minute=0), 1.0)
    prof = db.half_hourly_load_profile_kwh(window_days=14)
    # Every bucket should have a value; none should be 0
    assert all(v > 0 for v in prof.values()), f"some bucket has zero value: {[(k,v) for k,v in prof.items() if v == 0]}"
    # The empty buckets fall back to the global median, which equals our single sample → 0.5 kWh/slot
    other_bucket = prof[(0, 0)]  # hour 0:00 — not seeded
    assert abs(other_bucket - 0.5) < 0.01, f"empty bucket should fall back to 0.5 kWh/slot (global median); got {other_bucket}"
