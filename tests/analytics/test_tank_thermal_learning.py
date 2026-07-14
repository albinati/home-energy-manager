"""DHW tank thermal learner: synthetic-UA recovery, decontamination, gates,
COP events, evening-draw energy balance, and the bounded readers.

Same discipline as the W2 building learner's tests: the fitters are PURE, so
these drive them with synthetic curves of KNOWN physics and assert recovery —
the fit is proven independently of whatever prod's telemetry happens to hold.
"""
from __future__ import annotations

import json
import math
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src import db
from src.analytics import tank_thermal_learning as ttl
from src.config import config

TZ = ZoneInfo("UTC")
C_TANK = 200.0 * 4186.0  # J/K — the 200 L tank
AMBIENT = 20.0


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    path = tmp_path / "t.db"
    monkeypatch.setenv("DB_PATH", str(path))
    monkeypatch.setattr(db, "_db_path", lambda: path)
    db.init_db()
    return path


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(config, "BULLETPROOF_TIMEZONE", "UTC", raising=False)
    monkeypatch.setattr(config, "DHW_TANK_LITRES", 200.0, raising=False)
    monkeypatch.setattr(config, "DHW_WATER_CP", 4186.0, raising=False)
    monkeypatch.setattr(config, "DHW_TANK_UA_W_PER_K", 2.5, raising=False)
    monkeypatch.setattr(config, "DHW_TANK_LEARNED_VALUES_ENABLED", True, raising=False)
    yield


# ---------------------------------------------------------------------------
# Synthetic builders
# ---------------------------------------------------------------------------


def decay_rows(
    start: datetime,
    *,
    hours: float,
    t0: float,
    ua_w_per_k: float,
    step_minutes: int = 45,
    ambient: float = AMBIENT,
) -> list[tuple[float, float]]:
    """Exact Newtonian cooling at a KNOWN UA: T(t) = amb + (t0-amb)·e^(−t/τ),
    τ = C/UA. Sampled at the ~45-min cadence the Daikin telemetry actually has."""
    tau_h = C_TANK / (ua_w_per_k * 3600.0)
    out: list[tuple[float, float]] = []
    n = int(hours * 60 / step_minutes) + 1
    for k in range(n):
        t_h = k * step_minutes / 60.0
        temp = ambient + (t0 - ambient) * math.exp(-t_h / tau_h)
        out.append(((start + timedelta(hours=t_h)).timestamp(), temp))
    return out


def bucket_row(day: date, idx: int, kwh_dhw: float | None) -> dict:
    return {"date": day.isoformat(), "bucket_idx": idx, "kwh_dhw": kwh_dhw,
            "kwh_heating": 0.0}


def quiet_buckets(day: date, kwh_by_idx: dict[int, float | None]) -> list[dict]:
    """A full 12-bucket day, quiet except where specified."""
    return [bucket_row(day, i, kwh_by_idx.get(i, 0.0)) for i in range(12)]


# ---------------------------------------------------------------------------
# UA fit — the headline: recover a known UA from a synthetic decay
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("true_ua", [2.5, 5.0, 9.0])
def test_fit_recovers_known_ua(true_ua):
    start = datetime(2026, 7, 8, 23, 0, tzinfo=UTC)
    rows = decay_rows(start, hours=8, t0=55.0, ua_w_per_k=true_ua)
    eps = ttl.select_tank_decay_episodes(
        rows, quiet_buckets(date(2026, 7, 8), {}) + quiet_buckets(date(2026, 7, 9), {}),
        [], tz=TZ, t_ambient_c=AMBIENT,
    )
    assert len(eps) == 1
    ua, r2 = ttl.fit_tank_ua_for_episode(eps[0], c_tank_j_per_k=C_TANK)
    assert ua == pytest.approx(true_ua, rel=0.05)
    assert r2 > 0.99


def test_prod_shaped_night_fits_the_measured_ua():
    """The real 2026-07-11 night: 53 °C → 50 °C over 10.5 h. That shape must fit
    to ~2 W/K — the value 19 clean prod episodes actually produce. Guards the
    calibration against drifting away from measured reality.

    (A crude 22:00→07:00 delta on the same night reads ~1.1 °C/h and would imply
    a far leakier tank; it is swallowing the setback transient and a late draw.
    Fitting EPISODES, not endpoints, is what keeps that out.)
    """
    start = datetime(2026, 7, 11, 23, 0, tzinfo=UTC)
    tau_h = 10.5 / math.log((53.0 - AMBIENT) / (50.0 - AMBIENT))
    ua_true = C_TANK / (tau_h * 3600.0)
    assert 1.5 < ua_true < 2.6  # the prod-measured band
    rows = decay_rows(start, hours=10.5, t0=53.0, ua_w_per_k=ua_true)
    eps = ttl.select_tank_decay_episodes(
        rows, quiet_buckets(date(2026, 7, 11), {}) + quiet_buckets(date(2026, 7, 12), {}),
        [], tz=TZ, t_ambient_c=AMBIENT,
    )
    ua, _ = ttl.fit_tank_ua_for_episode(eps[0], c_tank_j_per_k=C_TANK)
    assert ua == pytest.approx(ua_true, rel=0.05)


def test_survives_the_overnight_polling_hole():
    """The heartbeat stops asking Onecta ~00:30–05:00 to protect the Daikin
    quota, so a REAL clean night is: one sample at 23:50, a ~5.5 h hole, then
    hourly samples. A 90-minute gap tolerance rejects every such night (measured:
    0 episodes over 21 prod days). The tank's τ is ~100 h, so the trapezoid
    across the hole is nearly exact — tolerate it, and still recover the UA."""
    start = datetime(2026, 7, 8, 23, 50, tzinfo=UTC)
    full = decay_rows(start, hours=9, t0=45.0, ua_w_per_k=2.0, step_minutes=15)
    sparse = [full[0]] + [
        r for r in full
        if 5 <= datetime.fromtimestamp(r[0], tz=UTC).hour < 9
    ]
    assert (sparse[1][0] - sparse[0][0]) / 3600 > 5  # the hole is really there
    eps = ttl.select_tank_decay_episodes(
        sparse, quiet_buckets(date(2026, 7, 8), {}) + quiet_buckets(date(2026, 7, 9), {}),
        [], tz=TZ, t_ambient_c=AMBIENT,
    )
    assert len(eps) == 1
    ua, _ = ttl.fit_tank_ua_for_episode(eps[0], c_tank_j_per_k=C_TANK)
    assert ua == pytest.approx(2.0, rel=0.08)


def test_one_degree_quantisation_does_not_fake_a_draw_or_a_reheat():
    """Onecta reports whole degrees. A flat-ish tank therefore steps up and down
    by 1 °C on rounding alone — which must not read as a reheat (episode
    rejected) nor as a draw (episode split). Only a real, STEEP fall is a draw."""
    start = datetime(2026, 7, 8, 23, 0, tzinfo=UTC)
    rows = [
        (r[0], float(round(r[1])))
        for r in decay_rows(start, hours=9, t0=45.0, ua_w_per_k=2.0, step_minutes=60)
    ]
    eps = ttl.select_tank_decay_episodes(
        rows, quiet_buckets(date(2026, 7, 8), {}) + quiet_buckets(date(2026, 7, 9), {}),
        [], tz=TZ, t_ambient_c=AMBIENT,
    )
    assert len(eps) == 1
    assert len(eps[0].points) == len(rows)  # nothing split off
    ua, _ = ttl.fit_tank_ua_for_episode(eps[0], c_tank_j_per_k=C_TANK)
    assert ua == pytest.approx(2.0, rel=0.25)  # coarse data, still the right ballpark


def test_fit_tank_ua_median_and_skip_gate():
    start = datetime(2026, 7, 1, 23, 0, tzinfo=UTC)
    eps = []
    for k, ua in enumerate([4.0, 5.0, 6.0, 5.5, 4.5]):
        rows = decay_rows(start + timedelta(days=k), hours=8, t0=55.0, ua_w_per_k=ua)
        cons = quiet_buckets((start + timedelta(days=k)).date(), {}) + quiet_buckets(
            (start + timedelta(days=k + 1)).date(), {}
        )
        eps += ttl.select_tank_decay_episodes(rows, cons, [], tz=TZ, t_ambient_c=AMBIENT)
    assert len(eps) == 5
    fit = ttl.fit_tank_ua(eps, c_tank_j_per_k=C_TANK)
    assert fit["status"] == "ok"
    assert fit["ua_w_per_k"] == pytest.approx(5.0, rel=0.05)
    assert fit["tau_hours"] == pytest.approx(C_TANK / (fit["ua_w_per_k"] * 3600.0), rel=1e-6)
    # Below the episode gate the learner skips rather than guessing.
    assert ttl.fit_tank_ua(eps[:2], c_tank_j_per_k=C_TANK)["status"] == "skipped"


# ---------------------------------------------------------------------------
# Decontamination
# ---------------------------------------------------------------------------


def test_reheat_bucket_blocks_its_span_but_keeps_the_clean_stretch_before_it():
    """A 04:00 negative-price boost must not cost us the clean 23:00–04:00
    decay — the W2 lesson, re-applied to the tank."""
    day = date(2026, 7, 8)
    start = datetime(2026, 7, 8, 23, 0, tzinfo=UTC)
    rows = decay_rows(start, hours=9, t0=55.0, ua_w_per_k=5.0)
    cons = quiet_buckets(day, {}) + quiet_buckets(day + timedelta(days=1), {2: 0.9})
    eps = ttl.select_tank_decay_episodes(rows, cons, [], tz=TZ, t_ambient_c=AMBIENT)
    assert len(eps) == 1
    # The kept stretch ends before the 04:00–06:00 bucket opens.
    assert eps[0].end_utc <= datetime(2026, 7, 9, 4, 0, tzinfo=UTC)
    assert (eps[0].end_utc - eps[0].start_utc) >= timedelta(hours=4)
    ua, _ = ttl.fit_tank_ua_for_episode(eps[0], c_tank_j_per_k=C_TANK)
    assert ua == pytest.approx(5.0, rel=0.06)


def test_a_draw_breaks_the_episode_and_does_not_inflate_ua():
    """A late-night draw is a cliff, not decay. Fitting through it would read
    as a hugely lossy tank — exactly the failure mode that would make the LP
    over-heat. The clean stretch before survives; the cliff is excluded."""
    day = date(2026, 7, 8)
    start = datetime(2026, 7, 8, 23, 0, tzinfo=UTC)
    rows = decay_rows(start, hours=5, t0=55.0, ua_w_per_k=5.0)
    # 04:00: someone runs a bath — 6 °C off the tank in one sample.
    cliff_start = rows[-1][0] + 45 * 60
    rows.append((cliff_start, rows[-1][1] - 6.0))
    rows += decay_rows(
        datetime.fromtimestamp(cliff_start + 45 * 60, tz=UTC),
        hours=3, t0=rows[-1][1] - 0.2, ua_w_per_k=5.0,
    )
    cons = quiet_buckets(day, {}) + quiet_buckets(day + timedelta(days=1), {})
    eps = ttl.select_tank_decay_episodes(rows, cons, [], tz=TZ, t_ambient_c=AMBIENT)
    assert eps, "the clean pre-draw stretch must survive"
    for ep in eps:
        ua, _ = ttl.fit_tank_ua_for_episode(ep, c_tank_j_per_k=C_TANK)
        assert ua == pytest.approx(5.0, rel=0.15)


def test_a_draw_hidden_inside_the_polling_hole_does_not_inflate_ua():
    """The one contaminant the energy buckets CANNOT see: drawing hot water burns
    no electricity, so a 3 a.m. draw leaves no trace in the counter. Across a 6 h
    gap the rate test is toothless (it would need a ~12 °C fall to fire) while the
    honest decay is only ~3 °C — so a quiet night-time draw would sail through and
    DOUBLE the fitted UA. The plausibility gate against the prior physics is what
    catches it."""
    start = datetime(2026, 7, 8, 23, 0, tzinfo=UTC)
    before = decay_rows(start, hours=2, t0=45.0, ua_w_per_k=2.0, step_minutes=30)
    # 6 h hole. Someone drew a bath at 03:00: the tank reappears 5 °C down, when
    # honest coasting over the hole would only have cost ~1.5 °C.
    resume = datetime(2026, 7, 9, 7, 0, tzinfo=UTC)
    after = decay_rows(resume, hours=2.5, t0=39.5, ua_w_per_k=2.0, step_minutes=30)
    cons = quiet_buckets(date(2026, 7, 8), {}) + quiet_buckets(date(2026, 7, 9), {})

    eps = ttl.select_tank_decay_episodes(
        before + after, cons, [], tz=TZ, t_ambient_c=AMBIENT, ua_prior_w_per_k=2.5,
        min_episode_hours=1.0, min_points=3,
    )
    # The step across the hole is rejected, so the night splits in two — and
    # crucially NO episode spans the draw.
    assert eps, "the clean stretches must survive (else this asserts nothing)"
    for ep in eps:
        # No surviving episode spans the hole the draw was hidden in.
        assert ep.end_utc <= start + timedelta(hours=2) or ep.start_utc >= resume
        fit = ttl.fit_tank_ua_for_episode(ep, c_tank_j_per_k=C_TANK)
        if fit is not None:
            assert fit[0] == pytest.approx(2.0, rel=0.35)

    # Sanity: without the gate, the same night reads as a far leakier tank.
    naive = ttl.select_tank_decay_episodes(
        before + after, cons, [], tz=TZ, t_ambient_c=AMBIENT,
        gap_drop_tolerance=99.0, min_episode_hours=1.0, min_points=3,
    )
    spanning = [e for e in naive if e.start_utc < resume < e.end_utc]
    assert spanning, "the naive selector should span the hole (else this asserts nothing)"
    ua_naive, _ = ttl.fit_tank_ua_for_episode(spanning[0], c_tank_j_per_k=C_TANK)
    assert ua_naive > 3.5  # the fake leak


def test_bucket_windows_are_local_and_dst_safe():
    """The 2 h buckets are LOCAL; the telemetry is UTC epoch. Prod runs
    Europe/London, so half the year carries a +1 offset, and two days a year a
    '2 h' bucket is 1 or 3 real hours. `_bucket_window_utc` is correct today by
    virtue of wall-clock arithmetic on an aware datetime — pin that down, because
    a refactor to plain UTC deltas would break it silently."""
    london = ZoneInfo("Europe/London")

    # BST: local 12:00–14:00 is 11:00–13:00 UTC.
    s, e = ttl._bucket_window_utc(date(2026, 7, 8), 6, london)
    assert (s.hour, e.hour) == (11, 13)

    # GMT: local 12:00–14:00 is 12:00–14:00 UTC.
    s, e = ttl._bucket_window_utc(date(2026, 1, 8), 6, london)
    assert (s.hour, e.hour) == (12, 14)

    # Spring forward (2026-03-29): the 00:00–02:00 local bucket is ONE real hour.
    s, e = ttl._bucket_window_utc(date(2026, 3, 29), 0, london)
    assert (e - s) == timedelta(hours=1)

    # Fall back (2026-10-25): the same bucket is THREE real hours.
    s, e = ttl._bucket_window_utc(date(2026, 10, 25), 0, london)
    assert (e - s) == timedelta(hours=3)


def test_boost_window_excludes_the_night():
    day = date(2026, 7, 8)
    start = datetime(2026, 7, 8, 23, 0, tzinfo=UTC)
    rows = decay_rows(start, hours=8, t0=55.0, ua_w_per_k=5.0)
    boosts = [("2026-07-08T22:00:00Z", "2026-07-09T07:00:00Z")]
    eps = ttl.select_tank_decay_episodes(
        rows, quiet_buckets(day, {}) + quiet_buckets(day + timedelta(days=1), {}),
        boosts, tz=TZ, t_ambient_c=AMBIENT,
    )
    assert eps == []


def test_warm_tank_is_skipped_not_guessed():
    """ΔT(tank − room) below the gate: no identifiable signal, so no episode.
    The honest limitation, stated rather than papered over."""
    start = datetime(2026, 7, 8, 23, 0, tzinfo=UTC)
    rows = decay_rows(start, hours=8, t0=25.0, ua_w_per_k=5.0)  # only 5 K above ambient
    eps = ttl.select_tank_decay_episodes(
        rows, quiet_buckets(date(2026, 7, 8), {}), [], tz=TZ, t_ambient_c=AMBIENT,
    )
    assert eps == []


def test_legionella_window_is_excluded():
    """Sunday 11:00 UTC: the firmware owns the tank. Nothing learned there."""
    sunday = date(2026, 7, 12)  # a Sunday
    start = datetime(2026, 7, 12, 11, 0, tzinfo=UTC)
    rows = decay_rows(start, hours=8, t0=60.0, ua_w_per_k=5.0)
    eps = ttl.select_tank_decay_episodes(
        rows, quiet_buckets(sunday, {}), [], tz=TZ, t_ambient_c=AMBIENT,
        night_start_hour_local=10, night_end_hour_local=23,  # force the window open
        legionella={"dow": 6, "start_hour_utc": 11, "start_minute_utc": 0,
                    "duration_minutes": 120},
    )
    # The 11:00–13:00 stand-off is carved out; whatever survives starts after it.
    assert eps, "the post-standoff stretch must survive (else this asserts nothing)"
    for ep in eps:
        assert ep.start_utc >= datetime(2026, 7, 12, 13, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# COP events
# ---------------------------------------------------------------------------


def _heat_rows(start: datetime, *, t0: float, rise: float, hours: float,
               step_minutes: int = 30) -> list[tuple[float, float]]:
    n = int(hours * 60 / step_minutes) + 1
    return [
        ((start + timedelta(minutes=k * step_minutes)).timestamp(),
         t0 + rise * (k / (n - 1)))
        for k in range(n)
    ]


def _cop_day(day: date, *, cop: float, ua: float, rise: float = 8.0
             ) -> tuple[list[tuple[float, float]], list[dict], list[tuple[datetime, float]]]:
    """One clean warmup event on `day`: bucket 6 (12:00–14:00), quiet neighbours,
    with the electric energy that a tank of this COP would actually have used."""
    start = datetime(day.year, day.month, day.day, 12, 0, tzinfo=UTC)
    rows = _heat_rows(start, t0=37.0, rise=rise, hours=2.0)
    pts = [((datetime.fromtimestamp(e, tz=UTC) - start).total_seconds() / 3600.0, v)
           for e, v in rows]
    thermal_j = C_TANK * rise + ttl._standing_loss_j(
        pts, ua_w_per_k=ua, t_ambient_c=AMBIENT
    )
    kwh = (thermal_j / 3.6e6) / cop
    outdoor = [(start + timedelta(minutes=30 * k), 15.0) for k in range(6)]
    return rows, quiet_buckets(day, {6: kwh}), outdoor


def test_cop_fit_recovers_a_known_cop_from_distinct_events():
    """Feed four INDEPENDENT warmups whose electric energy implies COP ≈ 2.5 and
    expect 2.5 back — plus the curve ratio the LP will actually be corrected by.

    Distinct events, not one event replicated: replication would pass the sample
    gate while testing neither the aggregation nor the median's robustness.
    """
    ua = 2.0
    rows: list[tuple[float, float]] = []
    cons: list[dict] = []
    outdoor: list[tuple[datetime, float]] = []
    for k, cop in enumerate([2.2, 2.4, 2.5, 2.6, 2.8]):
        r, c, o = _cop_day(date(2026, 7, 6) + timedelta(days=2 * k), cop=cop, ua=ua)
        rows += r
        cons += c
        outdoor += o
    events = ttl.select_tank_heat_events(rows, cons, [], outdoor, tz=TZ)
    assert len(events) == 5

    fit = ttl.fit_dhw_cop(
        events, c_tank_j_per_k=C_TANK, ua_w_per_k=ua, t_ambient_c=AMBIENT,
        min_samples=4,
    )
    assert fit["status"] == "ok"
    assert fit["cop_median"] == pytest.approx(2.5, rel=0.03)
    assert fit["cop_p25"] <= fit["cop_median"] <= fit["cop_p75"]
    assert fit["cop_p75"] > fit["cop_p25"]  # the spread is real, not collapsed
    assert fit["cop_t_outdoor_median"] == pytest.approx(15.0)

    expected_mult = 2.5 / ttl.modelled_cop_dhw(15.0)
    assert fit["cop_mult"] == pytest.approx(max(0.5, min(1.5, expected_mult)), rel=0.03)
    # The curve is a SPACE-heating curve: at 15 °C it claims a DHW COP the tank
    # cannot deliver, so the honest correction is a big one. If this ever stops
    # being true, the multiplier's floor needs revisiting, not silently clamping.
    assert ttl.modelled_cop_dhw(15.0) > 4.0
    assert expected_mult < 0.7


def test_cop_fit_needs_outdoor_coverage_to_form_the_ratio():
    ua = 2.0
    rows, cons, _ = _cop_day(date(2026, 7, 8), cop=2.5, ua=ua)
    events = ttl.select_tank_heat_events(rows, cons, [], [], tz=TZ)
    fit = ttl.fit_dhw_cop(
        events * 4, c_tank_j_per_k=C_TANK, ua_w_per_k=ua, t_ambient_c=AMBIENT,
        min_samples=4,
    )
    # The LEVEL is measurable without weather; the curve ratio is not.
    assert fit["cop_median"] == pytest.approx(2.5, rel=0.03)
    assert fit["status"] == "skipped"


def test_the_cop_sample_gate_is_reachable_at_the_observed_event_rate():
    """Prod yields roughly one clean heat event every five days. A gate the real
    data can never satisfy makes the component dead code — and the LP would keep
    costing tank heat at half price while the table sat empty, looking healthy."""
    window = config.DHW_TANK_LEARN_COP_WINDOW_DAYS
    gate = config.DHW_TANK_LEARN_COP_MIN_SAMPLES
    observed_events_per_day = 4 / 21  # measured over 21 prod days
    assert window * observed_events_per_day >= gate


def test_heat_event_needs_known_quiet_neighbours():
    """A MISSING neighbouring bucket is not a quiet one — energy could have
    spilled in from it, so the pairing is unsafe and the event is dropped."""
    day = date(2026, 7, 8)
    start = datetime(2026, 7, 8, 12, 0, tzinfo=UTC)
    rows = _heat_rows(start, t0=37.0, rise=8.0, hours=2.0)
    cons = [bucket_row(day, i, 0.0) for i in range(12) if i != 5]  # bucket 5 absent
    cons = [r if r["bucket_idx"] != 6 else bucket_row(day, 6, 0.8) for r in cons]
    assert ttl.select_tank_heat_events(rows, cons, [], [], tz=TZ) == []

    # A NULL counter is likewise unknown, not zero.
    cons2 = quiet_buckets(day, {5: None, 6: 0.8})
    assert ttl.select_tank_heat_events(rows, cons2, [], [], tz=TZ) == []


def test_heat_event_with_a_mid_reheat_draw_is_rejected():
    """A drop inside the run means a draw overlapped the reheat: the energy
    covers heat the thermometer never showed, so fitting it would read the COP
    low. Reject instead."""
    day = date(2026, 7, 8)
    start = datetime(2026, 7, 8, 12, 0, tzinfo=UTC)
    rows = _heat_rows(start, t0=37.0, rise=8.0, hours=2.0)
    rows[2] = (rows[2][0], rows[2][1] - 3.0)  # someone drew hot water mid-warmup
    cons = quiet_buckets(day, {6: 0.8})
    assert ttl.select_tank_heat_events(rows, cons, [], [], tz=TZ) == []


# ---------------------------------------------------------------------------
# Evening draw (energy balance)
# ---------------------------------------------------------------------------


def _closed_days(
    n_days: int,
    *,
    base: float = 50.0,
    drops_for_day=lambda k: {},
    reheat_hour: int = 13,   # ODD: lands INSIDE bucket 6, not on its 12:00 edge
    ua_w_per_k: float = 2.0,
    cop: float = 2.5,
    start_day: date = date(2026, 7, 6),
    extra_rise: dict[int, float] | None = None,
) -> tuple[list[tuple[float, float]], list[dict]]:
    """A CONTINUOUS, energy-closed multi-day tank series + its 2h energy buckets.

    Every joule is accounted: the tank coasts at ``ua_w_per_k``, loses ``drops``
    °C to draws, and is topped back up to ``base`` once a day by a reheat whose
    electricity is booked into the matching bucket at ``cop``.

    ``reheat_hour`` must be the SECOND hour of its 2h bucket: a rise applied on a
    bucket boundary is invisible inside that bucket (its start sample already
    carries it) and shows up as a rise-without-energy in the bucket before.

    Getting this right is the difference between testing the estimator and testing
    the fixture. A per-day builder that restarts each morning at ``base`` quietly
    lifts the tank with no energy behind it, and the balance — correctly — books
    that phantom heat as a negative residual, which then cancels the very draws
    the test is asserting. The estimator was right; the fixture was lying.
    """
    rows: list[tuple[float, float]] = []
    cons: list[dict] = []
    extra_rise = extra_rise or {}
    cur = base

    def _ts(day: date, hour: int) -> float:
        return (
            datetime(day.year, day.month, day.day, 0, 0, tzinfo=UTC)
            + timedelta(hours=hour)
        ).timestamp()

    for k in range(n_days):
        day = start_day + timedelta(days=k)
        drops = drops_for_day(k)
        reheat_kwh = 0.0
        for h in range(24):
            if h:
                cur += -(ua_w_per_k * (cur - AMBIENT) * 3600.0) / C_TANK  # coast 1 h
            cur -= drops.get(h, 0.0)
            if h in extra_rise:
                cur += extra_rise[h]
            if h == reheat_hour:
                rise = max(0.0, base - cur)
                cur += rise
                reheat_kwh = (rise * C_TANK / 3.6e6) / cop
            rows.append((_ts(day, h), cur))
        booked = dict.fromkeys(range(12), 0.0)
        booked[reheat_hour // 2] = reheat_kwh
        for h, r in extra_rise.items():
            booked[h // 2] = booked.get(h // 2, 0.0) + (r * C_TANK / 3.6e6) / cop
        cons += [bucket_row(day, b, booked[b]) for b in range(12)]
    rows.append((_ts(start_day + timedelta(days=n_days), 0), cur))
    return rows, cons


def test_draw_profile_adds_back_the_mid_drawdown_reheat():
    """The tank's temperature drop UNDERSTATES the draw whenever the firmware
    reheats mid-shower — and that reheat (prod: the 20:00-22:00 bucket, at peak
    price) is exactly the prize the LP is being asked to move. The energy balance
    must recover the full draw, not the visible drop."""
    cop = 2.5
    extra_reheat_c = 4.0  # the firmware tops the tank up DURING the drawdown
    rows, cons = _closed_days(
        8,
        drops_for_day=lambda k: {21: 8.0},
        extra_rise={21: extra_reheat_c},
        cop=cop,
    )
    fit = ttl.estimate_draw_profile(
        rows, cons, [], tz=TZ, c_tank_j_per_k=C_TANK, ua_w_per_k=2.0,
        t_ambient_c=AMBIENT, cop_dhw=cop, min_days=5,
    )
    assert fit["status"] == "ok"
    evening = fit["profile_kwh_median"][10]
    visible_drop_kwh = C_TANK * (8.0 - extra_reheat_c) / 3.6e6
    assert evening > visible_drop_kwh  # the reheat is added back
    assert evening == pytest.approx(C_TANK * 8.0 / 3.6e6, abs=0.2)  # the FULL draw
    # A quiet bucket stays quiet — standing loss is not mistaken for demand.
    assert fit["profile_kwh_median"][2] == pytest.approx(0.0, abs=0.06)


def test_draw_profile_finds_a_morning_draw_an_evening_window_would_miss():
    """This household's tank visibly drops around 08:30-09:30 while most evenings
    barely move it. An evening-window estimator would measure a small number and
    call it the answer; the per-bucket profile puts the draw where it happens —
    which is what the LP needs in order to time the tank at all."""
    rows, cons = _closed_days(8, drops_for_day=lambda k: {9: 6.0})
    fit = ttl.estimate_draw_profile(
        rows, cons, [], tz=TZ, c_tank_j_per_k=C_TANK, ua_w_per_k=2.0,
        t_ambient_c=AMBIENT, cop_dhw=2.5, min_days=5,
    )
    assert fit["status"] == "ok"
    profile = fit["profile_kwh_median"]
    assert profile[4] == pytest.approx(C_TANK * 6.0 / 3.6e6, abs=0.2)  # 08:00-10:00
    assert profile[10] == pytest.approx(0.0, abs=0.06)  # evening: nothing
    assert profile.index(max(profile)) == 4


def test_daily_total_is_not_the_sum_of_bucket_percentiles():
    """Sum of per-bucket p75s is an UPPER BOUND on a day's p75, not an estimate of
    it — no day sits at its 75th percentile in all twelve buckets at once. The
    daily stat must come from whole-day totals."""
    # Anti-correlated: a big-shower morning is followed by a light evening. Each
    # bucket has a high p75, yet no day is high in both — exactly where summing
    # the percentiles overshoots.
    rows, cons = _closed_days(
        8,
        base=55.0,
        drops_for_day=lambda k: {9: 8.0 if k % 2 else 2.0, 21: 2.0 if k % 2 else 8.0},
    )
    fit = ttl.estimate_draw_profile(
        rows, cons, [], tz=TZ, c_tank_j_per_k=C_TANK, ua_w_per_k=2.0,
        t_ambient_c=AMBIENT, cop_dhw=2.5, min_days=5,
    )
    assert fit["status"] == "ok"
    assert fit["daily_full_days"] == 8
    sum_of_p75 = sum(fit["profile_kwh_p75"])
    # Every day drew the same TOTAL (8 + 2 degrees), so the daily p75 is that
    # total — while the summed percentiles claim 8 + 8.
    assert fit["daily_kwh_p75"] == pytest.approx(C_TANK * 10.0 / 3.6e6, abs=0.35)
    assert fit["daily_kwh_p75"] < sum_of_p75


def test_a_pure_reheat_reports_no_draw_and_flags_its_cop_dependence():
    """A bucket whose entire temperature RISE is explained by its own reheat drew
    no water — and the estimator must say so rather than booking the reheat as
    demand. But that answer is only as good as the assumed COP: get the COP wrong
    and the same bucket reports a phantom draw. The dependence is real and
    unavoidable at 2 h resolution, so it is PUBLISHED (`profile_cop_sensitivity_kwh`
    = kWh the answer moves per unit of COP error) rather than hidden inside a
    number that looks as solid as the morning draw — which is read straight off the
    tank's fall and is COP-invariant."""
    cop = 2.5
    rows, cons = _closed_days(8, drops_for_day=lambda k: {9: 6.0}, cop=cop)
    fit = ttl.estimate_draw_profile(
        rows, cons, [], tz=TZ, c_tank_j_per_k=C_TANK, ua_w_per_k=2.0,
        t_ambient_c=AMBIENT, cop_dhw=cop, min_days=5,
    )
    assert fit["status"] == "ok"
    # Bucket 6 (12:00-14:00) is pure reheat: no draw is booked against it.
    assert fit["profile_kwh_median"][6] == pytest.approx(0.0, abs=0.1)
    # ...and its answer is the COP-dependent one; the morning draw's is not.
    assert fit["profile_cop_sensitivity_kwh"][6] > 0.2
    assert fit["profile_cop_sensitivity_kwh"][4] == pytest.approx(0.0, abs=0.01)

    # Prove the dependence is real: assume a COP 40% too high and the SAME data
    # reports a reheat-bucket draw that never happened, while the morning draw
    # barely moves. This asymmetry is why the sensitivity ships.
    inflated = ttl.estimate_draw_profile(
        rows, cons, [], tz=TZ, c_tank_j_per_k=C_TANK, ua_w_per_k=2.0,
        t_ambient_c=AMBIENT, cop_dhw=cop * 1.4, min_days=5,
    )
    assert inflated["profile_kwh_median"][6] > 0.3
    assert inflated["profile_kwh_median"][4] == pytest.approx(
        fit["profile_kwh_median"][4], abs=0.06
    )


def test_negative_residuals_are_not_rectified_into_phantom_draw():
    """A reheat's heat can land just after the bucket edge: the bucket holding the
    energy shows no rise (positive residual) while its neighbour shows a rise with
    no energy (negative residual). Clamping EACH observation at zero would keep the
    phantom and throw the correction away, inflating the profile. Clamp once, at
    publication — the errors have to be allowed to cancel first."""
    cop = 2.5
    # Reheat happens at 15:00 (bucket 7) but the counter books it to bucket 6.
    # Nobody drew any water all day.
    rows, cons = _closed_days(8, reheat_hour=15, cop=cop)
    spilled = {
        r["date"]: r["kwh_dhw"] for r in cons if r["bucket_idx"] == 7
    }
    for row in cons:
        if row["bucket_idx"] == 6:
            row["kwh_dhw"] = spilled[row["date"]]
        elif row["bucket_idx"] == 7:
            row["kwh_dhw"] = 0.0

    fit = ttl.estimate_draw_profile(
        rows, cons, [], tz=TZ, c_tank_j_per_k=C_TANK, ua_w_per_k=2.0,
        t_ambient_c=AMBIENT, cop_dhw=cop, min_days=5,
    )
    assert fit["status"] == "ok"
    # Bucket 6 books a phantom (energy, no rise); bucket 7 carries the compensating
    # negative (rise, no energy). Across the DAY they must net to ~zero.
    assert fit["profile_kwh_median"][6] > 0.3   # the phantom is real, unclamped
    assert fit["daily_kwh_median"] == pytest.approx(0.0, abs=0.35)


def test_stale_calibration_falls_back_rather_than_steering_a_new_season(tmp_db, monkeypatch):
    """The merge-preserving upsert keeps a component forever if it never re-fits.
    A cop_mult measured in July, at 21-33 °C outdoors, must not still be steering
    a January LP because no clean heat event has been seen since."""
    old = (datetime.now(UTC) - timedelta(days=120)).isoformat()
    db.upsert_dhw_tank_calibration({
        "ua_w_per_k": 2.04, "ua_computed_at": old,
        "cop_mult": 0.55, "cop_computed_at": old,
    })
    monkeypatch.setattr(config, "DHW_TANK_LEARN_MAX_AGE_DAYS", 45.0, raising=False)
    assert ttl.get_tank_ua_w_per_k() == pytest.approx(2.5)   # env constant
    assert ttl.get_dhw_cop_multiplier() == pytest.approx(1.0)  # the curve, uncorrected

    # Fresh again → the learned values come straight back.
    db.upsert_dhw_tank_calibration({
        "ua_computed_at": datetime.now(UTC).isoformat(),
        "cop_computed_at": datetime.now(UTC).isoformat(),
    })
    assert ttl.get_tank_ua_w_per_k() == pytest.approx(2.04)
    assert ttl.get_dhw_cop_multiplier() == pytest.approx(0.55)


def test_draw_profile_gates_on_days_and_skips_null_buckets():
    """Too little data → skip, don't guess. And a NULL counter is UNKNOWN, not
    zero: that bucket contributes nothing and its day is not a whole day."""
    rows, cons = _closed_days(2)  # only 2 days
    fit = ttl.estimate_draw_profile(
        rows, cons, [], tz=TZ, c_tank_j_per_k=C_TANK, ua_w_per_k=2.0,
        t_ambient_c=AMBIENT, cop_dhw=2.5, min_days=5,
    )
    assert fit["status"] == "skipped"

    rows, cons = _closed_days(8)
    for row in cons:
        if row["bucket_idx"] == 5:
            row["kwh_dhw"] = None
    fit = ttl.estimate_draw_profile(
        rows, cons, [], tz=TZ, c_tank_j_per_k=C_TANK, ua_w_per_k=2.0,
        t_ambient_c=AMBIENT, cop_dhw=2.5, min_days=5,
    )
    assert fit["status"] == "ok"
    assert fit["samples_per_bucket"][5] == 0
    assert fit["daily_full_days"] == 0  # no whole day → no daily figure published


# ---------------------------------------------------------------------------
# Persistence, merge semantics, readers
# ---------------------------------------------------------------------------


def test_readers_fall_back_to_env_when_unlearned(tmp_db):
    assert ttl.get_tank_ua_w_per_k() == pytest.approx(2.5)
    assert ttl.get_dhw_cop_multiplier() == pytest.approx(1.0)
    assert ttl.get_draw_profile_kwh_thermal() is None
    assert ttl.get_daily_draw_kwh_thermal() is None


def test_readers_use_learned_values_and_reject_out_of_bounds(tmp_db, monkeypatch):
    profile = [0.0] * 12
    profile[4] = 0.5
    profile[10] = 0.9
    db.upsert_dhw_tank_calibration({
        "ua_w_per_k": 6.2, "cop_mult": 0.85,
        "draw_profile_p75_json": json.dumps(profile),
        "draw_daily_kwh_p75": 1.4,
    })
    assert ttl.get_tank_ua_w_per_k() == pytest.approx(6.2)
    assert ttl.get_dhw_cop_multiplier() == pytest.approx(0.85)
    assert ttl.get_draw_profile_kwh_thermal() == pytest.approx(profile)
    assert ttl.get_daily_draw_kwh_thermal() == pytest.approx(1.4)

    # A physically absurd row must not reach the LP — bounds, then env.
    db.upsert_dhw_tank_calibration({
        "ua_w_per_k": 900.0, "cop_mult": 12.0,
        "draw_profile_p75_json": json.dumps([99.0] * 12),
        "draw_daily_kwh_p75": 99.0,
    })
    assert ttl.get_tank_ua_w_per_k() == pytest.approx(2.5)
    assert ttl.get_dhw_cop_multiplier() == pytest.approx(1.0)
    assert ttl.get_draw_profile_kwh_thermal() is None
    assert ttl.get_daily_draw_kwh_thermal() is None

    # The kill switch returns every reader to the env constants.
    db.upsert_dhw_tank_calibration({"ua_w_per_k": 6.2, "cop_mult": 0.85})
    monkeypatch.setattr(config, "DHW_TANK_LEARNED_VALUES_ENABLED", False, raising=False)
    assert ttl.get_tank_ua_w_per_k() == pytest.approx(2.5)
    assert ttl.get_dhw_cop_multiplier() == pytest.approx(1.0)


def test_refresh_is_a_quiet_noop_without_telemetry(tmp_db):
    result = ttl.refresh_tank_thermal_calibration()
    assert result["status"] == "skipped"
    # Readers still work — they just answer with the env constants.
    assert ttl.get_tank_ua_w_per_k() == pytest.approx(2.5)


def test_a_telemetry_outage_must_not_erase_the_calibration(tmp_db):
    """The dangerous path: a learned row EXISTS and then the telemetry dries up
    (Onecta re-linked, API down, quota burnt). A row-replacing write would null
    every column and silently drop the LP back onto the env constants the learner
    was built to replace — a policy change nobody asked for, arriving overnight."""
    db.upsert_dhw_tank_calibration({
        "ua_w_per_k": 2.04, "cop_mult": 0.55, "cop_dhw_median": 2.59,
        "cop_samples": 4, "draw_profile_p75_json": json.dumps([0.1] * 12),
    })
    result = ttl.refresh_tank_thermal_calibration()  # no telemetry in this DB
    assert result["status"] == "skipped"

    row = db.get_dhw_tank_calibration()
    assert row["ua_w_per_k"] == pytest.approx(2.04)
    assert row["cop_mult"] == pytest.approx(0.55)
    assert row["cop_samples"] == 4
    assert ttl.get_tank_ua_w_per_k() == pytest.approx(2.04)
    assert ttl.get_dhw_cop_multiplier() == pytest.approx(0.55)


def test_partial_upsert_preserves_untouched_columns(tmp_db):
    """A partial write is a MERGE, not a replace: the three components land on
    different schedules and any of them can skip for a week."""
    db.upsert_dhw_tank_calibration({"ua_w_per_k": 2.0, "cop_mult": 0.55})
    db.upsert_dhw_tank_calibration({"ua_w_per_k": 2.2})  # UA re-fit only
    row = db.get_dhw_tank_calibration()
    assert row["ua_w_per_k"] == pytest.approx(2.2)
    assert row["cop_mult"] == pytest.approx(0.55)  # survived


def test_refresh_learns_ua_from_db_and_preserves_skipped_components(tmp_db, monkeypatch):
    """End-to-end through the DB: five clean nights → a learned UA. The COP and
    draw fits skip (no heat events, no showers), and a PRIOR value for them
    must survive the merge — one quiet week cannot erase a good fit."""
    db.upsert_dhw_tank_calibration({"cop_mult": 0.9, "cop_dhw_median": 2.4,
                                    "cop_samples": 10})
    now = datetime.now(UTC)
    conn = db.get_connection()
    try:
        for k in range(6):
            night = (now - timedelta(days=k + 1)).replace(
                hour=23, minute=0, second=0, microsecond=0
            )
            for epoch, temp in decay_rows(night, hours=8, t0=55.0, ua_w_per_k=6.0):
                conn.execute(
                    "INSERT INTO daikin_telemetry (fetched_at, source, tank_temp_c) "
                    "VALUES (?, 'live', ?)",
                    (epoch, temp),
                )
            for b in range(12):
                for d in (night.date(), night.date() + timedelta(days=1)):
                    conn.execute(
                        "INSERT OR IGNORE INTO daikin_consumption_2hourly "
                        "(date, bucket_idx, kwh_total, kwh_heating, kwh_dhw) "
                        "VALUES (?, ?, 0, 0, 0)",
                        (d.isoformat(), b),
                    )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setattr(config, "INDOOR_SETPOINT_C", AMBIENT, raising=False)
    result = ttl.refresh_tank_thermal_calibration()
    assert result["status"] == "ok"
    assert result["ua"]["status"] == "ok"
    assert ttl.get_tank_ua_w_per_k() == pytest.approx(6.0, rel=0.08)
    # The skipped components kept their prior values.
    assert ttl.get_dhw_cop_multiplier() == pytest.approx(0.9)
    row = db.get_dhw_tank_calibration()
    assert row["cop_samples"] == 10
    assert row["last_run"]["ua"]["status"] == "ok"
