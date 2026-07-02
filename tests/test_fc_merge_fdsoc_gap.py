"""Depth-aware ForceCharge merge (PR D, 2026-07-02 LP audit).

A negative_hold row (fdSoc ≈ reserve — "hold, don't fill") merged into a
negative row (fdSoc 100 — "fill at the paid price") used to take max(fdSoc)=100
across the whole window, so Fox front-loaded the fill at the shallow price.
Merging is now per intent class (hold: fdSoc <= LP_FC_MERGE_HOLD_FDSOC_MAX;
fill: above): hold+hold and fill+fill merge as before — only hold↔fill
transitions produce a group boundary, so 8-group cap pressure is unchanged.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.config import config
from src.scheduler.optimizer import _merge_adjacent_force_charge_rows


def _row(h0, h1, fd_soc, fd_pwr=5000, mode="ForceCharge"):
    t0 = datetime(2026, 1, 15, tzinfo=UTC)
    return (
        t0 + timedelta(hours=h0),
        t0 + timedelta(hours=h1),
        (mode, fd_soc, fd_pwr, 10, None),
    )


def test_hold_and_fill_stay_separate():
    rows = [_row(4, 8, 15), _row(8, 15, 100)]  # hold@reserve then fill@100
    out = _merge_adjacent_force_charge_rows(rows)
    assert len(out) == 2, "hold must NOT be swallowed by the fill window"
    assert out[0][2][1] == 15 and out[1][2][1] == 100


def test_hold_runs_merge_and_fill_runs_merge():
    # hold, hold, fill(70), fill(100) → exactly 2 groups (the #607 test shape)
    rows = [_row(4, 6, 12), _row(6, 8, 12), _row(8, 10, 70), _row(10, 15, 100)]
    out = _merge_adjacent_force_charge_rows(rows)
    assert len(out) == 2
    assert out[0][2][1] == 12          # hold group keeps the reserve target
    assert out[1][2][1] == 100         # fill group takes max of the taper


def test_tapered_fill_run_is_one_group():
    rows = [_row(4, 8, 70), _row(8, 15, 100)]  # both fill class
    out = _merge_adjacent_force_charge_rows(rows)
    assert len(out) == 1 and out[0][2][1] == 100


def test_legacy_always_merge_via_config(monkeypatch):
    monkeypatch.setattr(config, "LP_FC_MERGE_HOLD_FDSOC_MAX", -1.0, raising=False)
    out = _merge_adjacent_force_charge_rows([_row(4, 8, 15), _row(8, 15, 100)])
    assert len(out) == 1  # rollback: nothing classifies as hold → always merge


def test_weighted_pwr_preserved_on_merge():
    rows = [_row(4, 8, 95, fd_pwr=4000), _row(8, 12, 100, fd_pwr=2000)]
    out = _merge_adjacent_force_charge_rows(rows)
    assert len(out) == 1
    assert out[0][2][2] == 3000  # duration-weighted (4h@4000 + 4h@2000)


def test_non_forcecharge_untouched():
    rows = [_row(4, 8, 15), _row(8, 15, 100, mode="SelfUse")]
    out = _merge_adjacent_force_charge_rows(rows)
    assert len(out) == 2
