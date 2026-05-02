"""PV-aware appliance window picker (#219).

Audit finding (2026-05-02): ``find_cheapest_window`` picked the cheapest
*Agile import* slot, even when daytime PV would have made the run free.
Live prod check at 15:20 BST showed 2.4 kW solar / 0.6 kW load / 100% SoC
/ -1.8 kW grid (exporting). Marginal cost of running the washer THEN was
~equal to forgone export revenue ≈ 18p/kWh. The dispatcher picked
21:00–23:00 (Agile cheapest at 20.6p), missing the free-PV pocket.

These tests lock the new marginal-cost path: PV-rich slots beat
slightly-cheaper night slots, and absence of forecasts falls back to the
legacy import-only behavior.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.scheduler import appliance_dispatch


def _slots(n: int, base: datetime | None = None) -> list[datetime]:
    base = base or datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    return [base + i * timedelta(minutes=30) for i in range(n)]


def test_marginal_cost_pv_surplus_beats_export_rate() -> None:
    """When residual_pv >= washer_kwh, marginal cost = washer_kwh × export_rate."""
    starts = _slots(2)
    # Build the marginal_cost_per_slot map directly (skip the forecast plumbing)
    # 0.5 kW washer × 0.5 h = 0.25 kWh per slot
    # If PV residual > 0.25 kWh: cost = 0.25 × export_rate
    # At export rate 18p, cost = 4.5p per slot
    marginal = {
        starts[0]: 0.25 * 18.0,        # 4.5p — PV-covered (sunny)
        starts[1]: 0.25 * 25.0,        # 6.25p — partial PV + grid
    }
    start, end, avg = appliance_dispatch.find_cheapest_window(
        starts[0], starts[-1] + timedelta(minutes=30),
        duration_minutes=30,
        marginal_cost_per_slot=marginal,
    )
    assert start == starts[0]
    assert end == starts[0] + timedelta(minutes=30)
    assert avg == pytest.approx(4.5)


def test_pv_aware_picks_pv_rich_window_over_cheaper_night() -> None:
    """The user's case: 21:00 has cheaper Agile import (20p), but 14:00 has
    free PV → dispatcher should pick 14:00."""
    starts = _slots(20)  # 14:00–23:30 across 10 hours
    # Slots 0–3 (14:00–15:30): PV is exporting at 18p → marginal cost = 0.25×18 = 4.5p
    # Slots 4–15 (16:00–21:30): no PV, normal Agile (~25p) → 0.25×25 = 6.25p
    # Slots 16–19 (22:00–23:30): cheap Agile (20p), no PV → 0.25×20 = 5p
    marginal = {}
    for i, s in enumerate(starts):
        if i < 4:
            marginal[s] = 4.5
        elif i >= 16:
            marginal[s] = 5.0
        else:
            marginal[s] = 6.25
    start, end, avg = appliance_dispatch.find_cheapest_window(
        starts[0], starts[-1] + timedelta(minutes=30),
        duration_minutes=120,                # 4 slots
        marginal_cost_per_slot=marginal,
    )
    # Expected: pick slots 0–3 (14:00) because 4 × 4.5p = 18p < 4 × 5p = 20p
    assert start == starts[0], f"Expected 14:00 PV window, got {start}"
    assert avg == pytest.approx(4.5)


def test_falls_back_to_import_only_when_no_marginal_map(
    monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    """No marginal_cost_per_slot supplied → legacy import-only path
    (sliding-window minimum on agile_rates)."""
    from src import db
    monkeypatch.setattr("src.config.config.DB_PATH", str(tmp_path / "t.db"), raising=False)
    db.init_db()
    tariff = "AGILE-TEST-FALLBACK"
    monkeypatch.setattr("src.config.config.OCTOPUS_TARIFF_CODE", tariff)
    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    rows = []
    for i in range(8):
        st = base + i * timedelta(minutes=30)
        # Slot 4 (14:00): super cheap (1p). Others: 20p.
        price = 1.0 if i == 4 else 20.0
        rows.append({
            "valid_from": st.isoformat().replace("+00:00", "Z"),
            "valid_to": (st + timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
            "value_inc_vat": price,
        })
    db.save_agile_rates(rows, tariff)

    start, end, avg = appliance_dispatch.find_cheapest_window(
        base, base + timedelta(hours=4),
        duration_minutes=30,
        marginal_cost_per_slot=None,        # legacy
    )
    assert start == base + timedelta(minutes=120), (
        f"Should pick slot 4 (the 1p slot), got {start.isoformat()}"
    )
    assert avg == pytest.approx(1.0)


def test_marginal_cost_window_must_be_contiguous() -> None:
    """A 60-min window can't span over a missing slot — picker must reject
    gaps and fall through to the import-only path or raise."""
    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    # Slot 0 cheap, slot 1 missing, slot 2 cheap → should NOT pick slots 0+2
    marginal = {
        base: 1.0,
        base + timedelta(hours=1): 1.0,    # gap! skipped slot at +30 min
    }
    # Without a contiguous window the marginal-cost path returns None and
    # the function falls through to the import-only path. With no DB rows
    # set up here, that returns the fallback window.
    from src.config import config as app_config
    # APPLIANCE_FALLBACK_WINDOW_LOCAL default is "02:00-05:00" — picker uses it.
    start, end, avg = appliance_dispatch.find_cheapest_window(
        base, base + timedelta(hours=4),
        duration_minutes=60,                # 2 slots
        marginal_cost_per_slot=marginal,
    )
    # Should NOT have picked a non-contiguous combination
    # (avg=0 indicates fallback path used; that's the documented contract)
    assert avg == 0.0 or start != base, (
        f"Picker should not stitch non-contiguous slots; got start={start}, avg={avg}"
    )


def test_build_marginal_cost_returns_none_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``APPLIANCE_PV_AWARE_DISPATCH=false`` → builder returns None
    (legacy fallback path is taken)."""
    monkeypatch.setattr("src.config.config.APPLIANCE_PV_AWARE_DISPATCH", False)
    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    result = appliance_dispatch.build_marginal_cost_per_slot(
        base, base + timedelta(hours=4), appliance_kw=0.5,
    )
    assert result is None


def test_build_marginal_cost_returns_none_when_no_tariff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.config.config.APPLIANCE_PV_AWARE_DISPATCH", True)
    monkeypatch.setattr("src.config.config.OCTOPUS_TARIFF_CODE", "")
    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    result = appliance_dispatch.build_marginal_cost_per_slot(
        base, base + timedelta(hours=4), appliance_kw=0.5,
    )
    assert result is None
