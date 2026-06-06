"""LP_LOAD_SCALE_FACTOR — operator load-forecast multiplier.

Covers the runtime-tunable knob wiring (config / SCHEMA / workbench whitelist)
and that /optimization/inputs reflects the scale (no-op at 1.0).
"""
from __future__ import annotations

import asyncio

import pytest

from src import db, runtime_settings as rts
from src.config import config
from src.scheduler import lp_overrides


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db.config, "DB_PATH", str(db_path))
    db.init_db()
    rts.clear_cache()
    config._overrides.clear()
    yield db_path
    rts.clear_cache()
    config._overrides.clear()


def test_knob_is_registered_everywhere():
    assert "LP_LOAD_SCALE_FACTOR" in rts.SCHEMA
    assert "LP_LOAD_SCALE_FACTOR" in lp_overrides.WHITELIST
    assert "LP_LOAD_SCALE_FACTOR" in lp_overrides.promotable_keys()
    # New "load" group surfaces for the workbench UI.
    assert any(s.group == "load" for s in lp_overrides.WHITELIST.values())


def test_default_is_one_and_round_trips():
    assert config.LP_LOAD_SCALE_FACTOR == 1.0
    rts.set_setting("LP_LOAD_SCALE_FACTOR", 1.3)
    assert config.LP_LOAD_SCALE_FACTOR == 1.3
    # Range guard 0.5..2.0
    with pytest.raises(rts.SettingValidationError):
        rts.set_setting("LP_LOAD_SCALE_FACTOR", 0.1)
    with pytest.raises(rts.SettingValidationError):
        rts.set_setting("LP_LOAD_SCALE_FACTOR", 3.0)


def _base_loads_from_inputs() -> list[float]:
    from src.api.main import optimization_inputs
    resp = asyncio.run(optimization_inputs())
    return [s["base_load_kwh"] for s in resp["slots"] if s["base_load_kwh"] is not None]


def _flat_v2(value: float = 0.5) -> dict:
    """A residual_load_profile_v2 object whose every (h,m) leaf is ``value``,
    so lookups resolve to it regardless of day-of-week."""
    return {
        "profile": {(h, m): value for h in range(24) for m in (0, 30)},
        "spread": {(h, m): value for h in range(24) for m in (0, 30)},
        "flat": value, "away_days": [], "day_counts": {},
        "calibrated_days": 0, "physics_only_days": 0,
    }


def test_optimization_inputs_scales_base_load(monkeypatch):
    # Constant residual profile so the scale is unambiguous.
    monkeypatch.setattr(db, "residual_load_profile_v2", lambda *a, **k: _flat_v2(0.5))

    rts.set_setting("LP_LOAD_SCALE_FACTOR", 1.0)
    base = _base_loads_from_inputs()
    assert base, "expected slots from /optimization/inputs"
    assert all(abs(v - 0.5) < 1e-6 for v in base), "1.0 must be a no-op"

    rts.set_setting("LP_LOAD_SCALE_FACTOR", 2.0)
    scaled = _base_loads_from_inputs()
    assert all(abs(v - 1.0) < 1e-6 for v in scaled), "2.0 must double the residual load"


def test_simulation_load_builder_scales(monkeypatch):
    """The Workbench Simulate path (run_lp_simulation → _build_load_profile)
    MUST honour the scale, else the headline knob is inert in the UI."""
    from datetime import UTC, datetime, timedelta
    from src.scheduler import lp_simulation

    monkeypatch.setattr(db, "residual_load_profile_v2", lambda *a, **k: _flat_v2(0.5))
    starts = [datetime(2026, 6, 1, 0, 0, tzinfo=UTC) + timedelta(minutes=30 * i) for i in range(8)]

    rts.set_setting("LP_LOAD_SCALE_FACTOR", 1.0)
    base = lp_simulation._build_load_profile(starts)
    assert all(abs(v - 0.5) < 1e-6 for v in base), "1.0 no-op"

    rts.set_setting("LP_LOAD_SCALE_FACTOR", 1.5)
    scaled = lp_simulation._build_load_profile(starts)
    assert all(abs(v - 0.75) < 1e-6 for v in scaled), "1.5 must scale the simulate base load"
