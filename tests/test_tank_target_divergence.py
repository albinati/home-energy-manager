"""Tests for the 2026-07-28 tank-setpoint divergence + cold-morning alerts.

Both sit at the end of ``_reconcile_daikin_actions``. They exist because of a
real incident: on 2026-07-27 the tank target was moved 37 -> 30 from outside HEM
at 22:32Z and nothing noticed for 14 h — the household woke to a 31 °C tank.

The two pre-existing override mechanisms structurally miss that case:

* the covering ``tank_setback`` row was already ``completed`` (pre-fire noop),
  and the reconciler loop only revisits pending/active rows;
* ``detect_user_override`` only arms for rows applied in the current process.

The load-bearing property, and the household's explicit constraint, is that
neither check may spend Daikin API quota: they read the heartbeat's existing
device snapshot plus ``daikin_telemetry``. ``test_*_costs_no_daikin_calls``
pins that.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from src import db
from src import state_machine as sm
from src.config import config


@dataclass
class _FakeDev:
    id: str = "dev-1"
    name: str = "Altherma"
    tank_on: bool | None = True
    tank_target: float | None = None
    tank_temperature: float | None = None
    tank_powerful: bool | None = False
    is_on: bool | None = True
    lwt_offset: float | None = None


@pytest.fixture(autouse=True)
def _isolated_db(monkeypatch, tmp_path):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setattr(config, "DB_PATH", db_path, raising=False)
    monkeypatch.setattr(config, "TANK_TARGET_DIVERGENCE_CHECK_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "TANK_TARGET_DIVERGENCE_TOLERANCE_C", 1.0, raising=False)
    monkeypatch.setattr(config, "MORNING_TANK_COLD_CHECK_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "MORNING_TANK_COLD_CHECK_START_HOUR_UTC", 5, raising=False)
    monkeypatch.setattr(config, "MORNING_TANK_COLD_CHECK_END_HOUR_UTC", 7, raising=False)
    monkeypatch.setattr(config, "DAIKIN_CONTROL_MODE", "active", raising=False)
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "normal", raising=False)
    monkeypatch.setattr(config, "BULLETPROOF_TIMEZONE", "Europe/London", raising=False)
    db.init_db()
    sm._TANK_TARGET_DIVERGENCE_NOTIFIED = None
    yield


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _seed_setback_row(now: datetime, tank_temp: int = 37) -> None:
    """A `tank_setback` row covering ``now`` — filed, like prod, as COMPLETED.

    This is the shape the incident had: the pre-fire state match completes the
    row the moment it fires, so any check that only looks at pending/active rows
    is blind to the rest of the window.
    """
    start = now - timedelta(hours=6)
    end = now + timedelta(hours=12)
    db.upsert_action(
        plan_date=start.astimezone(UTC).date().isoformat(),
        start_time=_iso(start),
        end_time=_iso(end),
        device="daikin",
        action_type="tank_setback",
        params={
            "tank_power": True,
            "tank_temp": tank_temp,
            "tank_powerful": False,
            "dhw_policy": True,
        },
        status="completed",
    )


def _seed_one(at: datetime, target: float, tank: float = 40.0) -> None:
    """A single live telemetry row at an exact instant."""
    db.insert_daikin_telemetry({
        "fetched_at": at.timestamp(),
        "source": "live",
        "tank_temp_c": tank,
        "tank_target_c": target,
        "outdoor_temp_c": 15.0,
        "indoor_temp_c": None,
        "lwt_actual_c": 27.0,
        "mode": "heating",
        "weather_regulation": 1,
    })


def _seed_telemetry(now: datetime, target: float, *, hours: float, tank: float = 40.0) -> None:
    """Live telemetry rows holding ``target`` for the last ``hours``."""
    n = max(2, int(hours * 2))
    for i in range(n):
        ts = now - timedelta(hours=hours) + timedelta(hours=hours * i / max(1, n - 1))
        db.insert_daikin_telemetry({
            "fetched_at": ts.timestamp(),
            "source": "live",
            "tank_temp_c": tank,
            "tank_target_c": target,
            "outdoor_temp_c": 15.0,
            "indoor_temp_c": None,
            "lwt_actual_c": 27.0,
            "mode": "heating",
            "weather_regulation": 1,
        })


# ---------------------------------------------------------------------------
# Setpoint divergence
# ---------------------------------------------------------------------------


def test_divergence_fires_on_the_incident_shape(monkeypatch) -> None:
    """37 planned, 30 live, held for hours, covering row already `completed`."""
    now = datetime(2026, 7, 28, 6, 55, tzinfo=UTC)
    _seed_setback_row(now, tank_temp=37)
    _seed_telemetry(now, target=30.0, hours=8.0, tank=31.0)
    notify = MagicMock()
    monkeypatch.setattr("src.notifier.notify_tank_target_divergence", notify)

    sm._check_tank_target_divergence(_FakeDev(tank_target=30.0), now, trigger="hb")

    notify.assert_called_once()
    kw = notify.call_args.kwargs
    assert kw["planned_c"] == 37.0
    assert kw["live_c"] == 30.0
    assert kw["action_type"] == "tank_setback"
    # Dated so the message can say how long it has been wrong — and CLAMPED to
    # the covering row's start (6 h ago), not the 8 h the device has held 30.
    # The plan cannot have been contradicted before the row existed.
    assert kw["hours"] == pytest.approx(6.0, abs=0.2)


def test_divergence_silent_within_tolerance(monkeypatch) -> None:
    now = datetime(2026, 7, 28, 6, 55, tzinfo=UTC)
    _seed_setback_row(now, tank_temp=37)
    _seed_telemetry(now, target=37.0, hours=8.0)
    notify = MagicMock()
    monkeypatch.setattr("src.notifier.notify_tank_target_divergence", notify)

    sm._check_tank_target_divergence(_FakeDev(tank_target=37.0), now, trigger="hb")

    notify.assert_not_called()


def test_divergence_needs_telemetry_confirmation(monkeypatch) -> None:
    """A single snapshot mid-PATCH must not page — the device's own history
    has to agree that it really holds the divergent target."""
    now = datetime(2026, 7, 28, 6, 55, tzinfo=UTC)
    _seed_setback_row(now, tank_temp=37)
    _seed_telemetry(now, target=37.0, hours=8.0)  # device history says 37
    notify = MagicMock()
    monkeypatch.setattr("src.notifier.notify_tank_target_divergence", notify)

    # ...but this one snapshot read 30.
    sm._check_tank_target_divergence(_FakeDev(tank_target=30.0), now, trigger="hb")

    notify.assert_not_called()


def test_divergence_dedupes_then_repages_on_a_new_gap(monkeypatch) -> None:
    now = datetime(2026, 7, 28, 6, 55, tzinfo=UTC)
    _seed_setback_row(now, tank_temp=37)
    _seed_telemetry(now, target=30.0, hours=8.0, tank=31.0)
    notify = MagicMock()
    monkeypatch.setattr("src.notifier.notify_tank_target_divergence", notify)

    dev = _FakeDev(tank_target=30.0)
    sm._check_tank_target_divergence(dev, now, trigger="hb")
    sm._check_tank_target_divergence(dev, now, trigger="hb")
    assert notify.call_count == 1, "sustained divergence must page once"

    # The user nudges it again — a DIFFERENT gap is news. Seed the new target
    # over the following half hour and re-check from there, so the device's own
    # history genuinely confirms 32 (two samples, as the detector requires).
    later = now + timedelta(minutes=30)
    _seed_one(later - timedelta(minutes=20), 32.0, tank=31.0)
    _seed_one(later - timedelta(minutes=10), 32.0, tank=31.0)
    sm._check_tank_target_divergence(_FakeDev(tank_target=32.0), later, trigger="hb")
    assert notify.call_count == 2


def test_divergence_clears_when_the_setpoint_converges(monkeypatch) -> None:
    now = datetime(2026, 7, 28, 6, 55, tzinfo=UTC)
    _seed_setback_row(now, tank_temp=37)
    _seed_telemetry(now, target=30.0, hours=8.0, tank=31.0)
    notify = MagicMock()
    monkeypatch.setattr("src.notifier.notify_tank_target_divergence", notify)

    sm._check_tank_target_divergence(_FakeDev(tank_target=30.0), now, trigger="hb")
    assert sm._TANK_TARGET_DIVERGENCE_NOTIFIED is not None

    sm._check_tank_target_divergence(_FakeDev(tank_target=37.0), now, trigger="hb")
    assert sm._TANK_TARGET_DIVERGENCE_NOTIFIED is None


def test_divergence_silent_when_the_row_is_user_overridden(monkeypatch) -> None:
    """The reconciler's own override machinery owns that case and pages there;
    this check must not double-page."""
    now = datetime(2026, 7, 28, 6, 55, tzinfo=UTC)
    start = now - timedelta(hours=6)
    db.upsert_action(
        plan_date=start.astimezone(UTC).date().isoformat(),
        start_time=_iso(start),
        end_time=_iso(now + timedelta(hours=12)),
        device="daikin",
        action_type="tank_setback",
        params={"tank_power": True, "tank_temp": 37, "tank_powerful": False},
        status="completed",
    )
    rows = db.get_actions_for_plan_date(start.astimezone(UTC).date().isoformat(), device="daikin")
    db.mark_action_user_overridden(int(rows[0]["id"]))
    _seed_telemetry(now, target=30.0, hours=8.0, tank=31.0)
    notify = MagicMock()
    monkeypatch.setattr("src.notifier.notify_tank_target_divergence", notify)

    sm._check_tank_target_divergence(_FakeDev(tank_target=30.0), now, trigger="hb")

    notify.assert_not_called()


def test_divergence_silent_in_passive_mode(monkeypatch) -> None:
    monkeypatch.setattr(config, "DAIKIN_CONTROL_MODE", "passive", raising=False)
    now = datetime(2026, 7, 28, 6, 55, tzinfo=UTC)
    _seed_setback_row(now, tank_temp=37)
    _seed_telemetry(now, target=30.0, hours=8.0, tank=31.0)
    notify = MagicMock()
    monkeypatch.setattr("src.notifier.notify_tank_target_divergence", notify)

    sm._check_tank_target_divergence(_FakeDev(tank_target=30.0), now, trigger="hb")

    notify.assert_not_called()


def test_divergence_silent_when_the_plan_wants_the_tank_off(monkeypatch) -> None:
    now = datetime(2026, 7, 28, 6, 55, tzinfo=UTC)
    start = now - timedelta(hours=2)
    db.upsert_action(
        plan_date=start.astimezone(UTC).date().isoformat(),
        start_time=_iso(start),
        end_time=_iso(now + timedelta(hours=2)),
        device="daikin",
        action_type="tank_setback",
        params={"tank_power": False, "tank_temp": 37, "tank_powerful": False},
        status="completed",
    )
    _seed_telemetry(now, target=30.0, hours=4.0, tank=31.0)
    notify = MagicMock()
    monkeypatch.setattr("src.notifier.notify_tank_target_divergence", notify)

    sm._check_tank_target_divergence(_FakeDev(tank_target=30.0), now, trigger="hb")

    notify.assert_not_called()


def test_divergence_silent_while_the_row_is_still_pending(monkeypatch) -> None:
    """HEM must not page the household about its OWN decision.

    Two live paths leave a covering row `pending`: the early-setback detector
    (which runs immediately before this check and defers its PATCH to the next
    tick) and the legionella stand-off (which deliberately leaves tank rows
    pending so they resume when the window closes). In both, the device is
    still legitimately holding the previous state.
    """
    now = datetime(2026, 7, 27, 20, 40, tzinfo=UTC)
    start = now - timedelta(minutes=1)
    db.upsert_action(
        plan_date=start.astimezone(UTC).date().isoformat(),
        start_time=_iso(start),
        end_time=_iso(now + timedelta(hours=16)),
        device="daikin",
        action_type="tank_setback",
        params={"tank_power": True, "tank_temp": 37, "tank_powerful": False},
        status="pending",
    )
    _seed_telemetry(now, target=47.0, hours=6.0, tank=44.0)
    notify = MagicMock()
    monkeypatch.setattr("src.notifier.notify_tank_target_divergence", notify)

    # Device still on the 47 warmup hold — the setback has not been written yet.
    sm._check_tank_target_divergence(_FakeDev(tank_target=47.0), now, trigger="hb")

    notify.assert_not_called()


def test_divergence_honours_a_latched_hp_target_lift(monkeypatch) -> None:
    """#737 lifts the commanded setpoint to the 50 °C cliff while the stored row
    keeps its goal — they disagree BY DESIGN. The #737 review named a spurious
    47 -> 50 alert as the thing to avoid; the latch is the expected setpoint."""
    now = datetime(2026, 7, 28, 13, 30, tzinfo=UTC)
    start = now - timedelta(minutes=30)
    db.upsert_action(
        plan_date=start.astimezone(UTC).date().isoformat(),
        start_time=_iso(start),
        end_time=_iso(now + timedelta(hours=2)),
        device="daikin",
        action_type="tank_warmup",
        params={"tank_power": True, "tank_temp": 47, "tank_powerful": False},
        status="completed",
    )
    rows = db.get_actions_for_plan_date(start.astimezone(UTC).date().isoformat(), device="daikin")
    aid = int(rows[0]["id"])
    db.log_action(
        device="daikin",
        action="warmup_deadband_force",
        params={"row_id": aid, "mechanism": "hp_target_lift", "lift_target_c": 50},
        result="success",
        trigger="hb",
    )
    _seed_telemetry(now, target=50.0, hours=0.5, tank=46.0)
    notify = MagicMock()
    monkeypatch.setattr("src.notifier.notify_tank_target_divergence", notify)

    sm._check_tank_target_divergence(_FakeDev(tank_target=50.0), now, trigger="hb")

    notify.assert_not_called()


def test_divergence_costs_no_daikin_calls(monkeypatch) -> None:
    """The household's hard constraint: alerting must not spend Onecta quota."""
    now = datetime(2026, 7, 28, 6, 55, tzinfo=UTC)
    _seed_setback_row(now, tank_temp=37)
    _seed_telemetry(now, target=30.0, hours=8.0, tank=31.0)
    monkeypatch.setattr("src.notifier.notify_tank_target_divergence", MagicMock())

    dev = MagicMock()
    dev.tank_target = 30.0
    sm._check_tank_target_divergence(dev, now, trigger="hb")

    # Only attribute reads on the cached snapshot — no method was invoked on it.
    assert dev.method_calls == [], f"unexpected device calls: {dev.method_calls}"


# ---------------------------------------------------------------------------
# Cold-morning warning
# ---------------------------------------------------------------------------


def test_cold_morning_fires_below_the_upcoming_window_floor(monkeypatch) -> None:
    """06:55 BST, tank at 31 — the morning window (07:00) floor is not met."""
    now = datetime(2026, 7, 28, 5, 55, tzinfo=UTC)  # 06:55 local (BST)
    notify = MagicMock()
    monkeypatch.setattr("src.notifier.notify_morning_tank_cold", notify)

    warmup_start = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
    actions = [{
        "id": 9,
        "action_type": "tank_warmup",
        "start_time": _iso(warmup_start),
        "end_time": _iso(warmup_start + timedelta(hours=3)),
        "status": "pending",
        "params": {"tank_power": True, "tank_temp": 47},
    }]
    sm._check_morning_tank_cold(
        actions, _FakeDev(tank_temperature=31.0, tank_target=30.0), now, trigger="hb"
    )

    notify.assert_called_once()
    kw = notify.call_args.kwargs
    assert kw["tank_c"] == 31.0
    assert kw["floor_c"] > 31.0
    # The message must say when the plan would fix it on its own — 13:00 BST.
    assert kw["next_warmup_local"] == "13:00"


def test_cold_morning_silent_when_warm_enough(monkeypatch) -> None:
    now = datetime(2026, 7, 28, 5, 55, tzinfo=UTC)
    notify = MagicMock()
    monkeypatch.setattr("src.notifier.notify_morning_tank_cold", notify)

    sm._check_morning_tank_cold([], _FakeDev(tank_temperature=54.0), now, trigger="hb")

    notify.assert_not_called()


def test_cold_morning_fires_once_per_day(monkeypatch) -> None:
    now = datetime(2026, 7, 28, 5, 55, tzinfo=UTC)
    notify = MagicMock()
    monkeypatch.setattr("src.notifier.notify_morning_tank_cold", notify)
    dev = _FakeDev(tank_temperature=31.0)

    sm._check_morning_tank_cold([], dev, now, trigger="hb")
    sm._check_morning_tank_cold([], dev, now + timedelta(minutes=10), trigger="hb")

    assert notify.call_count == 1


def test_cold_morning_silent_outside_the_check_window(monkeypatch) -> None:
    notify = MagicMock()
    monkeypatch.setattr("src.notifier.notify_morning_tank_cold", notify)

    # 15:00 UTC — well outside [05, 07).
    sm._check_morning_tank_cold(
        [], _FakeDev(tank_temperature=31.0), datetime(2026, 7, 28, 15, 0, tzinfo=UTC), trigger="hb"
    )

    notify.assert_not_called()


def test_cold_morning_falls_back_to_telemetry_when_snapshot_lacks_the_tank(monkeypatch) -> None:
    now = datetime(2026, 7, 28, 5, 55, tzinfo=UTC)
    _seed_telemetry(now, target=30.0, hours=2.0, tank=31.0)
    notify = MagicMock()
    monkeypatch.setattr("src.notifier.notify_morning_tank_cold", notify)

    sm._check_morning_tank_cold([], _FakeDev(tank_temperature=None), now, trigger="hb")

    notify.assert_called_once()
    assert notify.call_args.kwargs["tank_c"] == 31.0


def test_cold_morning_respects_a_disabled_window(monkeypatch) -> None:
    """`start_hour == end_hour` is how a comfort window is switched OFF (#748).
    Enforcing its floor anyway would be the one way this alert could disagree
    with the constraint it claims to mirror."""
    from src import runtime_settings as rts

    rts.set_setting("DHW_MORNING_RESERVE_START_HOUR", 7.0)
    rts.set_setting("DHW_MORNING_RESERVE_END_HOUR", 7.0)
    now = datetime(2026, 7, 28, 5, 55, tzinfo=UTC)  # 06:55 local
    notify = MagicMock()
    monkeypatch.setattr("src.notifier.notify_morning_tank_cold", notify)

    sm._check_morning_tank_cold([], _FakeDev(tank_temperature=31.0), now, trigger="hb")

    notify.assert_not_called()


def test_cold_morning_ignores_stale_telemetry(monkeypatch) -> None:
    """After an Onecta quota outage the newest live row can be days old; paging
    off it would report a temperature that is no longer true."""
    now = datetime(2026, 7, 28, 5, 55, tzinfo=UTC)
    _seed_one(now - timedelta(hours=30), 30.0, tank=31.0)
    notify = MagicMock()
    monkeypatch.setattr("src.notifier.notify_morning_tank_cold", notify)

    sm._check_morning_tank_cold([], _FakeDev(tank_temperature=None), now, trigger="hb")

    notify.assert_not_called()


def test_cold_morning_still_dedupes_when_delivery_fails(monkeypatch) -> None:
    """Notify runs BEFORE the ack so a Telegram outage doesn't silently burn
    the day's only warning — but a raising notifier must still not re-page."""
    now = datetime(2026, 7, 28, 5, 55, tzinfo=UTC)
    notify = MagicMock(side_effect=RuntimeError("telegram down"))
    monkeypatch.setattr("src.notifier.notify_morning_tank_cold", notify)
    dev = _FakeDev(tank_temperature=31.0)

    sm._check_morning_tank_cold([], dev, now, trigger="hb")
    sm._check_morning_tank_cold([], dev, now + timedelta(minutes=10), trigger="hb")

    assert notify.call_count == 1


def test_cold_morning_costs_no_daikin_calls(monkeypatch) -> None:
    now = datetime(2026, 7, 28, 5, 55, tzinfo=UTC)
    monkeypatch.setattr("src.notifier.notify_morning_tank_cold", MagicMock())

    dev = MagicMock()
    dev.tank_temperature = 31.0
    dev.tank_target = 30.0
    sm._check_morning_tank_cold([], dev, now, trigger="hb")

    assert dev.method_calls == [], f"unexpected device calls: {dev.method_calls}"
