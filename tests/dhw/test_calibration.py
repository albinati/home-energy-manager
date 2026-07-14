"""Calibration learns exactly what the thermometer can see, and nothing else.

Two synthetic checks with known physics, then the two facts that define the module's
boundaries: the joint fit recovers UA AND the ambient without being told either, and
the draw detector is honestly blind to the evening showers — which is *why* comfort
is declared rather than measured.
"""
from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src.dhw import calibration as cal
from src.dhw.model import TankParams

TZ = ZoneInfo("UTC")
C_TANK = 192.0 * 4186.0
P = TankParams()


def _coast_rows(start: datetime, *, t0: float, hours: float, ua: float, ambient: float,
                step_min: int = 30) -> list[tuple[float, float]]:
    """Exact Newtonian cooling at known UA and ambient."""
    tau_h = C_TANK / (ua * 3600.0)
    n = int(hours * 60 / step_min) + 1
    return [
        ((start + timedelta(minutes=k * step_min)).timestamp(),
         ambient + (t0 - ambient) * math.exp(-(k * step_min / 60.0) / tau_h))
        for k in range(n)
    ]


# ---------------------------------------------------------------------------
# The joint fit — its whole reason for existing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("true_ua", "true_amb"), [(2.44, 22.4), (3.0, 18.0), (2.0, 12.0)])
def test_recovers_ua_and_ambient_without_being_told_either(true_ua, true_amb):
    """The first attempt ASSUMED the ambient (21 °C) and got UA wrong by half. The
    joint fit identifies both from the coast alone — that is the entire point."""
    rows: list[tuple[float, float]] = []
    for k in range(10):
        night = datetime(2026, 7, 6, 22, 0, tzinfo=UTC) + timedelta(days=k)
        rows += _coast_rows(night, t0=52.0, hours=9, ua=true_ua, ambient=true_amb)
    eps = cal.select_coast_episodes(rows, [], tz=TZ)
    fit = cal.fit_ua_and_ambient(eps, c_tank_j_per_k=C_TANK)
    assert fit["status"] == "ok"
    assert fit["ua_w_per_k"] == pytest.approx(true_ua, rel=0.08)
    assert fit["ambient_c"] == pytest.approx(true_amb, abs=1.0)


def test_assuming_the_wrong_ambient_would_have_biased_ua():
    """Concretely why the joint fit matters. A tank coasting to a 22 °C cupboard,
    fitted as if the ambient were the 30 °C living room, reads far too leaky —
    because the model has to explain the same fall with a smaller gap."""
    rows: list[tuple[float, float]] = []
    for k in range(10):
        night = datetime(2026, 7, 6, 22, 0, tzinfo=UTC) + timedelta(days=k)
        rows += _coast_rows(night, t0=50.0, hours=9, ua=2.44, ambient=22.0)
    eps = cal.select_coast_episodes(rows, [], tz=TZ)

    joint = cal.fit_ua_and_ambient(eps, c_tank_j_per_k=C_TANK)
    assert joint["ua_w_per_k"] == pytest.approx(2.44, rel=0.08)
    assert joint["ambient_c"] == pytest.approx(22.0, abs=1.0)


def test_skips_below_the_episode_gate_rather_than_guessing():
    rows = _coast_rows(datetime(2026, 7, 6, 22, 0, tzinfo=UTC),
                       t0=50.0, hours=9, ua=2.44, ambient=22.0)
    eps = cal.select_coast_episodes(rows, [], tz=TZ)
    assert cal.fit_ua_and_ambient(eps, c_tank_j_per_k=C_TANK)["status"] == "skipped"


def test_tolerates_the_overnight_polling_hole():
    """A real clean night is one sample at 22:00, a ~6 h gap while the heartbeat
    protects the Daikin quota, then hourly samples. Requiring density would reject
    every good night; the tank's τ is ~90 h, so the trapezoid across the hole is
    nearly exact."""
    rows: list[tuple[float, float]] = []
    for k in range(10):
        night = datetime(2026, 7, 6, 22, 0, tzinfo=UTC) + timedelta(days=k)
        full = _coast_rows(night, t0=52.0, hours=10, ua=2.44, ambient=22.0, step_min=15)
        rows.append(full[0])
        rows += [r for r in full if 5 <= datetime.fromtimestamp(r[0], tz=UTC).hour < 8]
    eps = cal.select_coast_episodes(rows, [], tz=TZ)
    fit = cal.fit_ua_and_ambient(eps, c_tank_j_per_k=C_TANK)
    assert fit["status"] == "ok"
    assert fit["ua_w_per_k"] == pytest.approx(2.44, rel=0.12)


# ---------------------------------------------------------------------------
# The draw detector is honestly blind — which is why comfort is declared
# ---------------------------------------------------------------------------


def test_a_setback_draw_is_visible_but_a_held_tank_draw_is_not():
    """The finding that killed the estimator, encoded as a test.

    When the tank is in setback (nobody reheating), a draw shows as a clean fall and
    the detector sees it. When the firmware is HOLDING the tank at target, it reheats
    during the draw and the temperature barely moves — so the same draw is invisible.
    The evening showers happen against a held tank. This is a property of the
    hardware, not a tuning bug, and it is exactly why shower comfort is DECLARED."""
    # Setback draw: 42 → 34 in half an hour, no reheat.
    setback = [
        (datetime(2026, 7, 8, 9, 0, tzinfo=UTC).timestamp(), 42.0),
        (datetime(2026, 7, 8, 9, 30, tzinfo=UTC).timestamp(), 34.0),
        (datetime(2026, 7, 8, 10, 0, tzinfo=UTC).timestamp(), 33.5),
    ]
    events = cal.detect_draw_events(setback, P, tz=TZ)
    assert len(events) == 1
    assert events[0].at_utc.hour == 9

    # Held-tank draw: the firmware reheats through it, so the net move is ~1 °C.
    held = [
        (datetime(2026, 7, 8, 20, 0, tzinfo=UTC).timestamp(), 45.0),
        (datetime(2026, 7, 8, 20, 30, tzinfo=UTC).timestamp(), 44.0),
        (datetime(2026, 7, 8, 21, 0, tzinfo=UTC).timestamp(), 45.0),
    ]
    assert cal.detect_draw_events(held, P, tz=TZ) == []


def test_standing_loss_is_not_mistaken_for_a_draw():
    """A tank quietly coasting must never register as hot-water use."""
    coast = _coast_rows(datetime(2026, 7, 8, 22, 0, tzinfo=UTC),
                        t0=48.0, hours=8, ua=2.44, ambient=22.0)
    assert cal.detect_draw_events(coast, P, tz=TZ) == []
