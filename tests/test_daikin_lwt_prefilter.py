"""Daikin LWT offset pre-filter — skip the PATCH when the device reports
the characteristic as non-settable.

Onecta returns 400 READ_ONLY_CHARACTERISTIC for a leavingWaterOffset
write whenever setpointMode ≠ weatherDependent (i.e. the curve isn't
active). Issuing the PATCH anyway costs a 200/day quota slot for nothing,
so we pre-filter via the ``settable`` flag Onecta reports in the range
block.

These tests pin the pre-filter so it doesn't silently regress.
"""
from __future__ import annotations

import pytest

from src import db
from src.daikin.models import DaikinDevice, SetpointRange
from src.daikin_bulletproof import apply_scheduled_daikin_params


@pytest.fixture(autouse=True)
def _db_ready(monkeypatch):
    db.init_db()
    from src.config import config as _config
    from src.runtime_settings import clear_cache

    # DAIKIN_CONTROL_MODE is a property backed by a class-level _overrides
    # dict; we set the override directly so the property getter returns
    # "active" for this test. OPENCLAW_READ_ONLY is a plain dataclass field
    # evaluated at class definition (os.getenv is captured once), so
    # monkeypatch.setattr is the right tool for that one.
    _config._overrides.pop("DAIKIN_CONTROL_MODE", None)
    _config._overrides["DAIKIN_CONTROL_MODE"] = "active"
    monkeypatch.setattr(_config, "OPENCLAW_READ_ONLY", False)
    clear_cache()
    yield
    # Full cleanup so sibling tests (setenv("DAIKIN_CONTROL_MODE=passive")
    # in test_daikin_passive_mode.py) aren't shadowed by a leftover override.
    _config._overrides.pop("DAIKIN_CONTROL_MODE", None)
    try:
        db.delete_runtime_setting("DAIKIN_CONTROL_MODE")
    except Exception:
        pass
    clear_cache()


class _Recorder:
    """Duck-typed DaikinClient — records method names the bulletproof path calls."""
    def __init__(self):
        self.calls: list[str] = []

    def __getattr__(self, name):
        def _record(*args, **kwargs):
            self.calls.append(name)
            return None
        return _record


def _mk_device(**overrides) -> DaikinDevice:
    dev = DaikinDevice(id="dev-1", name="altherma", model="Altherma", is_on=True)
    for k, v in overrides.items():
        setattr(dev, k, v)
    return dev


def _apply(dev, params, monkeypatch) -> _Recorder:
    client = _Recorder()
    # Skip valve-settle sleeps to keep tests fast. OPENCLAW_READ_ONLY and
    # DAIKIN_CONTROL_MODE are pinned via the fixture's setenv.
    monkeypatch.setattr("src.config.config.DAIKIN_VALVE_SETTLE_SECONDS", 0)
    monkeypatch.setattr("src.daikin_bulletproof.daikin_device_matches_params", lambda d, p: False)
    monkeypatch.setattr("src.daikin_bulletproof.detect_user_override", lambda d, p: (False, None))
    # Signature is apply_scheduled_daikin_params(dev, client, params, *, trigger)
    apply_scheduled_daikin_params(dev, client, params, trigger="test")
    return client


def test_lwt_patch_skipped_when_range_settable_false(monkeypatch):
    dev = _mk_device(
        lwt_offset_range=SetpointRange(min_value=-10, max_value=10, step_value=1, settable=False),
    )
    client = _apply(dev, {"lwt_offset": 5.0}, monkeypatch)
    assert "set_lwt_offset" not in client.calls


def test_lwt_patch_issued_when_range_settable_true(monkeypatch):
    dev = _mk_device(
        lwt_offset_range=SetpointRange(min_value=-10, max_value=10, step_value=1, settable=True),
    )
    client = _apply(dev, {"lwt_offset": 5.0}, monkeypatch)
    assert "set_lwt_offset" in client.calls


def test_lwt_patch_skipped_when_climate_turning_off(monkeypatch):
    # Pre-existing check: zone_will_be_on=False → skip even if settable.
    dev = _mk_device(
        is_on=True,
        lwt_offset_range=SetpointRange(min_value=-10, max_value=10, step_value=1, settable=True),
    )
    client = _apply(dev, {"lwt_offset": 5.0, "climate_on": False}, monkeypatch)
    assert "set_lwt_offset" not in client.calls


def test_lwt_read_only_error_is_still_caught_as_fallback(monkeypatch):
    """Stale cache case: settable=True but live device rejects. Pre-filter
    can't catch it, so the post-facto catch must still log & continue."""
    dev = _mk_device(
        lwt_offset_range=SetpointRange(min_value=-10, max_value=10, step_value=1, settable=True),
    )
    # Simulate the client raising [read_only] on the PATCH.
    from src.daikin.client import DaikinError
    class _ReadOnlyClient(_Recorder):
        def set_lwt_offset(self, dev, offset):
            self.calls.append("set_lwt_offset")
            raise DaikinError("[read_only] leavingWaterOffset")
    client = _ReadOnlyClient()
    monkeypatch.setattr("src.config.config.DAIKIN_VALVE_SETTLE_SECONDS", 0)
    monkeypatch.setattr("src.daikin_bulletproof.daikin_device_matches_params", lambda d, p: False)
    monkeypatch.setattr("src.daikin_bulletproof.detect_user_override", lambda d, p: (False, None))
    # Must not raise despite the [read_only] error.
    apply_scheduled_daikin_params(dev, client, {"lwt_offset": 5.0, "tank_temp": 45.0}, trigger="test")
    assert "set_lwt_offset" in client.calls
    # Tank command should still have fired — [read_only] on LWT must not abort the rest.
    assert "set_tank_temperature" in client.calls
