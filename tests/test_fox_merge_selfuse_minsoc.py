"""_coarse_merge_fox must not freeze the battery when it collapses a
solar_charge SelfUse window (minSocOnGrid=100, "hold for PV") with a normal
SelfUse window (minSocOnGrid=10, "discharge"). Taking the MAX froze the whole
merged window at 100% → the battery couldn't discharge during the evening peak
it was charged for (prod 2026-06-06). The merge must take the MIN so discharge
stays allowed wherever any constituent window allowed it.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.scheduler.optimizer import _coarse_merge_fox

_T0 = datetime(2026, 6, 6, 9, 0, tzinfo=UTC)


def _w(h0: int, h1: int, msg: int, max_soc=None):
    return (_T0 + timedelta(hours=h0), _T0 + timedelta(hours=h1),
            ("SelfUse", None, None, msg, max_soc))


def test_solar_charge_does_not_freeze_adjacent_discharge():
    # solar_charge (hold, min 100) immediately followed by a self-use window
    # that must DISCHARGE (min 10). They must NOT collapse into one window
    # frozen at 100% — the discharge window keeps its reserve floor (the prod
    # bug: a 100% min spanned the whole evening so the battery never discharged).
    merged = _coarse_merge_fox([_w(0, 3, 100, 100), _w(3, 8, 10, None)])
    assert len(merged) == 2, "differing minSoc must stay separate (not freeze)"
    # the hold stays a hold; the discharge window keeps min 10 (not frozen to 100)
    assert merged[0][2][3] == 100
    assert merged[1][2][3] == 10, f"discharge window frozen: min={merged[1][2][3]}"


def test_two_solar_charge_holds_stay_held():
    merged = _coarse_merge_fox([_w(0, 3, 100, 100), _w(3, 6, 100, 100)])
    assert len(merged) == 1
    assert merged[0][2][3] == 100  # both hold → stays a hold


def test_two_normal_selfuse_stay_at_reserve():
    merged = _coarse_merge_fox([_w(0, 3, 10), _w(3, 6, 10)])
    assert len(merged) == 1
    assert merged[0][2][3] == 10


# --- #679: solar_charge is now Backup, a different workMode. The #480 same-floor
# SelfUse guard is unchanged; it just no longer needs to handle a solar_charge
# SelfUse(100) — that shape is retired. Backup structurally cannot merge with
# SelfUse (workMode differs), so an elevated hold can never absorb a discharge
# window through this path. -------------------------------------------------


def _backup(h0: int, h1: int, max_soc=None):
    return (_T0 + timedelta(hours=h0), _T0 + timedelta(hours=h1),
            ("Backup", None, None, 10, max_soc))


def test_backup_solar_charge_does_not_merge_into_selfuse():
    # A Backup hold (the new solar_charge shape) adjacent to a normal SelfUse
    # discharge window must NOT collapse — different workMode.
    merged = _coarse_merge_fox([_backup(0, 3, 90), _w(3, 8, 10, None)])
    assert len(merged) == 2
    assert merged[0][2][0] == "Backup"
    assert merged[1][2][0] == "SelfUse" and merged[1][2][3] == 10


def test_480_same_floor_guard_still_separates_elevated_selfuse():
    # The general #480 guard is intact: two SelfUse windows only merge when
    # their minSoc floors match, so an elevated floor never absorbs a reserve
    # one (regression guard for other SelfUse-floor variants).
    merged = _coarse_merge_fox([_w(0, 3, 50, None), _w(3, 6, 10, None)])
    assert len(merged) == 2
    assert merged[0][2][3] == 50
    assert merged[1][2][3] == 10
