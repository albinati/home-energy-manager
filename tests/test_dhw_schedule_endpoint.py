"""GET /api/v1/daikin/dhw-schedule — spans today+tomorrow and surfaces the
negative-price boost row when Outgoing rates have a negative window."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from src.config import config


def test_dhw_schedule_two_days_with_negative_boost(monkeypatch):
    import src.db as db
    from src.api import main

    monkeypatch.setattr(config, "OCTOPUS_TARIFF_CODE", "E-1R-AGILE-FLEX", raising=False)
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "normal", raising=False)

    # Negative IMPORT slot tomorrow ~15:00 UTC (inside tomorrow's warmup horizon).
    tomorrow = (datetime.now(UTC) + timedelta(days=1)).date()
    neg = datetime(tomorrow.year, tomorrow.month, tomorrow.day, 15, 0, tzinfo=UTC)

    def _fake_rates(tariff, a, b):  # get_rates_for_period(tariff, from_dt, to_dt)
        if a <= neg < b:
            return [{"valid_from": neg.isoformat().replace("+00:00", "Z"), "value_inc_vat": -4.0}]
        return []
    monkeypatch.setattr(db, "get_rates_for_period", _fake_rates)

    resp = asyncio.run(main.daikin_dhw_schedule())
    rows = resp["rows"]
    assert rows, "expected schedule rows"
    # Rows span two days (today + tomorrow).
    days = {r["start_utc"][:10] for r in rows if r.get("start_utc")}
    assert len(days) >= 2, f"expected today+tomorrow, got {days}"
    # The negative window produced a boost row at the commandable setpoint cap
    # (DHW_NEGATIVE_PRICE_BOOST_C = 60 °C; the heat pump rejects higher). Note
    # this is NOT DHW_TEMP_MAX_C (65, the physical/immersion ceiling).
    boosts = [r for r in rows if r["action_type"] == "tank_negative_boost"]
    assert boosts, "expected a tank_negative_boost row for the negative window"
    expected = int(round(min(config.DHW_NEGATIVE_PRICE_BOOST_C, config.DHW_TEMP_MAX_C)))
    assert boosts[0]["tank_temp_c"] == expected


def test_dhw_schedule_no_rates_no_boost(monkeypatch):
    import src.db as db
    from src.api import main

    monkeypatch.setattr(config, "OCTOPUS_TARIFF_CODE", "E-1R-AGILE-FLEX", raising=False)
    monkeypatch.setattr(db, "get_rates_for_period", lambda tariff, a, b: [])
    resp = asyncio.run(main.daikin_dhw_schedule())
    assert not [r for r in resp["rows"] if r["action_type"] == "tank_negative_boost"]
