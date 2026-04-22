"""Physics-based Daikin state estimator (#55)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from math import exp

from src.config import config
from src.daikin.estimator import estimate_state


def _seed(
    *,
    fetched_at: float,
    tank: float = 50.0,
    indoor: float = 21.0,
    outdoor: float | None = 8.0,
) -> dict:
    return {
        "fetched_at": fetched_at,
        "tank_temp_c": tank,
        "indoor_temp_c": indoor,
        "outdoor_temp_c": outdoor,
    }


def test_estimator_zero_age_returns_seed_exactly():
    """No time elapsed → output must equal the seed (rounded to 2 dp)."""
    now = datetime(2026, 4, 22, 12, 0, tzinfo=UTC)
    seed = _seed(fetched_at=now.timestamp(), tank=55.4, indoor=20.6, outdoor=9.0)
    est = estimate_state(seed, now)
    assert est.source == "estimate"
    assert abs(est.tank_temp_c - 55.4) < 0.01
    assert abs(est.indoor_temp_c - 20.6) < 0.01
    assert est.seed_age_seconds == 0.0


def test_estimator_tank_decays_toward_indoor():
    """Exponential decay with UA_tank/C_tank time constant."""
    now = datetime(2026, 4, 22, 12, 0, tzinfo=UTC)
    dt_sec = 6 * 3600.0  # 6 h
    seed = _seed(fetched_at=now.timestamp() - dt_sec, tank=55.0, indoor=21.0, outdoor=8.0)
    est = estimate_state(seed, now)

    c_tank = float(config.DHW_TANK_LITRES) * float(config.DHW_WATER_CP)
    k_tank = float(config.DHW_TANK_UA_W_PER_K) / c_tank
    expected = 21.0 + (55.0 - 21.0) * exp(-k_tank * dt_sec)
    # Tank must lose some heat (< seed) but approach indoor, not cross it.
    assert 21.0 < est.tank_temp_c < 55.0
    assert abs(est.tank_temp_c - expected) < 0.01


def test_estimator_indoor_decays_toward_outdoor_mean():
    """Indoor uses mean meteo outdoor. Same exponential form."""
    now = datetime(2026, 4, 22, 12, 0, tzinfo=UTC)
    dt_sec = 12 * 3600.0
    seed = _seed(
        fetched_at=now.timestamp() - dt_sec, tank=50.0, indoor=21.0, outdoor=None
    )
    meteo = [{"temp_c": 6.0}, {"temp_c": 10.0}, {"temp_c": 8.0}]  # mean = 8.0
    est = estimate_state(seed, now, meteo_rows=meteo)

    c_bld = float(config.BUILDING_THERMAL_MASS_KWH_PER_K) * 3.6e6
    k_bld = float(config.BUILDING_UA_W_PER_K) / c_bld
    expected = 8.0 + (21.0 - 8.0) * exp(-k_bld * dt_sec)
    assert 8.0 < est.indoor_temp_c < 21.0
    assert abs(est.indoor_temp_c - expected) < 0.01
    assert est.outdoor_temp_c == 8.0


def test_estimator_holds_indoor_when_no_outdoor_available():
    """With neither meteo_rows nor outdoor in the seed: fall back to holding."""
    now = datetime(2026, 4, 22, 12, 0, tzinfo=UTC)
    seed = _seed(fetched_at=now.timestamp() - 3600, tank=50.0, indoor=21.0, outdoor=None)
    est = estimate_state(seed, now)  # no meteo, no default_outdoor_c
    assert est.indoor_temp_c == 21.0  # no decay target → hold
    assert est.outdoor_temp_c is None


def test_estimator_falls_back_to_config_defaults_when_seed_missing_fields():
    """Robustness: caller might pass a partial seed row. The walk should still
    return a plausible state using config defaults (never crash)."""
    now = datetime(2026, 4, 22, 12, 0, tzinfo=UTC)
    sparse = {"fetched_at": now.timestamp() - 1800}  # no temps at all
    est = estimate_state(sparse, now)
    # Seed equals config defaults → at 30 min of decay, temps barely move.
    assert abs(est.tank_temp_c - float(config.DHW_TEMP_NORMAL_C)) < 2.0
    assert abs(est.indoor_temp_c - float(config.INDOOR_SETPOINT_C)) < 0.5


def test_estimator_accuracy_within_half_degree_at_3h():
    """Acceptance criterion from #55: within 0.5 °C at 3–6 h for passive decay.
    Hand-check the 3-hour horizon at a known seed."""
    now = datetime(2026, 4, 22, 12, 0, tzinfo=UTC)
    dt_sec = 3 * 3600.0
    seed = _seed(fetched_at=now.timestamp() - dt_sec, tank=52.0, indoor=21.0, outdoor=7.5)
    est = estimate_state(seed, now)
    # Closed-form expectation.
    c_tank = float(config.DHW_TANK_LITRES) * float(config.DHW_WATER_CP)
    k_tank = float(config.DHW_TANK_UA_W_PER_K) / c_tank
    expected_tank = 21.0 + (52.0 - 21.0) * exp(-k_tank * dt_sec)
    assert abs(est.tank_temp_c - expected_tank) < 0.5


def test_estimator_seed_age_is_non_negative_on_clock_skew():
    """If caller passes ``now_utc`` before the seed, clamp age to 0 rather
    than raise or return past-extrapolated values."""
    now = datetime(2026, 4, 22, 12, 0, tzinfo=UTC)
    future_seed = _seed(fetched_at=(now + timedelta(seconds=30)).timestamp())
    est = estimate_state(future_seed, now)
    assert est.seed_age_seconds == 0.0
