"""COP lift pre-processing (#29) — pure physics, no PuLP."""
from __future__ import annotations

import pytest

from src.physics import apply_cop_lift_multiplier


def test_lift_disabled_returns_cop_base_unchanged() -> None:
    assert apply_cop_lift_multiplier(
        3.2,
        5.0,
        45.0,
        penalty_per_k=0.0,
        reference_delta_k=25.0,
        min_mult=0.5,
    ) == pytest.approx(3.2)


def test_lift_reduces_cop_when_high_lift() -> None:
    out = apply_cop_lift_multiplier(
        4.0,
        -5.0,
        50.0,
        penalty_per_k=0.01,
        reference_delta_k=25.0,
        min_mult=0.5,
    )
    assert out < 4.0
    assert out >= 1.0


def test_lift_never_below_one() -> None:
    out = apply_cop_lift_multiplier(
        1.2,
        -20.0,
        55.0,
        penalty_per_k=0.5,
        reference_delta_k=0.0,
        min_mult=0.1,
    )
    assert out >= 1.0
