"""Passive mode (def1) — every Daikin write path must be a no-op.

Pinned via ``DAIKIN_CONTROL_MODE=passive`` (default in v10). Telemetry/read paths
must keep working. Active mode is tested as a regression at the end.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from src import db
from src.config import config
from src.daikin import service as daikin_service
from src.daikin.client import DaikinError
from src.daikin.models import DaikinDevice
from src.daikin_bulletproof import apply_comfort_restore, apply_scheduled_daikin_params
from src.runtime_settings import clear_cache


@pytest.fixture(autouse=True)
def _init_db() -> None:
    db.init_db()
    clear_cache()


@pytest.fixture
def passive_mode(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DAIKIN_CONTROL_MODE", "passive")
    clear_cache()
    yield
    clear_cache()


@pytest.fixture
def active_mode(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DAIKIN_CONTROL_MODE", "active")
    clear_cache()
    yield
    clear_cache()


# ---------------------------------------------------------------------------
# S2: bulletproof apply paths
# ---------------------------------------------------------------------------

def test_apply_scheduled_passive_skip(passive_mode) -> None:
    dev = DaikinDevice(id="gw", name="x", is_on=True, lwt_offset=0.0)
    client = MagicMock()
    result = apply_scheduled_daikin_params(
        dev, client, params={"lwt_offset": 2.0, "climate_on": True}, trigger="t"
    )
    assert result is False
    client.set_power.assert_not_called()
    client.set_lwt_offset.assert_not_called()


def test_apply_comfort_restore_passive_skip(passive_mode) -> None:
    dev = DaikinDevice(id="gw", name="x", is_on=True, lwt_offset=0.0, tank_target=45.0)
    client = MagicMock()
    apply_comfort_restore(dev, client, trigger="t")
    client.set_power.assert_not_called()
    client.set_tank_temperature.assert_not_called()
    client.set_tank_power.assert_not_called()


# ---------------------------------------------------------------------------
# S2: every public mutator on daikin/service.py
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fn_name,args", [
    ("set_power", (True,)),
    ("set_temperature", (21.0,)),
    ("set_lwt_offset", (0.0,)),
    ("set_operation_mode", ("heating",)),
    ("set_tank_temperature", (45.0,)),
    ("set_tank_power", (True,)),
    ("set_tank_powerful", (False,)),
    ("set_weather_regulation", (True,)),
])
def test_service_setters_passive_skip(passive_mode, fn_name: str, args: tuple) -> None:
    fn = getattr(daikin_service, fn_name)
    with pytest.raises(DaikinError, match="DAIKIN_CONTROL_MODE=passive"):
        fn(*args)


# ---------------------------------------------------------------------------
# S2: scheduler tick
# ---------------------------------------------------------------------------

def test_scheduler_tick_passive_skip(passive_mode, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.scheduler import daikin as sched_daikin

    # Ensure the rate-fetch path would otherwise be exercised so the early-return
    # is the only reason no error is raised.
    monkeypatch.setattr(config, "OCTOPUS_TARIFF_CODE", "TEST")
    monkeypatch.setattr(config, "SCHEDULER_ENABLED", True)
    fake = MagicMock(side_effect=AssertionError("rates fetch should not run in passive"))
    monkeypatch.setattr(sched_daikin, "fetch_agile_rates", fake)

    result = sched_daikin.run_daikin_scheduler_tick(is_paused=False)
    assert result is None
    fake.assert_not_called()


# ---------------------------------------------------------------------------
# S2: MCP preamble
# ---------------------------------------------------------------------------

def test_mcp_preamble_passive_blocks(passive_mode) -> None:
    from src.mcp_server import _daikin_write_preamble
    result = _daikin_write_preamble("test.action", {"on": True})
    assert result is not None
    assert result["ok"] is False
    assert result.get("passive_mode") is True
    assert "passive" in result["error"].lower()


# ---------------------------------------------------------------------------
# S3: LP clamps Daikin variables to predicted load when passive
# ---------------------------------------------------------------------------

def _solve_minimal(passive: bool):
    """Run a 4-slot LP solve and return the LpPlan."""
    from src.scheduler.lp_initial_state import LpInitialState
    from src.scheduler.lp_optimizer import solve_lp
    from src.weather import WeatherLpSeries

    n = 4
    start = datetime(2026, 4, 23, 12, 0, tzinfo=UTC)
    slots = [start + timedelta(minutes=30 * i) for i in range(n)]
    weather = WeatherLpSeries(
        slot_starts_utc=slots,
        temperature_outdoor_c=[5.0] * n,
        shortwave_radiation_wm2=[200.0] * n,
        cloud_cover_pct=[40.0] * n,
        pv_kwh_per_slot=[0.5] * n,
        cop_space=[3.0] * n,
        cop_dhw=[2.5] * n,
    )
    init = LpInitialState(soc_kwh=5.0, tank_temp_c=48.0, indoor_temp_c=21.0)
    return solve_lp(
        slot_starts_utc=slots,
        price_pence=[10.0, 5.0, 25.0, 8.0],
        base_load_kwh=[0.4] * n,
        weather=weather,
        initial=init,
        tz=ZoneInfo("Europe/London"),
    )


def test_lp_clamps_daikin_in_passive(passive_mode) -> None:
    from src.physics import predict_passive_daikin_load

    plan = _solve_minimal(passive=True)
    assert plan.ok and plan.status == "Optimal"

    n = len(plan.dhw_electric_kwh)
    exp_space, exp_dhw = predict_passive_daikin_load(
        [5.0] * n, [2.5] * n, [3.0] * n, slot_h=0.5,
        max_kwh_per_slot=2.0 * 0.5,  # config.DAIKIN_MAX_HP_KW * slot_h
    )
    for i in range(n):
        assert abs(plan.space_electric_kwh[i] - exp_space[i]) < 1e-3, (
            f"space[{i}] mismatch: {plan.space_electric_kwh[i]} vs predicted {exp_space[i]}"
        )
        assert abs(plan.dhw_electric_kwh[i] - exp_dhw[i]) < 1e-3, (
            f"dhw[{i}] mismatch: {plan.dhw_electric_kwh[i]} vs predicted {exp_dhw[i]}"
        )


def test_lp_active_mode_free_variables(active_mode) -> None:
    """In active mode the LP can choose to skip DHW (e.g. tank already warm)."""
    plan = _solve_minimal(passive=False)
    assert plan.ok
    # With no shower window and tank at 48°C, an unconstrained LP typically
    # skips DHW entirely — proves variables are NOT clamped to a non-zero predicted load.
    assert sum(plan.dhw_electric_kwh) < 1e-3, (
        f"active LP should be free to skip DHW; got {plan.dhw_electric_kwh}"
    )


# ---------------------------------------------------------------------------
# S4: daikin status surfaces control_mode
# ---------------------------------------------------------------------------

def test_daikin_status_response_includes_control_mode() -> None:
    from src.api.models import DaikinStatusResponse
    fields = DaikinStatusResponse.model_fields
    assert "control_mode" in fields
    assert fields["control_mode"].default == "passive"


# ---------------------------------------------------------------------------
# S4: BOOST deprecation alias maps to NORMAL
# ---------------------------------------------------------------------------

def test_boost_preset_alias_maps_to_normal(caplog) -> None:
    import logging
    from src.presets import OperationPreset
    with caplog.at_level(logging.WARNING):
        result = OperationPreset("boost")
    assert result == OperationPreset.NORMAL
    assert any("boost" in rec.message.lower() and "deprecated" in rec.message.lower()
               for rec in caplog.records), f"missing deprecation warning: {caplog.records}"


# ---------------------------------------------------------------------------
# S4: legacy hard-coded legionella env vars stay gone; new shape is runtime-mutable
# ---------------------------------------------------------------------------

def test_legacy_legionella_attrs_still_removed() -> None:
    """The old v9-shape attrs were the LP-commands-the-cycle design. Keep them
    out of `config` — anyone still wiring code against them is using the wrong
    abstraction. The replacement is runtime_settings (mutable, prediction-only).
    """
    for attr in (
        "DHW_LEGIONELLA_TEMP_C",
        "DHW_LEGIONELLA_HOUR_START",
        "DHW_LEGIONELLA_HOUR_END",
    ):
        assert not hasattr(config, attr), f"{attr} should not be a static config attr"


def test_legionella_runtime_settings_present() -> None:
    """Re-introduced as runtime_settings (mutable via PUT /api/v1/settings + MCP).
    Used by predict_passive_daikin_load to inject a one-shot DHW pulse.
    """
    from src import runtime_settings as rts
    for key in (
        "DHW_LEGIONELLA_DAY",
        "DHW_LEGIONELLA_HOUR_LOCAL",
        "DHW_LEGIONELLA_DURATION_MIN",
        "DHW_LEGIONELLA_TANK_TARGET_C",
    ):
        assert key in rts.SCHEMA, f"{key} should be a runtime_setting"
    assert rts.get_setting("DHW_LEGIONELLA_DAY") == -1, "default disabled"
