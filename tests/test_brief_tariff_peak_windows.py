"""Tariff peak windows surfaced in brief — independent of LP `peak` count.

When PV+battery cover load through expensive Octopus hours, LP `kind_counts`
shows ``peak=0`` even though the tariff has a 36p evening. The family reads
"peak=0" as "no expensive slots tomorrow", which is wrong.

`_tariff_peak_windows_summary` is the fix: it queries Octopus rates directly
and returns a one-line "Tariff peak: 17:00–19:30 (max 36.4p, 6 slots ≥ 25p)"
that the morning + night briefs include unconditionally.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src import db
from src.analytics.daily_brief import _tariff_peak_windows_summary
from src.config import config as app_config


@pytest.fixture(autouse=True)
def _init_db() -> None:
    db.init_db()


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_config, "BULLETPROOF_TIMEZONE", "Europe/London")
    monkeypatch.setattr(app_config, "OCTOPUS_TARIFF_CODE", "AGILE-TEST")


def _seed_day(target_local_date: date, evening_peak_p: float = 36.0,
              cheap_p: float = 9.0, peak_block_hours: tuple[int, int] = (17, 19)) -> None:
    """Seed 48 half-hour rates for a local day. Peak block in BST hours given."""
    tz = ZoneInfo("Europe/London")
    rates: list[dict] = []
    # Iterate 48 half-hour slots in local time, then convert to UTC for storage.
    start_local = datetime.combine(target_local_date, datetime.min.time(), tzinfo=tz)
    for i in range(48):
        slot_start_local = start_local + timedelta(minutes=30 * i)
        slot_end_local = slot_start_local + timedelta(minutes=30)
        local_hour = slot_start_local.hour
        if peak_block_hours[0] <= local_hour < peak_block_hours[1] + 1:
            price = evening_peak_p
        else:
            price = cheap_p
        rates.append({
            "valid_from": slot_start_local.astimezone(UTC).isoformat(),
            "valid_to": slot_end_local.astimezone(UTC).isoformat(),
            "value_inc_vat": price,
        })
    db.save_agile_rates(rates, "AGILE-TEST")


def test_returns_none_when_no_rates_loaded() -> None:
    out = _tariff_peak_windows_summary(date.today(), ZoneInfo("Europe/London"))
    assert out is None


def test_returns_none_when_no_slots_above_threshold() -> None:
    """All-cheap day → no peak window to surface."""
    target = date.today() + timedelta(days=1)
    _seed_day(target, evening_peak_p=12.0, cheap_p=8.0)   # both well below 25p
    out = _tariff_peak_windows_summary(target, ZoneInfo("Europe/London"))
    assert out is None


def test_surfaces_evening_peak_block() -> None:
    """Sun 17:00–19:30 BST at 36p → expected one-line summary covering that window."""
    target = date.today() + timedelta(days=1)
    _seed_day(target, evening_peak_p=36.4, cheap_p=15.0,
              peak_block_hours=(17, 19))                  # 17:00 → 19:59 BST
    out = _tariff_peak_windows_summary(target, ZoneInfo("Europe/London"))
    assert out is not None
    assert "Tariff peak" in out
    assert "17:00" in out
    assert "20:00" in out                                  # block end is exclusive end of last slot
    assert "36.4p" in out


def test_threshold_configurable() -> None:
    """Lower threshold should surface windows that the default 25p hides."""
    target = date.today() + timedelta(days=1)
    _seed_day(target, evening_peak_p=22.0, cheap_p=8.0,
              peak_block_hours=(17, 19))
    # Default threshold (25p) → no peak
    out_default = _tariff_peak_windows_summary(target, ZoneInfo("Europe/London"))
    assert out_default is None
    # Lowered threshold (20p) → window surfaces
    import src.config as cfg_mod
    cfg_mod.config.BRIEF_TARIFF_PEAK_THRESHOLD_PENCE = 20.0
    try:
        out_low = _tariff_peak_windows_summary(target, ZoneInfo("Europe/London"))
        assert out_low is not None
        assert "22.0p" in out_low
    finally:
        delattr(cfg_mod.config, "BRIEF_TARIFF_PEAK_THRESHOLD_PENCE")
