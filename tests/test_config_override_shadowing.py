"""#790 — an in-memory config override must never outlive its context.

Prod, 2026-08-21..23: the operator set ``OPTIMIZATION_PRESET=normal`` in the
UI. SQLite and ``.env`` both said ``normal``. The LP solved as ``vacation`` for
two days, drained the battery to 7 % via vacation-only peak_export arbitrage,
and tripped the #789 Infeasible cascade. Only ``systemctl restart hem`` fixed
it.

Cause: every runtime-tunable key is a property whose setter writes into the
class-level ``Config._overrides``, and ``_rt_get`` short-circuits on that dict
BEFORE consulting ``runtime_settings``. So a Workbench what-if that "restored"
the prior value with ``setattr`` pinned the key for the life of the process —
the 30-sec TTL cache that is supposed to make a UI write land is bypassed
entirely, and nothing is logged.
"""
from __future__ import annotations

import pytest

from src.config import config
from src.runtime_settings import shadowed_settings
from src.scheduler import lp_overrides
from src.scheduler.runner import _evaluate_lp_health


@pytest.fixture(autouse=True)
def _no_leaked_overrides():
    """This suite is about override hygiene — start and end clean."""
    before = config.override_items()
    yield
    for key in set(config.override_items()) - set(before):
        config.clear_override(key)


# ── patched_config must not pin the key ──────────────────────────────────────

def test_patched_config_leaves_no_override_behind() -> None:
    """The exact prod poisoner: a what-if on OPTIMIZATION_PRESET."""
    assert not config.has_override("OPTIMIZATION_PRESET")

    with lp_overrides.patched_config({"OPTIMIZATION_PRESET": "vacation"}):
        assert config.OPTIMIZATION_PRESET == "vacation"
        assert config.has_override("OPTIMIZATION_PRESET")

    # Not merely "the value looks right" — the SHADOW itself must be gone, or
    # the next UI write is silently ignored.
    assert not config.has_override("OPTIMIZATION_PRESET")


def test_patched_config_clears_even_when_the_body_raises() -> None:
    with pytest.raises(RuntimeError):
        with lp_overrides.patched_config({"OPTIMIZATION_PRESET": "vacation"}):
            raise RuntimeError("boom")
    assert not config.has_override("OPTIMIZATION_PRESET")


def test_patched_config_restores_a_pre_existing_override() -> None:
    """When something WAS already shadowing the key, put that back — clearing
    it would be a different bug (silently dropping a deliberate override)."""
    config.OPTIMIZATION_PRESET = "guests"
    try:
        with lp_overrides.patched_config({"OPTIMIZATION_PRESET": "vacation"}):
            assert config.OPTIMIZATION_PRESET == "vacation"
        assert config.has_override("OPTIMIZATION_PRESET")
        assert config.OPTIMIZATION_PRESET == "guests"
    finally:
        config.clear_override("OPTIMIZATION_PRESET")


def test_patched_config_restores_plain_attributes_by_assignment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Not every key need be a runtime property. For a plain attribute the
    override dict is never touched, so the exit path must restore by
    assignment — clearing would leave the PATCHED value live.

    Every whitelist key happens to be a property today, so stage a plain one:
    this guards the branch against the day a non-property key is added.
    """
    from src.scheduler.lp_overrides import OverrideSpec

    monkeypatch.setattr(config, "_TEST_PLAIN_KNOB", 1.0, raising=False)
    monkeypatch.setitem(
        lp_overrides.WHITELIST, "_TEST_PLAIN_KNOB",
        OverrideSpec(
            key="_TEST_PLAIN_KNOB", config_attr="_TEST_PLAIN_KNOB",
            type_name="float", min_value=0.0, max_value=10.0,
            description="test-only", group="solver",
        ),
    )
    assert not config.has_override("_TEST_PLAIN_KNOB")

    with lp_overrides.patched_config({"_TEST_PLAIN_KNOB": 7.0}):
        assert config._TEST_PLAIN_KNOB == 7.0

    # Restored by assignment, not silently left at 7.0.
    assert config._TEST_PLAIN_KNOB == 1.0
    assert not config.has_override("_TEST_PLAIN_KNOB")


# ── the missing signal ───────────────────────────────────────────────────────

def test_shadowed_settings_reports_a_divergence() -> None:
    assert shadowed_settings() == []
    config.OPTIMIZATION_PRESET = "vacation"
    try:
        rows = shadowed_settings()
        assert len(rows) == 1
        assert rows[0]["key"] == "OPTIMIZATION_PRESET"
        assert rows[0]["in_memory"] == "vacation"
        assert rows[0]["persisted"] != "vacation"
    finally:
        config.clear_override("OPTIMIZATION_PRESET")
    assert shadowed_settings() == []


def test_shadowed_settings_ignores_an_agreeing_override() -> None:
    """An override that matches the DB is harmless — don't cry wolf."""
    config.OPTIMIZATION_PRESET = config.OPTIMIZATION_PRESET
    try:
        assert shadowed_settings() == []
    finally:
        config.clear_override("OPTIMIZATION_PRESET")


def test_health_monitor_pages_on_a_shadowed_setting() -> None:
    """The wire: a divergence reaches the alert channel that already pages,
    naming the remedy. Silence is what made this cost two days."""
    issues = _evaluate_lp_health(
        infeasible_24h=0, neg_slot_count=0, neg_discharge_kwh=0.0,
        max_infeasible=5, neg_discharge_thr=0.5,
        shadowed=[{"key": "OPTIMIZATION_PRESET",
                   "in_memory": "vacation", "persisted": "normal"}],
    )
    assert len(issues) == 1
    assert "OPTIMIZATION_PRESET" in issues[0]
    assert "vacation" in issues[0] and "normal" in issues[0]
    assert "restart" in issues[0]


def test_health_monitor_silent_when_nothing_is_shadowed() -> None:
    assert _evaluate_lp_health(
        infeasible_24h=0, neg_slot_count=0, neg_discharge_kwh=0.0,
        max_infeasible=5, neg_discharge_thr=0.5, shadowed=[],
    ) == []
