"""Tests for PR A — mode collapse.

OPTIMIZATION_PRESET collapsed from 4 values (normal/guests/travel/away)
to 3 (normal/guests/vacation). Legacy values are translated transparently
at the runtime_settings read path so pre-existing DB rows stay readable.
``OperationPreset._missing_`` covers any direct enum instantiation with
the legacy string.
"""
from __future__ import annotations

import pytest

from src import db
from src import runtime_settings as rts
from src.config import config
from src.presets import HouseholdMode, OperationPreset


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch, tmp_path):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setattr(config, "DB_PATH", db_path, raising=False)
    db.init_db()
    # Reset module-level dedup so each test starts with a clean slate
    # for legacy-translation log checking.
    rts._LEGACY_TRANSLATION_LOGGED.clear()
    from src.presets import _LEGACY_LOGGED
    _LEGACY_LOGGED.clear()
    # Clear runtime_settings cache AND the class-level config._overrides
    # so a previous test's `config.OPTIMIZATION_PRESET = ...` setter call
    # doesn't bleed into this test's read.
    rts._cache.clear()
    type(config)._overrides.clear()
    yield
    type(config)._overrides.clear()


# ---------------------------------------------------------------------------
# Enum members + alias
# ---------------------------------------------------------------------------


def test_household_mode_aliases_operation_preset():
    """`HouseholdMode` is the forward-looking name; `OperationPreset` is
    kept for back-compat in 18+ existing files. Both point at the same enum."""
    assert HouseholdMode is OperationPreset
    assert HouseholdMode.NORMAL is OperationPreset.NORMAL
    assert HouseholdMode.VACATION.value == "vacation"


def test_three_canonical_members():
    """PR A collapsed TRAVEL/AWAY into VACATION. Enum should have exactly 3."""
    values = {m.value for m in OperationPreset}
    assert values == {"normal", "guests", "vacation"}


# ---------------------------------------------------------------------------
# `_missing_` legacy translator (direct enum calls)
# ---------------------------------------------------------------------------


def test_enum_missing_translates_travel_to_vacation():
    """Code paths that do ``OperationPreset("travel")`` keep working."""
    assert OperationPreset("travel") is OperationPreset.VACATION


def test_enum_missing_translates_away_to_vacation():
    assert OperationPreset("away") is OperationPreset.VACATION


def test_enum_missing_keeps_boost_mapping():
    """The pre-existing v10 'boost' deprecation still maps to NORMAL."""
    assert OperationPreset("boost") is OperationPreset.NORMAL


def test_enum_missing_unknown_value_raises():
    """An actual typo should still raise — the translator only covers
    intentionally-deprecated values."""
    with pytest.raises(ValueError):
        OperationPreset("flibble")


# ---------------------------------------------------------------------------
# Runtime-settings read-path translator (pre-existing DB rows)
# ---------------------------------------------------------------------------


def test_legacy_value_in_db_reads_as_vacation():
    """Pre-PR-A rows stored 'travel' or 'away' in the DB; after PR A the
    setter rejects them but the reader transparently maps them to 'vacation'."""
    # Side-channel write that bypasses validation, simulating a row written
    # by an older release.
    db.set_runtime_setting("OPTIMIZATION_PRESET", "travel")
    rts._cache.clear()  # force re-read from DB
    assert rts.get_setting("OPTIMIZATION_PRESET") == "vacation"


def test_legacy_value_away_also_translates():
    db.set_runtime_setting("OPTIMIZATION_PRESET", "away")
    rts._cache.clear()
    assert rts.get_setting("OPTIMIZATION_PRESET") == "vacation"


def test_canonical_values_pass_through():
    """No translation for the 3 valid values — must round-trip exactly."""
    for v in ("normal", "guests", "vacation"):
        rts.set_setting("OPTIMIZATION_PRESET", v)
        assert rts.get_setting("OPTIMIZATION_PRESET") == v


def test_setter_rejects_legacy_values():
    """The setter enforces the schema enum; legacy values can only enter
    the DB via direct SQL (the back-compat case the read translator covers)."""
    with pytest.raises(rts.SettingValidationError):
        rts.set_setting("OPTIMIZATION_PRESET", "travel")
    with pytest.raises(rts.SettingValidationError):
        rts.set_setting("OPTIMIZATION_PRESET", "away")


# ---------------------------------------------------------------------------
# config.OPTIMIZATION_PRESET property uses the translator too
# ---------------------------------------------------------------------------


def test_config_property_reads_translated_value():
    """The runtime_settings translator is single-source-of-truth — the
    config property must also see 'vacation' for a stored 'travel'."""
    db.set_runtime_setting("OPTIMIZATION_PRESET", "travel")
    rts._cache.clear()
    assert config.OPTIMIZATION_PRESET == "vacation"
