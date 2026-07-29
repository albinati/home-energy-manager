"""Tests for the #767 overnight morning top-up and the sensor-silence detector.

The design decision under test is WHEN the question is asked. Before the evening
showers, "will the morning be warm enough?" needs a draw model, and the two
available ones disagree by 3x — three declared showers imply a 15.3 °C fall on
this cylinder, while the measured setback-to-morning drop over 28 prod nights
has a median of 5 and a p90 of 10. Guessing high buys COP-1 resistance nine
nights in ten; guessing low is the 2026-07-28 cold shower.

Asked AFTER them there is nothing to guess: the draw is already in the
thermometer, and what remains is a plain coast plus an economic choice.
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from src import db
from src.config import config
from src.dhw.model import TankParams, coast_to
from src.dhw.topup import next_morning_entry_utc, plan_morning_topup, required_start_temp_c

UTC = dt.timezone.utc
TZ = ZoneInfo("Europe/London")
P = TankParams()


def _rates(start: dt.datetime, prices: list[float]) -> list[dict]:
    return [
        {
            "valid_from": (start + dt.timedelta(minutes=30 * i)).isoformat(),
            "valid_to": (start + dt.timedelta(minutes=30 * (i + 1))).isoformat(),
            "value_inc_vat": pr,
        }
        for i, pr in enumerate(prices)
    ]


# Cheapest at index 7 (01:30Z when the window opens at 22:00Z).
_NIGHT_PRICES = [22.0, 19.5, 14.2, 11.8, 9.9, 8.4, 7.1, 6.9, 8.8, 12.4,
                 17.0, 21.5, 24.0, 26.5, 30.0, 33.0]


# ---------------------------------------------------------------------------
# The physics
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("hours", [1.0, 3.5, 9.0, 14.0])
def test_required_start_temp_inverts_the_coast_exactly(hours):
    """The commanded target is `coast_to` solved backwards; if the two ever
    disagree the top-up would either overshoot (waste) or undershoot (cold)."""
    need = required_start_temp_c(37.0, hours, P, max_target_c=50.0)
    assert coast_to(need, hours, P) == pytest.approx(37.0, abs=1e-6)


def test_a_floor_below_ambient_needs_no_heat():
    """The cupboard itself holds the tank at ambient, so a floor under it is
    delivered by doing nothing — the inversion must not demand heat for it."""
    assert required_start_temp_c(P.ambient_c - 5.0, 10.0, P, max_target_c=50.0) <= P.ambient_c


# ---------------------------------------------------------------------------
# The decision
# ---------------------------------------------------------------------------


def test_a_normal_night_buys_nothing():
    """The common case, and the one that decides whether this feature costs
    money. A tank at 44 coasts well past a 37 floor on its own."""
    now = dt.datetime(2026, 7, 27, 22, 0, tzinfo=UTC)
    plan = plan_morning_topup(
        tank_c=44.0, now_utc=now, morning_entry_utc=now + dt.timedelta(hours=8),
        floor_c=37.0, margin_c=1.5, p=P, rates=_rates(now, _NIGHT_PRICES),
        max_target_c=P.t_hp_max_c,
        differential_c=6.0, min_shortfall_c=2.0,
    )
    assert plan is None


def test_the_2026_07_28_night_buys_the_cheapest_half_hour():
    """The real failure: tank at 31 °C at 22:00Z after a heavy draw, with the
    setpoint manually at 30 so the firmware would never reheat. The tank
    genuinely cannot reach 37 by morning — so buy the heat, at the bottom of
    the overnight price curve rather than wherever a deadband happens to land."""
    now = dt.datetime(2026, 7, 27, 22, 0, tzinfo=UTC)
    morning = dt.datetime(2026, 7, 28, 6, 0, tzinfo=UTC)
    plan = plan_morning_topup(
        tank_c=31.0, now_utc=now, morning_entry_utc=morning,
        floor_c=37.0, margin_c=1.5, p=P, rates=_rates(now, _NIGHT_PRICES),
        max_target_c=P.t_hp_max_c,
        differential_c=6.0, min_shortfall_c=2.0,
    )

    assert plan is not None
    assert plan.price_p == min(_NIGHT_PRICES), "must pick the cheapest eligible slot"
    assert plan.at_utc == dt.datetime(2026, 7, 28, 1, 30, tzinfo=UTC)
    assert plan.projected_without_c < 37.0
    assert plan.projected_with_c >= 37.0
    assert not plan.best_effort


def test_the_commanded_target_respects_the_deadband_but_is_not_the_cliff():
    """Two constraints, and the larger wins. The floor's inversion says ~39;
    the firmware will not start below `tank + differential`, which says ~37.5.
    Sizing straight to the 50 C cliff would be the lazy answer and would cost
    real money every night it fired."""
    now = dt.datetime(2026, 7, 27, 22, 0, tzinfo=UTC)
    plan = plan_morning_topup(
        tank_c=31.0, now_utc=now, morning_entry_utc=now + dt.timedelta(hours=8),
        floor_c=37.0, margin_c=1.5, p=P, rates=_rates(now, _NIGHT_PRICES),
        max_target_c=P.t_hp_max_c,
        differential_c=6.0, min_shortfall_c=2.0,
    )
    assert plan is not None
    assert plan.target_c < int(P.t_hp_max_c), "must not just command the cliff"
    # Whatever it commands MUST clear the deadband, or the firmware never starts
    # and the row is a physical no-op.
    assert plan.target_c - 31.0 > 6.0


def test_slots_outside_the_window_are_ineligible():
    """Only half-hours strictly between now and the shower entry can be bought."""
    now = dt.datetime(2026, 7, 27, 22, 0, tzinfo=UTC)
    morning = dt.datetime(2026, 7, 28, 0, 0, tzinfo=UTC)  # only 4 slots eligible
    plan = plan_morning_topup(
        tank_c=31.0, now_utc=now, morning_entry_utc=morning,
        floor_c=37.0, margin_c=1.5, p=P, rates=_rates(now, _NIGHT_PRICES),
        max_target_c=P.t_hp_max_c,
        differential_c=6.0, min_shortfall_c=2.0,
    )
    assert plan is not None
    assert now < plan.at_utc < morning
    assert plan.price_p == min(_NIGHT_PRICES[:4])


def test_an_unreachable_floor_is_flagged_best_effort_not_hidden():
    """A cylinder that cannot deliver the floor from any reachable temperature
    must still get the most the heat pump can give — and must SAY so, rather
    than let the caller imply comfort is guaranteed."""
    now = dt.datetime(2026, 7, 27, 22, 0, tzinfo=UTC)
    plan = plan_morning_topup(
        tank_c=20.0, now_utc=now, morning_entry_utc=now + dt.timedelta(hours=8),
        floor_c=58.0,  # above the 50 C heat-pump cliff even with no coasting
        margin_c=1.5, p=P, rates=_rates(now, _NIGHT_PRICES),
        max_target_c=P.t_hp_max_c,
        differential_c=6.0, min_shortfall_c=2.0,
    )
    assert plan is not None
    assert plan.best_effort is True
    assert plan.target_c == int(P.t_hp_max_c)


def test_no_rates_means_no_purchase():
    now = dt.datetime(2026, 7, 27, 22, 0, tzinfo=UTC)
    assert plan_morning_topup(
        tank_c=31.0, now_utc=now, morning_entry_utc=now + dt.timedelta(hours=8),
        floor_c=37.0, margin_c=1.5, p=P, rates=[], max_target_c=P.t_hp_max_c,
        differential_c=6.0, min_shortfall_c=2.0,
    ) is None


def test_morning_entry_is_local_wall_clock_across_a_dst_change():
    """The household showers at 07:00 local, not at a fixed UTC hour. On the
    October transition the UTC answer shifts by an hour and must."""
    # The 2026 transition is 02:00 on 25 Oct, so the morning AFTER a 24 Oct
    # evening is already GMT — which is exactly the trap a fixed UTC hour falls
    # into. Pick one evening either side of it.
    bst = dt.datetime(2026, 10, 20, 22, 0, tzinfo=TZ)   # morning of 21 Oct: BST
    gmt = dt.datetime(2026, 10, 24, 22, 0, tzinfo=TZ)   # morning of 25 Oct: GMT
    assert next_morning_entry_utc(bst, 7.0).hour == 6, "07:00 BST is 06:00 UTC"
    assert next_morning_entry_utc(gmt, 7.0).hour == 7, "07:00 GMT is 07:00 UTC"


# ---------------------------------------------------------------------------
# The heartbeat hook
# ---------------------------------------------------------------------------


@pytest.fixture()
def _isolated(monkeypatch, tmp_path):
    db_path = str(tmp_path / "t.db")
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setattr(config, "DB_PATH", db_path, raising=False)
    monkeypatch.setattr(config, "DHW_MORNING_TOPUP_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "OPENCLAW_READ_ONLY", False, raising=False)
    monkeypatch.setattr(config, "DAIKIN_CONTROL_MODE", "active", raising=False)
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "normal", raising=False)
    monkeypatch.setattr(config, "BULLETPROOF_TIMEZONE", "Europe/London", raising=False)
    monkeypatch.setattr(config, "OCTOPUS_TARIFF_CODE", "TEST-TARIFF", raising=False)
    db.init_db()
    import src.state_machine as sm
    sm._SENSOR_SILENT_NOTIFIED = set()
    # These are module-level dicts keyed by action_schedule ROW ID. Row ids
    # restart at 1 in every isolated test DB, so a leftover entry from another
    # test collides and arms `detect_user_override` against a row this test just
    # created — which surfaced here as the restore being suppressed as a "user
    # override" of HEM's own top-up. Same hazard class as the documented
    # `Config._overrides` leak: global state keyed by a non-global identifier.
    sm._FIRST_APPLIED_SESSION.clear()
    sm._USER_OVERRIDE_NOTIFIED.clear()
    sm._USER_OVERRIDE_INHERITED_NOTIFIED.clear()
    yield


def _seed_tank(at: dt.datetime, temp: float) -> None:
    """A live tank reading. The hook requires TWO within its freshness window —
    a lone cached float can predate the showers and collapse the premise."""
    db.insert_daikin_telemetry({
        "fetched_at": at.timestamp(), "source": "live", "tank_temp_c": temp,
        "tank_target_c": 37.0, "outdoor_temp_c": 15.0, "indoor_temp_c": None,
        "lwt_actual_c": 27.0, "mode": "heating", "weather_regulation": 1,
    })


def _seed_rates(start: dt.datetime, prices: list[float]) -> None:
    conn = db.get_connection()
    try:
        for i, pr in enumerate(prices):
            vf = (start + dt.timedelta(minutes=30 * i)).isoformat().replace("+00:00", "Z")
            vt = (start + dt.timedelta(minutes=30 * (i + 1))).isoformat().replace("+00:00", "Z")
            conn.execute(
                "INSERT OR REPLACE INTO agile_rates (valid_from, valid_to, value_inc_vat, "
                "tariff_code, fetched_at) VALUES (?,?,?,?,?)",
                (vf, vt, pr, "TEST-TARIFF", vf),
            )
        conn.commit()
    finally:
        conn.close()


def test_hook_writes_a_row_only_when_the_tank_needs_it(_isolated):
    import src.state_machine as sm

    now = dt.datetime(2026, 7, 27, 22, 0, tzinfo=UTC)  # 23:00 BST — inside the window
    _seed_rates(now, _NIGHT_PRICES)

    for i in (20, 10):
        _seed_tank(now - dt.timedelta(minutes=i), 44.0)
    sm._check_morning_topup(SimpleNamespace(tank_temperature=44.0), now, trigger="hb")
    assert db.get_actions_for_plan_date("2026-07-28", device="daikin") == []

    for i in (6, 3):
        _seed_tank(now - dt.timedelta(minutes=i), 31.0)
    sm._check_morning_topup(SimpleNamespace(tank_temperature=31.0), now, trigger="hb")

    # Filed under the row's OWN local date (28 Jul), not the decision date.
    rows = db.get_actions_for_plan_date("2026-07-28", device="daikin")
    kinds = {r["action_type"]: r for r in rows}
    assert set(kinds) == {"tank_warmup", "tank_setback"}, "the top-up must be PAIRED"
    assert kinds["tank_warmup"]["params"]["morning_topup"] is True
    assert kinds["tank_warmup"]["params"]["tank_powerful"] is False, "never COP-1 resistance"
    assert kinds["tank_setback"]["params"]["morning_topup_restore"] is True


def test_hook_re_asserts_the_row_after_a_replan_deletes_it(_isolated):
    """The nightly plan push regenerates the tank schedule and
    `clear_actions_in_range` deletes pending rows — a write-once top-up would
    simply vanish. Re-asserting is idempotent on the natural key."""
    import src.state_machine as sm

    now = dt.datetime(2026, 7, 27, 22, 0, tzinfo=UTC)
    _seed_rates(now, _NIGHT_PRICES)
    dev = SimpleNamespace(tank_temperature=31.0)
    for i in (6, 3):
        _seed_tank(now - dt.timedelta(minutes=i), 31.0)

    sm._check_morning_topup(dev, now, trigger="hb")
    before = db.get_actions_for_plan_date("2026-07-28", device="daikin")
    assert len(before) == 2

    conn = db.get_connection()
    try:
        conn.execute("DELETE FROM action_schedule")
        conn.commit()
    finally:
        conn.close()

    sm._check_morning_topup(dev, now + dt.timedelta(minutes=5), trigger="hb")
    after = db.get_actions_for_plan_date("2026-07-28", device="daikin")
    assert len(after) == 2
    assert [r["start_time"] for r in after] == [r["start_time"] for r in before], \
        "re-assert must reuse the SAME slot, never re-plan onto a later one"


def test_hook_is_inert_outside_the_evening_window(_isolated):
    """Asked before the showers the answer would need a draw model, which is
    the guess this design exists to avoid."""
    import src.state_machine as sm

    midday = dt.datetime(2026, 7, 27, 12, 0, tzinfo=UTC)  # 13:00 BST
    _seed_rates(midday, _NIGHT_PRICES)

    for i in (6, 3):
        _seed_tank(midday - dt.timedelta(minutes=i), 31.0)
    sm._check_morning_topup(SimpleNamespace(tank_temperature=31.0), midday, trigger="hb")

    assert db.get_actions_for_plan_date("2026-07-27", device="daikin") == []
    assert db.get_actions_for_plan_date("2026-07-28", device="daikin") == []


def test_hook_is_inert_in_read_only_and_vacation(_isolated, monkeypatch):
    import src.state_machine as sm

    now = dt.datetime(2026, 7, 27, 22, 0, tzinfo=UTC)
    _seed_rates(now, _NIGHT_PRICES)
    dev = SimpleNamespace(tank_temperature=31.0)
    for i in (6, 3):
        _seed_tank(now - dt.timedelta(minutes=i), 31.0)

    monkeypatch.setattr(config, "OPENCLAW_READ_ONLY", True, raising=False)
    sm._check_morning_topup(dev, now, trigger="hb")
    assert db.get_actions_for_plan_date("2026-07-28", device="daikin") == []

    monkeypatch.setattr(config, "OPENCLAW_READ_ONLY", False, raising=False)
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "vacation", raising=False)
    sm._check_morning_topup(dev, now, trigger="hb")
    assert db.get_actions_for_plan_date("2026-07-28", device="daikin") == []


def test_hook_logs_once_even_though_the_row_is_re_asserted(_isolated):
    import src.state_machine as sm

    now = dt.datetime(2026, 7, 27, 22, 0, tzinfo=UTC)
    _seed_rates(now, _NIGHT_PRICES)
    dev = SimpleNamespace(tank_temperature=31.0)
    for i in (6, 3):
        _seed_tank(now - dt.timedelta(minutes=i), 31.0)

    for i in range(4):
        sm._check_morning_topup(dev, now + dt.timedelta(minutes=5 * i), trigger="hb")

    logs = [r for r in db.get_action_logs(device="daikin", limit=50)
            if r["action"] == "dhw_morning_topup"]
    assert len(logs) == 1


# ---------------------------------------------------------------------------
# Sensor silence
# ---------------------------------------------------------------------------


def _seed_device(key: str, room: str, at: dt.datetime) -> None:
    conn = db.get_connection()
    try:
        iso = at.isoformat().replace("+00:00", "Z")
        conn.execute(
            "INSERT INTO device_reading_log (device_key, dedup_key, room, received_at, "
            "captured_at, source, temp_c, payload_json) VALUES (?,?,?,?,?,?,?,?)",
            (key, f"{key}|{iso}", room, iso, iso, "esphome", 21.0, "{}"),
        )
        conn.commit()
    finally:
        conn.close()


def test_sensor_silence_pages_once_per_episode(_isolated, monkeypatch):
    import src.state_machine as sm

    now = dt.datetime(2026, 7, 29, 8, 0, tzinfo=UTC)
    _seed_device("AA:BB", "cozinha", now - dt.timedelta(hours=8))
    _seed_device("CC:DD", "corredor", now - dt.timedelta(hours=8))
    notify = MagicMock()
    monkeypatch.setattr("src.notifier.notify_sensor_silent", notify)

    sm._check_sensor_silence(now, trigger="hb")
    sm._check_sensor_silence(now + dt.timedelta(minutes=10), trigger="hb")

    assert notify.call_count == 1
    assert {d["room"] for d in notify.call_args.kwargs["devices"]} == {"cozinha", "corredor"}


def test_sensor_silence_quiet_while_readings_flow(_isolated, monkeypatch):
    import src.state_machine as sm

    now = dt.datetime(2026, 7, 29, 8, 0, tzinfo=UTC)
    _seed_device("AA:BB", "cozinha", now - dt.timedelta(minutes=8))
    notify = MagicMock()
    monkeypatch.setattr("src.notifier.notify_sensor_silent", notify)

    sm._check_sensor_silence(now, trigger="hb")
    notify.assert_not_called()


def test_a_failed_page_does_not_silence_the_episode(_isolated, monkeypatch):
    """Suppression is set only after the dispatch succeeds, so a Telegram
    outage during the outage does not swallow the whole thing."""
    import src.state_machine as sm

    now = dt.datetime(2026, 7, 29, 8, 0, tzinfo=UTC)
    _seed_device("AA:BB", "cozinha", now - dt.timedelta(hours=8))
    boom = MagicMock(side_effect=RuntimeError("telegram down"))
    monkeypatch.setattr("src.notifier.notify_sensor_silent", boom)

    sm._check_sensor_silence(now, trigger="hb")
    assert boom.call_count == 1

    ok = MagicMock()
    monkeypatch.setattr("src.notifier.notify_sensor_silent", ok)
    sm._check_sensor_silence(now + dt.timedelta(minutes=5), trigger="hb")
    assert ok.call_count == 1, "a failed page must be retried, not treated as delivered"


def test_a_retired_device_does_not_page_forever(_isolated, monkeypatch):
    import src.state_machine as sm

    now = dt.datetime(2026, 7, 29, 8, 0, tzinfo=UTC)
    _seed_device("OLD:01", "quarto", now - dt.timedelta(days=30))
    notify = MagicMock()
    monkeypatch.setattr("src.notifier.notify_sensor_silent", notify)

    sm._check_sensor_silence(now, trigger="hb")
    notify.assert_not_called()


def test_sensor_silence_judges_server_time_not_the_device_clock(_isolated, monkeypatch):
    """A unit whose clock drifted could claim a fresh `captured_at` while
    sending nothing — exactly what this exists to catch (#700)."""
    import src.state_machine as sm

    now = dt.datetime(2026, 7, 29, 8, 0, tzinfo=UTC)
    conn = db.get_connection()
    try:
        srv = (now - dt.timedelta(hours=8)).isoformat().replace("+00:00", "Z")
        conn.execute(
            "INSERT INTO device_reading_log (device_key, dedup_key, room, received_at, "
            "captured_at, source, temp_c, payload_json) VALUES (?,?,?,?,?,?,?,?)",
            ("AA:BB", f"AA:BB|{srv}", "cozinha", srv,
             (now + dt.timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
             "esphome", 21.0, "{}"),
        )
        conn.commit()
    finally:
        conn.close()
    notify = MagicMock()
    monkeypatch.setattr("src.notifier.notify_sensor_silent", notify)

    sm._check_sensor_silence(now, trigger="hb")
    notify.assert_called_once()


# ---------------------------------------------------------------------------
# The integration the previous attempt never had
# ---------------------------------------------------------------------------


def test_the_row_fires_at_its_own_target_and_then_restores(_isolated, monkeypatch):
    """The test whose absence let #773 ship broken: it asserted what was
    PLANNED and never what was PATCHED.

    Two things have to hold on the wire. The top-up must fire at the target it
    chose — not get rewritten to the 50 °C cliff by the deadband guard, which is
    what happened when the command sat inside the reheat differential. And the
    tank must come back down afterwards, or it sits high until the 13:00 warmup
    and trips the divergence alert every night.
    """
    import src.state_machine as sm

    now = dt.datetime(2026, 7, 27, 22, 0, tzinfo=UTC)
    _seed_rates(now, _NIGHT_PRICES)
    for i in (6, 3):
        _seed_tank(now - dt.timedelta(minutes=i), 31.0)
    sm._check_morning_topup(SimpleNamespace(tank_temperature=31.0), now, trigger="hb")

    rows = db.get_actions_for_plan_date("2026-07-28", device="daikin")
    warmup = next(r for r in rows if r["action_type"] == "tank_warmup")
    restore = next(r for r in rows if r["action_type"] == "tank_setback")
    chosen = int(warmup["params"]["tank_temp"])

    applied: list[dict] = []
    monkeypatch.setattr(
        "src.state_machine.apply_scheduled_daikin_params",
        lambda d, c, p, trigger: applied.append(dict(p)) or True,
    )
    dev = SimpleNamespace(
        tank_temperature=31.0, tank_target=37.0, tank_on=True,
        tank_powerful=False, is_on=True, lwt_offset=None, id="d", name="Altherma",
    )

    fire = dt.datetime.fromisoformat(warmup["start_time"].replace("Z", "+00:00"))
    sm._reconcile_daikin_actions(rows, MagicMock(), dev, fire, trigger="test")

    assert applied, "the top-up row never fired"
    assert applied[-1]["tank_temp"] == chosen, (
        f"fired at {applied[-1]['tank_temp']}, not the chosen {chosen} — "
        "the deadband guard rewrote it"
    )
    assert applied[-1]["tank_temp"] < int(P.t_hp_max_c), "escalated to the cliff"
    assert applied[-1].get("tank_powerful") is not True, "COP-1 resistance"

    # ...and the paired restore brings it back to the setback.
    applied.clear()
    dev.tank_target = float(chosen)
    dev.tank_temperature = float(chosen)
    after = dt.datetime.fromisoformat(restore["start_time"].replace("Z", "+00:00"))
    rows2 = db.get_actions_for_plan_date("2026-07-28", device="daikin")
    sm._reconcile_daikin_actions(rows2, MagicMock(), dev, after, trigger="test")

    assert applied, "nothing restored the setback"
    assert applied[-1]["tank_temp"] == int(config.DHW_TEMP_SETBACK_C)
