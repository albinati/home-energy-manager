"""Guests preset lifts the residual BASE load (not just DHW).

Visitors raise base load (cooking, lights, devices), so the battery must be
provisioned for the higher consumption. `LP_GUESTS_BASE_LOAD_SCALE` multiplies
the residual profile ONLY when mode=guests; normal/vacation are a bit-identical
no-op. Mirrors the documented 'guests -> 1.3' intent. Both base-load builders
(optimizer + lp_simulation) apply it.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.config import config
from src.scheduler import lp_simulation
from src.scheduler.optimizer import _guests_base_load_scale


def test_helper_noop_for_normal_and_vacation(monkeypatch):
    monkeypatch.setattr(config, "LP_GUESTS_BASE_LOAD_SCALE", 1.3, raising=False)
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "normal", raising=False)
    assert _guests_base_load_scale() == 1.0
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "vacation", raising=False)
    assert _guests_base_load_scale() == 1.0


def test_helper_applies_scale_in_guests(monkeypatch):
    monkeypatch.setattr(config, "LP_GUESTS_BASE_LOAD_SCALE", 1.3, raising=False)
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "guests", raising=False)
    assert _guests_base_load_scale() == 1.3


def test_helper_runtime_tunable(monkeypatch):
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "guests", raising=False)
    monkeypatch.setattr(config, "LP_GUESTS_BASE_LOAD_SCALE", 1.5, raising=False)
    assert _guests_base_load_scale() == 1.5


def _slots(n=4):
    t0 = datetime(2026, 6, 17, 18, 0, tzinfo=UTC)
    return [t0 + timedelta(minutes=30 * i) for i in range(n)]


def test_simulation_builder_normal_bit_identical(monkeypatch):
    """Normal preset → the simulation base load is the median × operator scale,
    with the guests factor a bit-identical no-op (×1.0)."""
    monkeypatch.setattr(lp_simulation.db, "residual_load_profile_v2", lambda *a, **k: {})
    monkeypatch.setattr(lp_simulation.db, "lookup_residual_kwh", lambda *a, **k: 0.40)
    monkeypatch.setattr(config, "LP_LOAD_SCALE_FACTOR", 1.0, raising=False)
    monkeypatch.setattr(config, "LP_GUESTS_BASE_LOAD_SCALE", 1.3, raising=False)
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "normal", raising=False)
    out = lp_simulation._build_load_profile(_slots())
    assert all(abs(v - 0.40) < 1e-9 for v in out), out


def test_simulation_builder_scales_in_guests(monkeypatch):
    """Guests preset → the simulation base load is lifted by the guests factor."""
    monkeypatch.setattr(lp_simulation.db, "residual_load_profile_v2", lambda *a, **k: {})
    monkeypatch.setattr(lp_simulation.db, "lookup_residual_kwh", lambda *a, **k: 0.40)
    monkeypatch.setattr(config, "LP_LOAD_SCALE_FACTOR", 1.0, raising=False)
    monkeypatch.setattr(config, "LP_GUESTS_BASE_LOAD_SCALE", 1.3, raising=False)
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "guests", raising=False)
    out = lp_simulation._build_load_profile(_slots())
    assert all(abs(v - 0.40 * 1.3) < 1e-9 for v in out), out


def test_simulation_builder_combines_with_operator_scale(monkeypatch):
    """The guests factor multiplies ON TOP of the operator LP_LOAD_SCALE_FACTOR."""
    monkeypatch.setattr(lp_simulation.db, "residual_load_profile_v2", lambda *a, **k: {})
    monkeypatch.setattr(lp_simulation.db, "lookup_residual_kwh", lambda *a, **k: 0.40)
    monkeypatch.setattr(config, "LP_LOAD_SCALE_FACTOR", 1.1, raising=False)
    monkeypatch.setattr(config, "LP_GUESTS_BASE_LOAD_SCALE", 1.3, raising=False)
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "guests", raising=False)
    out = lp_simulation._build_load_profile(_slots())
    assert all(abs(v - 0.40 * 1.1 * 1.3) < 1e-9 for v in out), out
