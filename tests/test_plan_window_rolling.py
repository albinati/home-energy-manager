"""Rolling 24 h planning window: start = now ceil-to-HH:30 UTC, end = min(now + LP_HORIZON_HOURS, last Agile slot)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src import db
from src.config import config as app_config
from src.scheduler import optimizer
from src.scheduler.optimizer import _resolve_plan_window

TARIFF = "E-1R-AGILE-TEST-A"


@pytest.fixture(autouse=True)
def _init_db() -> None:
    db.init_db()


@pytest.fixture(autouse=True)
def _london_tz(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_config, "BULLETPROOF_TIMEZONE", "Europe/London")
    monkeypatch.setattr(app_config, "OCTOPUS_TARIFF_CODE", TARIFF)
    monkeypatch.setattr(app_config, "LP_HORIZON_HOURS", 24)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _seed_rates(valid_from: datetime, count: int, price: float = 10.0) -> None:
    rows: list[dict[str, object]] = []
    vf = valid_from
    for _ in range(count):
        vt = vf + timedelta(minutes=30)
        rows.append({"valid_from": _iso(vf), "valid_to": _iso(vt), "value_inc_vat": price})
        vf = vt
    db.save_agile_rates(rows, TARIFF)


def test_rolling_24h_when_full_tomorrow_available(monkeypatch: pytest.MonkeyPatch) -> None:
    """At 18:00 UTC with tomorrow fully published, window is 18:30 UTC today → 18:30 UTC tomorrow."""
    now = datetime(2026, 4, 22, 18, 0, tzinfo=UTC)
    monkeypatch.setattr(optimizer, "_now_utc", lambda: now)
    _seed_rates(datetime(2026, 4, 22, 0, 0, tzinfo=UTC), 96)

    w = _resolve_plan_window(TARIFF)
    assert w is not None
    assert w.day_start == datetime(2026, 4, 22, 18, 30, tzinfo=UTC)
    assert w.horizon_end == datetime(2026, 4, 23, 18, 30, tzinfo=UTC)
    assert w.horizon_hours == pytest.approx(24.0)


def test_rolling_extends_with_priors_when_tomorrow_not_published(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S10.2 (#169): when rates run out before LP_HORIZON_HOURS, synthesised
    rows from historical median per-hour-of-day priors fill the tail. With a
    24 h horizon and 23 h of seeded rates ending at 23:00 UTC, the window
    should still cover 9:30 → 9:30 next day (24 h) using priors for the gap.
    """
    now = datetime(2026, 4, 22, 9, 0, tzinfo=UTC)
    monkeypatch.setattr(optimizer, "_now_utc", lambda: now)
    # Seed enough historical rates that priors are non-empty (28 d before today)
    _seed_rates(datetime(2026, 4, 1, 0, 0, tzinfo=UTC), 1000, price=12.0)
    # Plus today's 23 h that the LP would normally use
    _seed_rates(datetime(2026, 4, 22, 0, 0, tzinfo=UTC), 46, price=10.0)

    w = _resolve_plan_window(TARIFF)
    assert w is not None
    assert w.day_start == datetime(2026, 4, 22, 9, 30, tzinfo=UTC)
    # Horizon now extends to full 24h via priors instead of truncating at 23:00
    assert w.horizon_end == datetime(2026, 4, 23, 9, 30, tzinfo=UTC)
    assert w.horizon_hours == pytest.approx(24.0)
    # Confirm at least one synthesised "prior" row is present in rates
    prior_rows = [r for r in w.rates if r.get("fetched_at") == "prior"]
    assert prior_rows, "expected synthesised prior rows in rates list"


def test_rolling_truncates_when_no_priors_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No history → can't synthesise; legacy truncation behaviour applies."""
    now = datetime(2026, 4, 22, 9, 0, tzinfo=UTC)
    monkeypatch.setattr(optimizer, "_now_utc", lambda: now)
    # Only today's data; no history older than 28 d ago for priors
    _seed_rates(datetime(2026, 4, 22, 0, 0, tzinfo=UTC), 46)

    # Force priors to be empty by querying with no historical data
    monkeypatch.setattr(
        "src.scheduler.optimizer.db.get_half_hourly_agile_priors",
        lambda *a, **kw: {},
    )

    w = _resolve_plan_window(TARIFF)
    assert w is not None
    assert w.day_start == datetime(2026, 4, 22, 9, 30, tzinfo=UTC)
    assert w.horizon_end == datetime(2026, 4, 22, 23, 0, tzinfo=UTC)
    assert w.horizon_hours == pytest.approx(13.5)


def test_start_advances_past_currently_live_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    """When 'now' lands exactly on a half-hour boundary, start moves to the *next* one."""
    now = datetime(2026, 4, 22, 14, 30, tzinfo=UTC)
    monkeypatch.setattr(optimizer, "_now_utc", lambda: now)
    _seed_rates(datetime(2026, 4, 22, 0, 0, tzinfo=UTC), 96)

    w = _resolve_plan_window(TARIFF)
    assert w is not None
    assert w.day_start == datetime(2026, 4, 22, 15, 0, tzinfo=UTC)


def test_returns_none_when_no_rates(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 4, 22, 9, 0, tzinfo=UTC)
    monkeypatch.setattr(optimizer, "_now_utc", lambda: now)
    assert _resolve_plan_window(TARIFF) is None


def test_returns_none_below_min_usable_slots(monkeypatch: pytest.MonkeyPatch) -> None:
    """< 4 future half-hour slots (2 h) → Self-Use fallback."""
    now = datetime(2026, 4, 22, 22, 0, tzinfo=UTC)
    monkeypatch.setattr(optimizer, "_now_utc", lambda: now)
    _seed_rates(datetime(2026, 4, 22, 22, 0, tzinfo=UTC), 3)  # 22:00–23:30 → 1 slot after 22:30 ceil
    assert _resolve_plan_window(TARIFF) is None


def test_plan_date_tags_local_start_date(monkeypatch: pytest.MonkeyPatch) -> None:
    """plan_date is the local date of the window start — 23:00 UTC in BST lands on the next local date."""
    # BST (UTC+1): 23:00 UTC = 00:00 next local day
    now = datetime(2026, 4, 22, 23, 0, tzinfo=UTC)
    monkeypatch.setattr(optimizer, "_now_utc", lambda: now)
    _seed_rates(datetime(2026, 4, 22, 0, 0, tzinfo=UTC), 96)

    w = _resolve_plan_window(TARIFF)
    assert w is not None
    # start_utc rounds up to 23:30 UTC → local 00:30 BST on 2026-04-23
    assert w.plan_date == "2026-04-23"


def test_horizon_hours_property_matches_window() -> None:
    w = optimizer.PlanWindow(
        plan_date="2026-04-22",
        day_start=datetime(2026, 4, 22, 12, 0, tzinfo=UTC),
        horizon_end=datetime(2026, 4, 23, 0, 0, tzinfo=UTC),
        rates=[],
    )
    assert w.horizon_hours == pytest.approx(12.0)
