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

    monkeypatch.setattr(config, "OCTOPUS_EXPORT_TARIFF_CODE", "E-1R-AGILE-OUTGOING", raising=False)
    monkeypatch.setattr(config, "OPTIMIZATION_PRESET", "normal", raising=False)

    # Negative slot tomorrow ~15:00 UTC (inside tomorrow's warmup horizon).
    tomorrow = (datetime.now(UTC) + timedelta(days=1)).date()
    neg = datetime(tomorrow.year, tomorrow.month, tomorrow.day, 15, 0, tzinfo=UTC)

    def _fake_rates(a, b):
        # Return the negative slot only when the requested range covers it.
        if a <= neg.isoformat().replace("+00:00", "Z") < b:
            return [{"valid_from": neg.isoformat().replace("+00:00", "Z"), "value_inc_vat": -4.0}]
        return []
    monkeypatch.setattr(db, "get_agile_export_rates_in_range", _fake_rates)

    resp = asyncio.run(main.daikin_dhw_schedule())
    rows = resp["rows"]
    assert rows, "expected schedule rows"
    # Rows span two days (today + tomorrow).
    days = {r["start_utc"][:10] for r in rows if r.get("start_utc")}
    assert len(days) >= 2, f"expected today+tomorrow, got {days}"
    # The negative window produced a boost row at MAX (65).
    boosts = [r for r in rows if r["action_type"] == "tank_negative_boost"]
    assert boosts, "expected a tank_negative_boost row for the negative window"
    assert boosts[0]["tank_temp_c"] == int(config.DHW_TEMP_MAX_C)


def test_dhw_schedule_no_rates_no_boost(monkeypatch):
    import src.db as db
    from src.api import main

    monkeypatch.setattr(config, "OCTOPUS_EXPORT_TARIFF_CODE", "E-1R-AGILE-OUTGOING", raising=False)
    monkeypatch.setattr(db, "get_agile_export_rates_in_range", lambda a, b: [])
    resp = asyncio.run(main.daikin_dhw_schedule())
    assert not [r for r in resp["rows"] if r["action_type"] == "tank_negative_boost"]
