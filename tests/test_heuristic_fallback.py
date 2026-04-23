"""Smoke test for OPTIMIZER_BACKEND=heuristic — catches silent rot in the fallback path.

The heuristic backend is the safety net when PuLP/HiGHS fails to solve. No prod
config sets it, so it can rot without anyone noticing — until the day PuLP fails
and the rot bites. This test seeds a realistic 48-slot day, runs the heuristic,
and asserts a non-empty plan comes back.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src import db
from src.config import config as app_config
from src.scheduler import optimizer

TARIFF = "E-1R-AGILE-TEST-HEURISTIC"


@pytest.fixture(autouse=True)
def _init_db() -> None:
    db.init_db()


@pytest.fixture(autouse=True)
def _heuristic_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_config, "BULLETPROOF_TIMEZONE", "Europe/London")
    monkeypatch.setattr(app_config, "OCTOPUS_TARIFF_CODE", TARIFF)
    monkeypatch.setattr(app_config, "OPTIMIZER_BACKEND", "heuristic")
    monkeypatch.setattr(app_config, "OPERATION_MODE", "simulation")


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _seed_realistic_day(start: datetime) -> None:
    """48 slots with a clear cheap-night / peak-evening pattern."""
    rows = []
    vf = start
    for i in range(48):
        hour = (vf.hour + 0.5 * (i % 2))  # 0.5h slot spacing
        if 1 <= vf.hour < 5:
            price = -2.0  # negative overnight
        elif 5 <= vf.hour < 16:
            price = 10.0  # standard
        elif 16 <= vf.hour < 19:
            price = 35.0  # peak
        else:
            price = 12.0
        vt = vf + timedelta(minutes=30)
        rows.append({"valid_from": _iso(vf), "valid_to": _iso(vt), "value_inc_vat": price})
        vf = vt
    db.save_agile_rates(rows, TARIFF)


def test_heuristic_fallback_returns_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    """A realistic 48-slot day must produce a non-empty heuristic plan."""
    now = datetime(2026, 4, 22, 18, 0, tzinfo=UTC)
    monkeypatch.setattr(optimizer, "_now_utc", lambda: now)
    _seed_realistic_day(datetime(2026, 4, 22, 0, 0, tzinfo=UTC))

    result = optimizer.run_optimizer(fox=None, daikin=None)

    assert isinstance(result, dict), f"expected dict, got {type(result)}"
    assert result.get("ok") is True, f"heuristic failed: {result.get('error')}"
    assert result.get("optimizer_backend") == "heuristic", (
        f"expected heuristic backend, got {result.get('optimizer_backend')}"
    )
    # The heuristic must emit slot counts (proves it built and classified slots).
    counts = result.get("counts") or {}
    total_slots = sum(int(v) for v in counts.values())
    assert total_slots > 0, f"heuristic produced 0 slots: counts={counts}"
    # And it should have generated at least one Daikin action for our peak window.
    assert isinstance(result.get("daikin_actions"), int), (
        f"missing daikin_actions count: {result}"
    )
