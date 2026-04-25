"""Runtime-tunable settings — PUT→property read round-trip, cron hot-reload (#52)."""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from src import db, runtime_settings as rts
from src.config import config


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(db.config, "DB_PATH", str(db_path))
    db.init_db()
    rts.clear_cache()
    config._overrides.clear()  # in-memory config overrides leak across tests
    yield db_path
    rts.clear_cache()
    config._overrides.clear()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_validate_rejects_unknown_key():
    with pytest.raises(rts.SettingValidationError):
        rts.set_setting("NOT_A_KNOB", 42)


def test_validate_enforces_range():
    # DHW_TEMP_COMFORT_C: 40..65
    with pytest.raises(rts.SettingValidationError):
        rts.set_setting("DHW_TEMP_COMFORT_C", 30)
    with pytest.raises(rts.SettingValidationError):
        rts.set_setting("DHW_TEMP_COMFORT_C", 80)


def test_validate_enforces_enum():
    with pytest.raises(rts.SettingValidationError):
        rts.set_setting("OPTIMIZATION_PRESET", "noprep")


def test_validate_coerces_int_list_from_csv():
    rts.set_setting("LP_MPC_HOURS", "12,6,21,6")
    assert rts.get_setting("LP_MPC_HOURS") == [6, 12, 21]


def test_validate_coerces_int_list_from_python_list():
    rts.set_setting("LP_MPC_HOURS", [21, 6, 12])
    assert rts.get_setting("LP_MPC_HOURS") == [6, 12, 21]


# ---------------------------------------------------------------------------
# DB persistence + cache invalidation
# ---------------------------------------------------------------------------


def test_get_setting_falls_back_to_env_default_when_db_empty(monkeypatch):
    monkeypatch.delenv("DHW_TEMP_COMFORT_C", raising=False)
    # Simulate "no override in DB" — default from the schema's env_default lambda.
    rts.clear_cache()
    assert rts.get_setting("DHW_TEMP_COMFORT_C") == 48.0  # schema default


def test_put_then_get_round_trip():
    rts.set_setting("DHW_TEMP_COMFORT_C", 52.0)
    assert rts.get_setting("DHW_TEMP_COMFORT_C") == 52.0


def test_put_invalidates_cache_via_version_counter(monkeypatch):
    """Two successive PUTs within the TTL window must both be visible — the
    version bump is what makes the cache invalidate, not the TTL."""
    rts.set_setting("DHW_TEMP_COMFORT_C", 50.0)
    assert rts.get_setting("DHW_TEMP_COMFORT_C") == 50.0
    rts.set_setting("DHW_TEMP_COMFORT_C", 55.0)
    assert rts.get_setting("DHW_TEMP_COMFORT_C") == 55.0


def test_delete_reverts_to_env_default():
    rts.set_setting("DHW_TEMP_COMFORT_C", 55.0)
    assert rts.get_setting("DHW_TEMP_COMFORT_C") == 55.0
    removed = rts.delete_setting("DHW_TEMP_COMFORT_C")
    assert removed is True
    assert rts.get_setting("DHW_TEMP_COMFORT_C") == 48.0  # env default


def test_delete_returns_false_when_no_row():
    assert rts.delete_setting("DHW_TEMP_COMFORT_C") is False


# ---------------------------------------------------------------------------
# Config property integration (no call-site churn)
# ---------------------------------------------------------------------------


def test_config_property_reads_runtime_value():
    rts.set_setting("DHW_TEMP_COMFORT_C", 52.5)
    assert config.DHW_TEMP_COMFORT_C == 52.5


def test_config_property_reads_lp_mpc_hours_list():
    rts.set_setting("LP_MPC_HOURS", "4,10,15")
    assert config.LP_MPC_HOURS_LIST == [4, 10, 15]
    # The legacy string form is derived from the canonical list.
    assert config.LP_MPC_HOURS == "4,10,15"


def test_config_enum_property_round_trip():
    rts.set_setting("OPTIMIZATION_PRESET", "guests")
    assert config.OPTIMIZATION_PRESET == "guests"
    rts.set_setting("OPTIMIZATION_PRESET", "normal")
    assert config.OPTIMIZATION_PRESET == "normal"


# ---------------------------------------------------------------------------
# list_settings shape
# ---------------------------------------------------------------------------


def test_list_settings_marks_overridden(monkeypatch):
    rts.set_setting("DHW_TEMP_COMFORT_C", 53.0)
    rows = {r["key"]: r for r in rts.list_settings()}
    assert rows["DHW_TEMP_COMFORT_C"]["overridden"] is True
    assert rows["DHW_TEMP_COMFORT_C"]["value"] == 53.0
    # A key we haven't touched stays overridden=False.
    assert rows["INDOOR_SETPOINT_C"]["overridden"] is False


# ---------------------------------------------------------------------------
# Cron hot-reload (LP_MPC_HOURS / LP_PLAN_PUSH_HOUR / LP_PLAN_PUSH_MINUTE)
# ---------------------------------------------------------------------------


def test_cron_reload_reregisters_jobs_when_active(monkeypatch):
    """Simulate an active background scheduler and assert the job set is rebuilt
    with the new LP_MPC_HOURS without tearing down the rest."""
    from src.scheduler import runner

    # Build a minimal fake scheduler with the jobs the function tears down.
    class _Job:
        def __init__(self, jid): self.id = jid

    class _Sched:
        def __init__(self, initial_ids):
            self._jobs = [_Job(j) for j in initial_ids]
            self.removed: list[str] = []
            self.added: list[str] = []
        def get_jobs(self):
            return list(self._jobs)
        def remove_job(self, jid):
            self._jobs = [j for j in self._jobs if j.id != jid]
            self.removed.append(jid)
        def add_job(self, func, trigger, *, id):  # noqa: A002
            self._jobs.append(_Job(id))
            self.added.append(id)

    fake = _Sched([
        "bulletproof_octopus_fetch",   # unrelated — must be preserved
        "bulletproof_plan_push",
        "bulletproof_mpc_06",
        "bulletproof_mpc_12",
        "bulletproof_mpc_21",
    ])
    monkeypatch.setattr(runner, "_background_scheduler", fake)
    monkeypatch.setattr(runner.config, "USE_BULLETPROOF_ENGINE", True)

    rts.set_setting("LP_MPC_HOURS", [4, 10, 15])
    result = runner.reregister_cron_jobs(reason="test")

    assert result["status"] == "ok"
    assert sorted(fake.removed) == sorted([
        "bulletproof_plan_push",
        "bulletproof_mpc_06",
        "bulletproof_mpc_12",
        "bulletproof_mpc_21",
    ])
    # New MPC jobs match the freshly-written setting; push job re-added too.
    assert set(fake.added) == {
        "bulletproof_mpc_04",
        "bulletproof_mpc_10",
        "bulletproof_mpc_15",
        "bulletproof_plan_push",
    }
    # Unrelated job is untouched.
    assert any(j.id == "bulletproof_octopus_fetch" for j in fake.get_jobs())


def test_lp_soc_final_kwh_default_scales_with_battery_capacity(monkeypatch):
    """Default = 25 % of BATTERY_CAPACITY_KWH when no explicit override is set."""
    import src.runtime_settings as rts
    from src.config import config

    # Clear any in-memory override and any DB row so we exercise the env_default path.
    monkeypatch.setattr(config, "_overrides", {}, raising=False)
    rts.delete_setting("LP_SOC_FINAL_KWH")
    monkeypatch.delenv("LP_SOC_FINAL_KWH", raising=False)
    monkeypatch.setenv("BATTERY_CAPACITY_KWH", "12")
    rts._cache.pop("LP_SOC_FINAL_KWH", None)

    assert rts.get_setting("LP_SOC_FINAL_KWH") == 3.0  # 25 % of 12 = 3.0

    # Explicit env override beats the percentage default.
    monkeypatch.setenv("LP_SOC_FINAL_KWH", "1.5")
    rts._cache.pop("LP_SOC_FINAL_KWH", None)
    assert rts.get_setting("LP_SOC_FINAL_KWH") == 1.5


def test_lp_soc_final_kwh_runtime_tunable_round_trip(monkeypatch):
    """PUT via runtime_settings persists; config.LP_SOC_FINAL_KWH reads it back."""
    import src.runtime_settings as rts
    from src.config import config

    monkeypatch.setattr(config, "_overrides", {}, raising=False)
    rts.set_setting("LP_SOC_FINAL_KWH", 4.2)
    assert config.LP_SOC_FINAL_KWH == 4.2
    # cleanup so other tests don't see the override
    rts.delete_setting("LP_SOC_FINAL_KWH")


def test_cron_reload_is_noop_when_scheduler_inactive(monkeypatch):
    from src.scheduler import runner
    monkeypatch.setattr(runner, "_background_scheduler", None)
    result = runner.reregister_cron_jobs(reason="test")
    assert result["status"] == "inactive"
