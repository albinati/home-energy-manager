"""LP price quantization must be conservative (audit finding #5).

Symmetric ``round(p/qp)*qp`` collapses prices like -0.2p to 0p (with the
default qp=0.5p), removing the LP's incentive to import during slightly-
negative slots. Conservative rounding: negatives → floor (more negative),
positives → ceil (more expensive). LP plans for "slightly worse than
realised" pricing so realised cost can only beat the plan.
"""
from __future__ import annotations

import math

import pytest

from src.config import config as app_config


def _quantize(p: float, qp: float = 0.5) -> float:
    """Mirror the LP's quantization logic for unit-testable verification."""
    if qp <= 0:
        return p
    if p < 0:
        return math.floor(p / qp) * qp
    return math.ceil(p / qp) * qp


def test_small_negative_does_not_collapse_to_zero() -> None:
    """The audit-cited bug: -0.2p with qp=0.5 used to round to 0p, losing
    the import incentive. Conservative floor → -0.5p (still incentivises)."""
    assert _quantize(-0.2, qp=0.5) == -0.5
    assert _quantize(-0.4, qp=0.5) == -0.5
    assert _quantize(-0.6, qp=0.5) == -1.0


def test_small_positive_rounds_up_conservatively() -> None:
    """A 0.2p import price → ceil to 0.5p (slightly more expensive). LP
    plans as if it costs 0.5p, realised is 0.2p → realised beats plan."""
    assert _quantize(0.2, qp=0.5) == 0.5
    assert _quantize(0.4, qp=0.5) == 0.5
    assert _quantize(0.6, qp=0.5) == 1.0


def test_zero_stays_zero() -> None:
    assert _quantize(0.0, qp=0.5) == 0.0


def test_exact_grid_values_unchanged() -> None:
    """Prices already on the quantization grid should be unchanged either way."""
    for p in (-2.0, -1.5, -0.5, 0.5, 1.0, 1.5, 5.0, 17.85, 22.5):
        # Multiples of 0.5 should round-trip exactly
        if abs(p / 0.5 - round(p / 0.5)) < 1e-9:
            assert _quantize(p, qp=0.5) == p, f"on-grid {p} should be unchanged"


def test_quantization_preserves_negative_signal_on_realistic_agile_slots() -> None:
    """A run of realistic Agile prices including small-magnitude negatives:
    ALL negative slots must remain negative after quantization (so the LP
    still sees the import incentive). Pre-fix, -0.4p collapsed to 0p."""
    realistic = [-1.5, -0.8, -0.4, -0.2, 0.0, 0.2, 0.5, 5.0, 18.5, 32.7]
    quantized = [_quantize(p, qp=0.5) for p in realistic]
    # Every input < 0 must produce output < 0
    for orig, q in zip(realistic, quantized):
        if orig < 0:
            assert q < 0, (
                f"Original {orig}p quantized to {q}p — negative signal lost! "
                "LP would no longer prefer this slot for grid-charge."
            )


def test_lp_solve_uses_conservative_quantization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: solve a tiny LP with a small-negative-price slot and
    confirm the LP charges the battery there (would not happen if the
    price were collapsed to 0p, since cycling penalty + battery loss
    make zero-price charge break-even at best)."""
    from datetime import UTC, datetime, timedelta
    from zoneinfo import ZoneInfo

    from src.scheduler.lp_optimizer import LpInitialState, solve_lp
    from src.weather import WeatherLpSeries

    monkeypatch.setattr(app_config, "LP_HIGHS_TIME_LIMIT_SECONDS", 15)
    monkeypatch.setattr(app_config, "LP_INVERTER_STRESS_COST_PENCE", 0.0)
    monkeypatch.setattr(app_config, "LP_HP_MIN_ON_SLOTS", 1)
    monkeypatch.setattr(app_config, "LP_SOC_TERMINAL_VALUE_PENCE_PER_KWH", 0.0)
    monkeypatch.setattr(app_config, "LP_PRICE_QUANTIZE_PENCE", 0.5)

    n = 12
    base = datetime(2026, 6, 1, 0, 0, tzinfo=UTC)
    starts = [base + i * timedelta(minutes=30) for i in range(n)]
    # Slot 5 has -0.4p — pre-fix collapsed to 0p (no incentive to charge).
    # Other slots: 15p (positive, not particularly cheap).
    prices = [15.0] * 5 + [-0.4] + [15.0] * 6
    base_load = [0.3] * n

    weather = WeatherLpSeries(
        slot_starts_utc=starts,
        temperature_outdoor_c=[15.0] * n,
        shortwave_radiation_wm2=[400.0] * n,
        cloud_cover_pct=[40.0] * n,
        pv_kwh_per_slot=[0.5] * n,
        cop_space=[3.5] * n,
        cop_dhw=[3.0] * n,
    )
    init = LpInitialState(
        soc_kwh=2.5, tank_temp_c=48.0, indoor_temp_c=20.5,
    )

    plan = solve_lp(
        slot_starts_utc=starts,
        price_pence=prices,
        base_load_kwh=base_load,
        weather=weather,
        initial=init,
        tz=ZoneInfo("UTC"),
        export_price_pence=[5.0] * n,
    )
    assert plan.ok, f"LP failed: {plan.status}"

    # The negative slot (slot 5) should attract grid charge — the LP sees
    # -0.5p (post-quantize) as legitimately profitable to grid-import.
    chg_neg = plan.battery_charge_kwh[5]
    chg_pos_avg = sum(plan.battery_charge_kwh[i] for i in range(n) if i != 5) / 11
    assert chg_neg > chg_pos_avg, (
        f"Negative slot charge {chg_neg:.3f} should exceed mean positive-slot "
        f"charge {chg_pos_avg:.3f} when quantization preserves the signal. "
        "Pre-fix: -0.4p collapsed to 0p → no preferential charge here."
    )
