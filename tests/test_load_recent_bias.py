"""Phase-2 load recent-bias corrector (additive, per-local-hour, closed-loop).

Seeds load_error_log rows directly with a known per-local-hour additive bias and
checks: warm-start jumps to the measured bias; accumulation nudges the previous
value; the clamp bounds it; the backtest reduces MAE on structured bias.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src import db, load_bias
from src.config import config as app_config

LON = ZoneInfo("Europe/London")


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(app_config, "DB_PATH", str(tmp_path / "t.db"), raising=False)
    db.init_db()


def _seed_error_row(slot_utc: datetime, forecast: float, actual: float, base: float | None = None) -> None:
    key = slot_utc.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    base = forecast if base is None else base  # default: base==total → low Daikin
    with db._lock:
        conn = db.get_connection()
        try:
            conn.execute(
                """INSERT OR REPLACE INTO load_error_log
                   (slot_time_utc, forecast_kwh, forecast_base_kwh, actual_kwh, error_kwh, built_at_utc)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (key, forecast, base, actual, actual - forecast, "2026-06-15T00:00:00Z"),
            )
            conn.commit()
        finally:
            conn.close()


def _seed_constant_bias(*, utc_hour: int, forecast: float, bias: float, n_days: int = 10,
                        base: float | None = None) -> int:
    """Seed n_days of one slot at a fixed UTC hour with actual = forecast + bias.
    ``base`` controls committed Daikin (total−base); default base==forecast → low.
    Returns the LOCAL hour those slots fall in."""
    now = datetime.now(UTC)
    local_hour = None
    for d in range(1, n_days + 1):
        slot = (now - timedelta(days=d)).replace(hour=utc_hour, minute=0, second=0, microsecond=0)
        _seed_error_row(slot, forecast, forecast + bias, base=base)
        local_hour = slot.astimezone(LON).hour
    return local_hour


def test_warm_start_jumps_to_measured_bias() -> None:
    lh = _seed_constant_bias(utc_hour=12, forecast=0.5, bias=0.2)
    applied, raw, samples, diag = load_bias.compute_load_recent_bias_by_hour_local()
    assert lh in applied
    assert raw[lh] == pytest.approx(0.2, abs=0.01)
    assert applied[lh] == pytest.approx(0.2, abs=0.01)  # warm start = raw


def test_accumulates_on_previous() -> None:
    lh = _seed_constant_bias(utc_hour=12, forecast=0.5, bias=0.2)
    # Pretend a previous correction of 0.1 already exists.
    db.upsert_load_recent_bias({lh: 0.1}, {lh: 0.1}, {lh: 5}, "2026-06-15T00:00:00Z")
    applied, raw, _s, _d = load_bias.compute_load_recent_bias_by_hour_local()
    # new = prev(0.1) + damping(0.5) * raw(0.2) = 0.2
    assert applied[lh] == pytest.approx(0.1 + 0.5 * 0.2, abs=0.01)


def test_clamped_to_max() -> None:
    lh = _seed_constant_bias(utc_hour=12, forecast=1.0, bias=5.0)  # absurd bias
    applied, _r, _s, _d = load_bias.compute_load_recent_bias_by_hour_local()
    assert applied[lh] == pytest.approx(app_config.LOAD_RECENT_BIAS_MAX_KWH)


def test_below_min_days_dropped() -> None:
    _seed_constant_bias(utc_hour=12, forecast=0.5, bias=0.2, n_days=2)  # 2 days < MIN (3)
    applied, _r, _s, _d = load_bias.compute_load_recent_bias_by_hour_local()
    assert applied == {}


def test_high_daikin_slots_excluded() -> None:
    """A slot whose committed heat-pump load (total−base) is large must NOT teach
    the base corrector — its error isn't the base's."""
    # forecast 1.0, base 0.3 → committed Daikin 0.7 (>> 0.1 threshold).
    lh = _seed_constant_bias(utc_hour=12, forecast=1.0, bias=0.3, n_days=10, base=0.3)
    applied, _r, _s, _d = load_bias.compute_load_recent_bias_by_hour_local()
    assert lh not in applied  # excluded → no correction learned for that hour


def test_refresh_persists_round_trip() -> None:
    lh = _seed_constant_bias(utc_hour=12, forecast=0.5, bias=0.2)
    n = load_bias.refresh_load_recent_bias()
    assert n >= 1
    got = db.get_load_recent_bias()
    assert got[lh] == pytest.approx(0.2, abs=0.01)


def test_backtest_reduces_mae_on_structured_bias() -> None:
    # Two hours with opposite bias (nets to ~0 overall, like the real diurnal pattern).
    _seed_constant_bias(utc_hour=2, forecast=0.4, bias=-0.15, n_days=20)   # overnight over
    _seed_constant_bias(utc_hour=13, forecast=0.5, bias=+0.18, n_days=20)  # midday under
    res = load_bias.backtest_load_recent_bias()
    ins = res["in_sample"]
    assert ins["n_slots"] >= 20
    # In-sample: corrector removes the per-hour bias → MAE drops materially.
    assert ins["after"]["mae_kwh"] < ins["before"]["mae_kwh"]
    assert ins["mae_reduction_kwh"] > 0.1
    # Out-of-sample present (older→recent holdout) and also improves on stable bias.
    oos = res["out_of_sample"]
    assert oos is not None
    assert oos["after"]["mae_kwh"] < oos["before"]["mae_kwh"]


def test_corrector_off_by_default() -> None:
    assert app_config.LOAD_RECENT_BIAS_ENABLED is False
