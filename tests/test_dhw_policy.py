"""Tests for the PR K1 dhw_policy module — the fixed daily tank schedule
that replaces LP-driven tank arbitrage.

The schedule shape per user 2026-05-23:
    Normal mode:  warmup 13:00→22:00 at NORMAL=45, setback 22:00→13:00 at 37
    Guests mode:  single 24h warmup at NORMAL=45 (no setback; morning showers)
    Vacation:     no actions (Daikin firmware owns)
    Negative-price slots: overlay tank_negative_boost at 60 °C
"""
from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src import db as _db
from src import dhw_policy
from src.config import config

TZ_LOCAL = ZoneInfo("Europe/London")


class _FrozenDatetime(datetime):
    """Pin ``dhw_policy``'s clock so the fixed 2026-06-01 anchor date is never
    treated as "in the past" by the past-date guard. Kills the date-flake that
    otherwise breaks the whole module once the wall clock passes 2026-06-01."""

    @classmethod
    def now(cls, tz=None):  # noqa: D401
        base = datetime(2026, 6, 1, 10, 0, tzinfo=UTC)
        return base.astimezone(tz) if tz is not None else base.replace(tzinfo=None)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("DB_PATH", db_path)
    monkeypatch.setattr(config, "DB_PATH", db_path, raising=False)
    monkeypatch.setattr(dhw_policy, "datetime", _FrozenDatetime, raising=False)
    _db.init_db()
    monkeypatch.setattr(config, "DHW_FIXED_SCHEDULE_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "DHW_WARMUP_START_HOUR_LOCAL", 13, raising=False)
    monkeypatch.setattr(config, "DHW_SETBACK_START_HOUR_LOCAL", 22, raising=False)
    monkeypatch.setattr(config, "DHW_TEMP_NORMAL_C", 45.0, raising=False)
    monkeypatch.setattr(config, "DHW_TEMP_SETBACK_C", 37.0, raising=False)
    monkeypatch.setattr(config, "DHW_NEGATIVE_PRICE_BOOST_C", 60.0, raising=False)
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "normal", raising=False)
    monkeypatch.setattr(config, "BULLETPROOF_TIMEZONE", "Europe/London", raising=False)
    yield


# ---------------------------------------------------------------------------
# Generate (pure function — no DB)
# ---------------------------------------------------------------------------


def test_normal_mode_emits_two_rows():
    """Normal mode: warmup + setback rows."""
    rows = dhw_policy.generate_daily_tank_schedule(date(2026, 6, 1), mode="normal")
    assert len(rows) == 2
    types = [r["action_type"] for r in rows]
    assert types == ["tank_warmup", "tank_setback"]


def test_normal_mode_warmup_timing():
    """tank_warmup runs 13:00→22:00 local on the target day."""
    rows = dhw_policy.generate_daily_tank_schedule(date(2026, 6, 1), mode="normal")
    warmup = next(r for r in rows if r["action_type"] == "tank_warmup")
    start = datetime.fromisoformat(warmup["start_time"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(warmup["end_time"].replace("Z", "+00:00"))
    # 13:00 BST = 12:00 UTC (BST = UTC+1 in June)
    assert start.astimezone(TZ_LOCAL).hour == 13
    assert end.astimezone(TZ_LOCAL).hour == 22
    assert warmup["params"]["tank_temp"] == 45
    assert warmup["params"]["tank_power"] is True


def test_normal_mode_setback_timing():
    """tank_setback runs 22:00→next day 13:00 local."""
    rows = dhw_policy.generate_daily_tank_schedule(date(2026, 6, 1), mode="normal")
    setback = next(r for r in rows if r["action_type"] == "tank_setback")
    start = datetime.fromisoformat(setback["start_time"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(setback["end_time"].replace("Z", "+00:00"))
    assert start.astimezone(TZ_LOCAL).hour == 22
    assert start.astimezone(TZ_LOCAL).day == 1
    assert end.astimezone(TZ_LOCAL).hour == 13
    assert end.astimezone(TZ_LOCAL).day == 2  # next day
    assert setback["params"]["tank_temp"] == 37


def test_guests_mode_emits_single_24h_row():
    """Guests preset: tank stays at NORMAL the whole day (morning showers)."""
    rows = dhw_policy.generate_daily_tank_schedule(date(2026, 6, 1), mode="guests")
    assert len(rows) == 1
    r = rows[0]
    assert r["action_type"] == "tank_warmup"
    assert r["params"]["tank_temp"] == 45
    start = datetime.fromisoformat(r["start_time"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(r["end_time"].replace("Z", "+00:00"))
    duration = (end - start).total_seconds() / 3600
    assert abs(duration - 24) < 0.1


def test_vacation_mode_emits_nothing():
    """Vacation preset: tank firmware-owned, no HEM actions."""
    rows = dhw_policy.generate_daily_tank_schedule(date(2026, 6, 1), mode="vacation")
    assert rows == []


def test_unknown_mode_defaults_to_normal_behavior():
    """An unrecognized mode string falls through to normal (defensive)."""
    rows = dhw_policy.generate_daily_tank_schedule(date(2026, 6, 1), mode="bogus")
    assert len(rows) == 2
    assert {r["action_type"] for r in rows} == {"tank_warmup", "tank_setback"}


def test_mode_pulled_from_config_when_none(monkeypatch):
    """When mode=None, falls back to OPTIMIZATION_PRESET."""
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "guests", raising=False)
    rows = dhw_policy.generate_daily_tank_schedule(date(2026, 6, 1))
    assert len(rows) == 1
    assert rows[0]["action_type"] == "tank_warmup"


# ---------------------------------------------------------------------------
# Negative-price overlay
# ---------------------------------------------------------------------------


def test_negative_price_single_slot_overlay():
    """One negative-price 30-min slot inside the schedule horizon adds a
    tank_negative_boost row at 60 °C."""
    # Negative slot at 14:00 UTC (15:00 BST = inside warmup window)
    neg_slot_utc = datetime(2026, 6, 1, 14, 0, tzinfo=UTC).isoformat().replace("+00:00", "Z")
    outgoing = [
        {"valid_from": neg_slot_utc, "value_inc_vat": -5.0, "tariff_code": "AGILE-OUTGOING"},
    ]
    rows = dhw_policy.generate_daily_tank_schedule(
        date(2026, 6, 1), agile_rates=outgoing, mode="normal",
    )
    boost = [r for r in rows if r["action_type"] == "tank_negative_boost"]
    assert len(boost) == 1
    assert boost[0]["params"]["tank_temp"] == 60
    assert boost[0]["params"]["tank_powerful"] is True  # grid pays — load max


def test_consecutive_negative_slots_merged_into_window():
    """3 consecutive negative slots → one merged boost window (90 min)."""
    outgoing = [
        {"valid_from": "2026-06-01T14:00:00Z", "value_inc_vat": -5.0},
        {"valid_from": "2026-06-01T14:30:00Z", "value_inc_vat": -3.5},
        {"valid_from": "2026-06-01T15:00:00Z", "value_inc_vat": -1.2},
    ]
    rows = dhw_policy.generate_daily_tank_schedule(
        date(2026, 6, 1), agile_rates=outgoing, mode="normal",
    )
    boost = [r for r in rows if r["action_type"] == "tank_negative_boost"]
    assert len(boost) == 1
    start = datetime.fromisoformat(boost[0]["start_time"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(boost[0]["end_time"].replace("Z", "+00:00"))
    duration_min = (end - start).total_seconds() / 60
    assert duration_min == 90  # 3 slots × 30 min


def test_non_contiguous_negative_slots_emit_separate_windows():
    """A gap between negative slots → two separate boost windows."""
    outgoing = [
        {"valid_from": "2026-06-01T14:00:00Z", "value_inc_vat": -5.0},
        # Gap at 14:30 (positive price)
        {"valid_from": "2026-06-01T15:30:00Z", "value_inc_vat": -2.0},
    ]
    rows = dhw_policy.generate_daily_tank_schedule(
        date(2026, 6, 1), agile_rates=outgoing, mode="normal",
    )
    boost = [r for r in rows if r["action_type"] == "tank_negative_boost"]
    assert len(boost) == 2


def test_positive_outgoing_rates_do_not_trigger_boost():
    """All-positive outgoing rates → no boost rows, just warmup + setback."""
    outgoing = [
        {"valid_from": "2026-06-01T14:00:00Z", "value_inc_vat": 15.0},
        {"valid_from": "2026-06-01T15:00:00Z", "value_inc_vat": 20.0},
    ]
    rows = dhw_policy.generate_daily_tank_schedule(
        date(2026, 6, 1), agile_rates=outgoing, mode="normal",
    )
    assert all(r["action_type"] != "tank_negative_boost" for r in rows)


def test_negative_slot_outside_horizon_ignored():
    """Negative slot before today's 13:00 warmup start is outside horizon
    and gets skipped."""
    # Negative at 10:00 UTC (11:00 BST = before warmup start)
    outgoing = [
        {"valid_from": "2026-06-01T10:00:00Z", "value_inc_vat": -5.0},
    ]
    rows = dhw_policy.generate_daily_tank_schedule(
        date(2026, 6, 1), agile_rates=outgoing, mode="normal",
    )
    boost = [r for r in rows if r["action_type"] == "tank_negative_boost"]
    assert boost == []


def test_outgoing_rates_none_no_crash():
    """agile_rates=None just yields warmup+setback, no boost; doesn't crash."""
    rows = dhw_policy.generate_daily_tank_schedule(
        date(2026, 6, 1), agile_rates=None, mode="normal",
    )
    assert len(rows) == 2
    assert all(r["action_type"] in ("tank_warmup", "tank_setback") for r in rows)


def test_outgoing_rate_zero_does_not_trigger_boost():
    """Rate=0 is not strictly negative; no boost."""
    outgoing = [{"valid_from": "2026-06-01T14:00:00Z", "value_inc_vat": 0.0}]
    rows = dhw_policy.generate_daily_tank_schedule(
        date(2026, 6, 1), agile_rates=outgoing, mode="normal",
    )
    boost = [r for r in rows if r["action_type"] == "tank_negative_boost"]
    assert boost == []


def test_malformed_outgoing_rate_skipped():
    """Garbled rate entries don't crash; just get skipped."""
    outgoing = [
        {"valid_from": None, "value_inc_vat": -5.0},  # missing time
        {"valid_from": "not-a-date", "value_inc_vat": -3.0},  # bad time
        {"valid_from": "2026-06-01T14:00:00Z", "value_inc_vat": "huh"},  # bad rate
        {"valid_from": "2026-06-01T15:00:00Z", "value_inc_vat": -1.0},  # good
    ]
    rows = dhw_policy.generate_daily_tank_schedule(
        date(2026, 6, 1), agile_rates=outgoing, mode="normal",
    )
    boost = [r for r in rows if r["action_type"] == "tank_negative_boost"]
    # Only the one good negative rate emits a boost
    assert len(boost) == 1


# ---------------------------------------------------------------------------
# Boost-supersedes-warmup override (no positive-price pre-heat before a boost)
# ---------------------------------------------------------------------------


def _warmup(rows):
    return next((r for r in rows if r["action_type"] == "tank_warmup"), None)


def test_boost_near_warmup_start_defers_warmup():
    """A boost opening shortly after the warmup start defers the warmup past it
    — no pre-heating to NORMAL at a positive price right before the free boost.
    Warmup start moves from 13:00 BST (12:00 UTC) to the boost end."""
    # Negative slot at 12:30 UTC (13:30 BST) — 30 min after the 13:00 warmup.
    outgoing = [{"valid_from": "2026-06-01T12:30:00Z", "value_inc_vat": -5.0}]
    rows = dhw_policy.generate_daily_tank_schedule(
        date(2026, 6, 1), agile_rates=outgoing, mode="normal",
    )
    warmup = _warmup(rows)
    assert warmup is not None
    start = datetime.fromisoformat(warmup["start_time"].replace("Z", "+00:00"))
    # Deferred to the boost end (13:00 UTC = 14:00 BST), NOT the original 12:00 UTC.
    assert start == datetime(2026, 6, 1, 13, 0, tzinfo=UTC), start
    # Setback is untouched.
    setback = next(r for r in rows if r["action_type"] == "tank_setback")
    s_start = datetime.fromisoformat(setback["start_time"].replace("Z", "+00:00"))
    assert s_start.astimezone(TZ_LOCAL).hour == 22


def test_boost_far_from_warmup_start_keeps_warmup():
    """A boost later than the lead window leaves the warmup intact — the
    afternoon still needs hot water before that boost arrives."""
    # Boost at 16:00 UTC (17:00 BST), well beyond the 120-min lead.
    outgoing = [{"valid_from": "2026-06-01T16:00:00Z", "value_inc_vat": -5.0}]
    rows = dhw_policy.generate_daily_tank_schedule(
        date(2026, 6, 1), agile_rates=outgoing, mode="normal",
    )
    warmup = _warmup(rows)
    assert warmup is not None
    start = datetime.fromisoformat(warmup["start_time"].replace("Z", "+00:00"))
    assert start.astimezone(TZ_LOCAL).hour == 13  # unchanged 13:00 BST
    assert warmup["params"]["tank_temp"] == 45


def test_interleaved_negative_window_chains_warmup_defer():
    """The real prod case (2026-06-04): negatives at 12:30 / 13:30 / 14:00 UTC
    with a positive slot at 13:00 between them. The warmup must defer past the
    WHOLE early cluster (two boost windows chained) — no warmup at the positive
    12:00 or 13:00 slots."""
    outgoing = [
        {"valid_from": "2026-06-01T12:30:00Z", "value_inc_vat": -0.34},
        {"valid_from": "2026-06-01T13:00:00Z", "value_inc_vat": 0.77},  # positive gap
        {"valid_from": "2026-06-01T13:30:00Z", "value_inc_vat": -1.30},
        {"valid_from": "2026-06-01T14:00:00Z", "value_inc_vat": -0.86},
    ]
    rows = dhw_policy.generate_daily_tank_schedule(
        date(2026, 6, 1), agile_rates=outgoing, mode="normal",
    )
    boost = [r for r in rows if r["action_type"] == "tank_negative_boost"]
    assert len(boost) == 2  # (12:30-13:00) and (13:30-14:30)
    warmup = _warmup(rows)
    assert warmup is not None
    start = datetime.fromisoformat(warmup["start_time"].replace("Z", "+00:00"))
    # Deferred past the last chained boost end: 14:30 UTC (15:30 BST).
    assert start == datetime(2026, 6, 1, 14, 30, tzinfo=UTC), start


def test_boost_chain_can_drop_guest_warmup_start():
    """Guests mode: a near-start boost defers the single 24h warmup too."""
    outgoing = [{"valid_from": "2026-06-01T12:30:00Z", "value_inc_vat": -5.0}]
    rows = dhw_policy.generate_daily_tank_schedule(
        date(2026, 6, 1), agile_rates=outgoing, mode="guests",
    )
    warmup = _warmup(rows)
    assert warmup is not None
    start = datetime.fromisoformat(warmup["start_time"].replace("Z", "+00:00"))
    assert start == datetime(2026, 6, 1, 13, 0, tzinfo=UTC), start


def test_warmup_override_disabled_by_zero_precool():
    """precool_hours=0 disables both the LP pre-cool AND the schedule defer —
    warmup stays at 13:00 even with a boost at the very next slot (rollback
    escape hatch, and the two paths stay consistent: no pre-cool ⇒ no defer)."""
    outgoing = [{"valid_from": "2026-06-01T12:30:00Z", "value_inc_vat": -5.0}]
    import src.dhw_policy as _dp
    import pytest as _pytest
    with _pytest.MonkeyPatch.context() as mp:
        mp.setattr(_dp.config, "LP_PRE_NEGATIVE_PRECOOL_HOURS", 0.0, raising=False)
        rows = _dp.generate_daily_tank_schedule(
            date(2026, 6, 1), agile_rates=outgoing, mode="normal",
        )
    warmup = _warmup(rows)
    start = datetime.fromisoformat(warmup["start_time"].replace("Z", "+00:00"))
    assert start.astimezone(TZ_LOCAL).hour == 13


def test_schedule_defer_matches_lp_forecast_precool():
    """Cross-path consistency: the warmup-start slot the SCHEDULE defers is the
    same slot the LP ENERGY FORECAST pre-cools (zeroes warmup energy for). If
    these ever diverge, the LP budgets DHW import for heating that won't fire
    (or vice-versa) — the architectural drift this guard exists to catch."""
    # Negative window opening one slot after the 13:00 BST (12:00 UTC) warmup.
    outgoing = [{"valid_from": "2026-06-01T12:30:00Z", "value_inc_vat": -5.0}]
    rows = dhw_policy.generate_daily_tank_schedule(
        date(2026, 6, 1), agile_rates=outgoing, mode="normal",
    )
    warmup = _warmup(rows)
    start = datetime.fromisoformat(warmup["start_time"].replace("Z", "+00:00"))
    # SCHEDULE side: warmup deferred past the boost (not firing at 12:00 UTC).
    assert start > datetime(2026, 6, 1, 12, 0, tzinfo=UTC)

    # FORECAST side: same horizon + a price_line negative on the 12:30 slot.
    slots = [datetime(2026, 6, 1, 12, 0, tzinfo=UTC) + i * timedelta(minutes=30)
             for i in range(12)]
    price_line = [5.0] * 12
    price_line[1] = -5.0  # 12:30 UTC negative
    e_dhw, _temps = dhw_policy.forecast_dhw_load_per_slot(
        slots, mode="normal", price_line=price_line,
    )
    # The 12:00 UTC warmup-transition slot must be pre-cooled (no warmup energy
    # budgeted) — consistent with the schedule deferring that warmup.
    assert e_dhw[0] <= 0.05, f"LP still budgets leading warmup energy: {e_dhw[0]:.3f} kWh"


# ---------------------------------------------------------------------------
# DB write path (write_daily_tank_schedule)
# ---------------------------------------------------------------------------


def test_write_daily_tank_schedule_persists_rows():
    """write_daily_tank_schedule upserts rows into action_schedule."""
    n = dhw_policy.write_daily_tank_schedule(
        target_date_local=date(2026, 6, 1),
        agile_rates=None,
        mode="normal",
        clear_existing=False,
    )
    assert n == 2
    import sqlite3
    conn = sqlite3.connect(config.DB_PATH)
    rows = list(conn.execute(
        """SELECT action_type, params FROM action_schedule
           WHERE device = 'daikin' AND date = '2026-06-01'
           ORDER BY start_time"""
    ))
    assert len(rows) == 2
    types = [r[0] for r in rows]
    assert types == ["tank_warmup", "tank_setback"]
    for r in rows:
        params = json.loads(r[1])
        assert params["dhw_policy"] is True  # marker present


def test_write_vacation_writes_zero_rows():
    """write_daily_tank_schedule in vacation mode is a no-op."""
    n = dhw_policy.write_daily_tank_schedule(
        target_date_local=date(2026, 6, 1),
        mode="vacation",
        clear_existing=False,
    )
    assert n == 0


def test_write_with_clear_existing_clears_then_writes():
    """clear_existing=True clears the horizon then writes the new rows."""
    # First seed a stale solar_preheat row
    _db.upsert_action(
        plan_date="2026-06-01",
        device="daikin", action_type="solar_preheat",
        start_time="2026-06-01T13:00:00Z",
        end_time="2026-06-01T16:00:00Z",
        params={"tank_temp": 60},
        status="pending",
    )
    import sqlite3
    conn = sqlite3.connect(config.DB_PATH)
    pre = list(conn.execute("SELECT COUNT(*) FROM action_schedule WHERE action_type='solar_preheat'"))[0][0]
    assert pre == 1

    n = dhw_policy.write_daily_tank_schedule(
        target_date_local=date(2026, 6, 1),
        mode="normal",
        clear_existing=True,
    )
    assert n == 2
    # Stale row cleared, new rows present
    rows = list(conn.execute(
        "SELECT action_type FROM action_schedule WHERE device='daikin' ORDER BY start_time"
    ))
    types = [r[0] for r in rows]
    assert "solar_preheat" not in types
    assert "tank_warmup" in types
    assert "tank_setback" in types


# ---------------------------------------------------------------------------
# Runtime hour overrides
# ---------------------------------------------------------------------------


def test_warmup_hour_runtime_override(monkeypatch):
    """User can change warmup start via env var/runtime setting."""
    monkeypatch.setattr(config, "DHW_WARMUP_START_HOUR_LOCAL", 15, raising=False)
    rows = dhw_policy.generate_daily_tank_schedule(date(2026, 6, 1), mode="normal")
    warmup = next(r for r in rows if r["action_type"] == "tank_warmup")
    start = datetime.fromisoformat(warmup["start_time"].replace("Z", "+00:00"))
    assert start.astimezone(TZ_LOCAL).hour == 15


def test_setback_hour_runtime_override(monkeypatch):
    """User can shift setback start time."""
    monkeypatch.setattr(config, "DHW_SETBACK_START_HOUR_LOCAL", 20, raising=False)
    rows = dhw_policy.generate_daily_tank_schedule(date(2026, 6, 1), mode="normal")
    warmup = next(r for r in rows if r["action_type"] == "tank_warmup")
    end = datetime.fromisoformat(warmup["end_time"].replace("Z", "+00:00"))
    assert end.astimezone(TZ_LOCAL).hour == 20


def test_setback_temp_override(monkeypatch):
    """SETBACK_C is runtime-tunable."""
    monkeypatch.setattr(config, "DHW_TEMP_SETBACK_C", 40.0, raising=False)
    rows = dhw_policy.generate_daily_tank_schedule(date(2026, 6, 1), mode="normal")
    setback = next(r for r in rows if r["action_type"] == "tank_setback")
    assert setback["params"]["tank_temp"] == 40


def test_negative_boost_temp_clamped_to_device_max(monkeypatch):
    """The boost target is clamped to DHW_TEMP_MAX_C (the device setpoint
    ceiling). Requesting 65 must NOT emit a 65 °C setpoint — the heat pump
    rejects anything above its max ("Max tank temperature is 60°C",
    client.py:306), which made the whole boost write FAIL in prod (no heating
    at all). This guard ensures we only ever write a setpoint the device
    accepts."""
    monkeypatch.setattr(config, "DHW_NEGATIVE_PRICE_BOOST_C", 65.0, raising=False)
    monkeypatch.setattr(config, "DHW_TEMP_MAX_C", 60.0, raising=False)
    outgoing = [{"valid_from": "2026-06-01T14:00:00Z", "value_inc_vat": -5.0}]
    rows = dhw_policy.generate_daily_tank_schedule(
        date(2026, 6, 1), agile_rates=outgoing, mode="normal",
    )
    boost = next(r for r in rows if r["action_type"] == "tank_negative_boost")
    assert boost["params"]["tank_temp"] == 60  # clamped to device max, not 65


def test_negative_boost_temp_tunable_below_max(monkeypatch):
    """A valid sub-max boost target passes through unchanged."""
    monkeypatch.setattr(config, "DHW_NEGATIVE_PRICE_BOOST_C", 55.0, raising=False)
    monkeypatch.setattr(config, "DHW_TEMP_MAX_C", 60.0, raising=False)
    outgoing = [{"valid_from": "2026-06-01T14:00:00Z", "value_inc_vat": -5.0}]
    rows = dhw_policy.generate_daily_tank_schedule(
        date(2026, 6, 1), agile_rates=outgoing, mode="normal",
    )
    boost = next(r for r in rows if r["action_type"] == "tank_negative_boost")
    assert boost["params"]["tank_temp"] == 55


def test_normal_temp_override(monkeypatch):
    """NORMAL_C runtime change reflects in warmup row."""
    monkeypatch.setattr(config, "DHW_TEMP_NORMAL_C", 46.0, raising=False)
    rows = dhw_policy.generate_daily_tank_schedule(date(2026, 6, 1), mode="normal")
    warmup = next(r for r in rows if r["action_type"] == "tank_warmup")
    assert warmup["params"]["tank_temp"] == 46


# ---------------------------------------------------------------------------
# Action shape contract (so heartbeat dispatcher can apply them)
# ---------------------------------------------------------------------------


def test_action_row_required_keys():
    """All rows have the keys db.upsert_action requires."""
    rows = dhw_policy.generate_daily_tank_schedule(date(2026, 6, 1), mode="normal")
    for r in rows:
        assert set(r.keys()) >= {"device", "action_type", "start_time", "end_time", "params"}
        assert r["device"] == "daikin"
        # tank_power, tank_temp are what daikin_bulletproof expects
        assert "tank_power" in r["params"]
        assert "tank_temp" in r["params"]
        assert isinstance(r["params"]["tank_temp"], int)


def test_warmup_does_not_set_tank_powerful():
    """Default warmup is gentle: powerful=False. Only negative_boost uses powerful=True."""
    rows = dhw_policy.generate_daily_tank_schedule(date(2026, 6, 1), mode="normal")
    warmup = next(r for r in rows if r["action_type"] == "tank_warmup")
    assert warmup["params"]["tank_powerful"] is False


def test_negative_boost_uses_powerful():
    """Negative-price boost loads tank fast — tank_powerful=True."""
    outgoing = [{"valid_from": "2026-06-01T14:00:00Z", "value_inc_vat": -5.0}]
    rows = dhw_policy.generate_daily_tank_schedule(
        date(2026, 6, 1), agile_rates=outgoing, mode="normal",
    )
    boost = next(r for r in rows if r["action_type"] == "tank_negative_boost")
    assert boost["params"]["tank_powerful"] is True


def test_precool_lowers_setback_into_negative_window(monkeypatch):
    # A negative window in the setback period + evening showers (no conflict) →
    # the setback target drops toward the device minimum (pre-cool).
    monkeypatch.setattr(config, "DHW_TANK_PRECOOL_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "DHW_TANK_PRECOOL_TARGET_C", 30, raising=False)
    monkeypatch.setattr(config, "DHW_SHOWER_SCHEDULE", "19:00-22:00", raising=False)
    neg = datetime(2026, 6, 2, 4, 0, tzinfo=UTC)  # inside setback (22:00 d1→13:00 d2)
    rates = [{"valid_from": neg.isoformat().replace("+00:00", "Z"), "value_inc_vat": -5.0}]
    rows = dhw_policy.generate_daily_tank_schedule(date(2026, 6, 1), mode="normal", agile_rates=rates)
    setback = [r for r in rows if r["action_type"] == "tank_setback"][0]
    assert setback["params"]["tank_temp"] == 30


def test_no_precool_without_negative_window(monkeypatch):
    monkeypatch.setattr(config, "DHW_TANK_PRECOOL_ENABLED", True, raising=False)
    rows = dhw_policy.generate_daily_tank_schedule(date(2026, 6, 1), mode="normal")
    setback = [r for r in rows if r["action_type"] == "tank_setback"][0]
    assert setback["params"]["tank_temp"] == int(round(float(config.DHW_TEMP_SETBACK_C)))


def test_no_precool_when_shower_in_span(monkeypatch):
    # A morning shower between setback start and the boost → guard blocks precool.
    monkeypatch.setattr(config, "DHW_TANK_PRECOOL_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "DHW_SHOWER_SCHEDULE", "07:00-08:00", raising=False)
    neg = datetime(2026, 6, 2, 9, 0, tzinfo=UTC)  # boost after the 07:00 BST shower
    rates = [{"valid_from": neg.isoformat().replace("+00:00", "Z"), "value_inc_vat": -5.0}]
    rows = dhw_policy.generate_daily_tank_schedule(date(2026, 6, 1), mode="normal", agile_rates=rates)
    setback = [r for r in rows if r["action_type"] == "tank_setback"][0]
    assert setback["params"]["tank_temp"] == int(round(float(config.DHW_TEMP_SETBACK_C)))
