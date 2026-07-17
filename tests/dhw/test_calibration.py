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


# --- #732: reheat differential (firmware deadband) ------------------------------

def _step_series(*segments):
    """Build (epoch, tank, target) rows from (minutes, tank, target) triples."""
    from datetime import UTC, datetime, timedelta
    t0 = datetime(2026, 7, 10, 10, 0, tzinfo=UTC)
    return [((t0 + timedelta(minutes=m)).timestamp(), tank, tgt) for m, tank, tgt in segments]


def test_reheat_differential_finds_the_threshold():
    from src.dhw.calibration import fit_reheat_differential
    rows = _step_series(
        # Δ9 heats
        (0, 38, 37), (5, 38, 47), (35, 40, 47), (65, 44, 47),
        (200, 44, 37),
        # Δ5 does NOT heat across the window
        (300, 42, 37), (305, 42, 47), (340, 42, 47), (395, 42, 47), (410, 42, 47),
        # Δ9 heats
        (600, 38, 37), (605, 38, 47), (650, 41, 47),
        (800, 44, 37),
        # Δ4 does NOT heat
        (900, 43, 37), (905, 43, 47), (1000, 43, 47),
        (1100, 44, 37),
        # Δ8 heats
        (1200, 39, 37), (1205, 39, 47), (1250, 42, 47),
    )
    fit = fit_reheat_differential(rows)
    assert fit["status"] == "ok"
    assert fit["n_episodes"] == 5
    assert fit["n_misclassified"] == 0
    # best split between Δ5 (no heat) and Δ8 (heat) → midpoint 6.5
    assert fit["differential_c"] == 6.5


def test_reheat_differential_excludes_powerful_class_targets():
    """Boosts to 60 °C run Powerful — they heat regardless of the deadband and
    would poison the heated set with small deltas."""
    from src.dhw.calibration import fit_reheat_differential
    rows = _step_series(
        (0, 55, 45), (5, 55, 60), (35, 58, 60),   # Δ5 heated — but boost class
        (300, 42, 37), (305, 42, 47), (400, 42, 47),  # Δ5 no heat (warmup class)
        (600, 38, 37), (605, 38, 47), (650, 41, 47),  # Δ9 heat
    )
    fit = fit_reheat_differential(rows)
    # only the two warmup-class episodes count
    assert fit["n_episodes"] == 2
    assert fit["status"] != "inconsistent"


def test_reheat_differential_discards_draw_contaminated_and_short_windows():
    from src.dhw.calibration import fit_reheat_differential
    rows = _step_series(
        # draw right after the step (tank falls) → discarded
        (0, 44, 37), (5, 44, 47), (20, 41, 47),
        # step observed for only 10 min before target re-steps → discarded
        (300, 42, 37), (305, 42, 47), (315, 42, 60),
    )
    fit = fit_reheat_differential(rows)
    assert fit["n_episodes"] == 0
    assert fit["n_discarded_draw"] >= 1
    assert fit["status"] == "insufficient"


def test_reheat_differential_tolerates_bounded_outliers_and_reports_them():
    """One power-off day (huge Δ, no heat) must not kill the fit — it is
    reported as misclassified, not averaged in (2026-06-26 precool episode)."""
    from src.dhw.calibration import fit_reheat_differential
    rows = _step_series(
        (0, 38, 37), (5, 38, 47), (35, 41, 47),          # Δ9 heats
        (300, 42, 37), (305, 42, 47), (400, 42, 47),      # Δ5 no heat
        (600, 38, 37), (605, 38, 47), (650, 41, 47),      # Δ9 heats
        (900, 43, 37), (905, 43, 47), (1000, 43, 47),     # Δ4 no heat
        (1200, 30, 25), (1205, 30, 45), (1300, 30, 45),   # Δ15 NO heat — power off
    )
    fit = fit_reheat_differential(rows)
    assert fit["status"] == "ok"
    assert fit["n_misclassified"] == 1
    assert fit["misclassified"][0]["delta_c"] == 15.0
    assert fit["differential_c"] == 7.0  # split between 5 and 9


def test_reheat_differential_refuses_when_noise_dominates():
    """More than 25% unexplained episodes = the deadband is not the story in
    this window — refuse rather than report a fiction."""
    from src.dhw.calibration import fit_reheat_differential
    rows = _step_series(
        (0, 43, 37), (5, 43, 47), (40, 46, 47),          # Δ4 heated
        (300, 42, 37), (305, 42, 47), (400, 42, 47),      # Δ5 no heat
        (600, 38, 37), (605, 38, 47), (650, 41, 47),      # Δ9 heat
        (900, 39, 37), (905, 39, 47), (1000, 39, 47),     # Δ8 no heat
        (1200, 41, 37), (1205, 41, 47), (1250, 44, 47),   # Δ6 heat
    )
    fit = fit_reheat_differential(rows)
    assert fit["status"] == "inconsistent"
    assert "differential_c" not in fit


def test_reheat_differential_upward_restep_does_not_poison_the_episode():
    """Review case: warmup 47 lands in the deadband (Δ5, no heat), then a boost
    chain re-steps 47→60 + Powerful and the tank rises. The rise belongs to the
    BOOST — the Δ5 episode must end at the re-step, not read as 'Δ5 heated'."""
    from src.dhw.calibration import fit_reheat_differential
    rows = _step_series(
        (0, 42, 37), (5, 42, 47), (40, 42, 47),      # Δ5: 35 min observed, no heat
        (60, 42, 60), (90, 45, 60), (120, 50, 60),    # boost re-step + rise
        (300, 38, 37), (305, 38, 47), (350, 41, 47),  # Δ9 heats
        (600, 43, 37), (605, 43, 47), (700, 43, 47),  # Δ4 no heat
        (900, 39, 37), (905, 39, 47), (950, 42, 47),  # Δ8 heats
        (1200, 42, 37), (1205, 42, 47), (1300, 42, 47),  # Δ5 no heat
    )
    fit = fit_reheat_differential(rows)
    assert fit["status"] == "ok"
    eps = {(e["delta_c"], e["heated"]) for e in fit["episodes"]}
    assert (5.0, False) in eps and (5.0, True) not in eps
    assert fit["n_misclassified"] == 0


def test_reheat_differential_gap_guard_discards_stale_steps():
    """A step first observed after a long polling gap may already be mid-heat —
    Δ measured against the part-heated tank would mislabel low."""
    from src.dhw.calibration import fit_reheat_differential
    rows = _step_series(
        (0, 38, 37),                                   # last row before the gap
        (60, 42, 47), (100, 45, 47),                   # step seen 60 min later
    )
    fit = fit_reheat_differential(rows)
    assert fit["n_episodes"] == 0
    assert fit["n_discarded_draw"] >= 1


def test_reheat_differential_excludes_commanded_powerful_windows():
    """Guests-mode warmups command 47 WITH Powerful — they heat at any Δ and
    telemetry can't see the flag. HEM's own action windows exclude them."""
    from datetime import UTC, datetime, timedelta
    from src.dhw.calibration import fit_reheat_differential
    t0 = datetime(2026, 7, 10, 10, 0, tzinfo=UTC)
    rows = _step_series(
        (0, 42, 37), (5, 42, 47), (40, 45, 47),        # Δ5 "heated" — but Powerful
        (300, 38, 37), (305, 38, 47), (350, 41, 47),    # Δ9 heats
        (600, 43, 37), (605, 43, 47), (700, 43, 47),    # Δ4 no heat
    )
    pwin = [(t0, t0 + timedelta(minutes=50))]
    fit = fit_reheat_differential(rows, powerful_windows_utc=pwin)
    deltas = [e["delta_c"] for e in fit["episodes"]]
    assert 5.0 not in deltas and len(fit["episodes"]) == 2
