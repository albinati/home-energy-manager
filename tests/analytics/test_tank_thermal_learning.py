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


def _full_day_rows(day: date, temps_by_hour: dict[int, float], base: float) -> list[tuple[float, float]]:
    """Hourly tank samples across a whole local day (= UTC in these tests)."""
    rows = []
    cur = base
    for h in range(25):
        cur = temps_by_hour.get(h, cur)
        ts = datetime(day.year, day.month, day.day, 0, 0, tzinfo=UTC) + timedelta(hours=h)
        rows.append((ts.timestamp(), cur))
    return rows


def test_draw_profile_adds_back_the_mid_drawdown_reheat():
    """The tank's temperature drop UNDERSTATES the draw whenever the firmware
    reheats mid-shower — and that reheat (prod: the 20:00–22:00 bucket, at peak
    price) is exactly the prize the LP is being asked to move. The energy balance
    must recover the full draw, not the visible drop."""
    ua, cop = 2.0, 2.5
    reheat_kwh = 0.4
    rows: list[tuple[float, float]] = []
    cons: list[dict] = []
    for k in range(8):
        day = date(2026, 7, 6) + timedelta(days=k)
        # Flat at 45 all day; the 20:00-22:00 bucket drops 4 °C AND takes a reheat.
        rows += _full_day_rows(day, {20: 45.0, 21: 43.0, 22: 41.0}, 45.0)
        cons += quiet_buckets(day, {10: reheat_kwh})
    fit = ttl.estimate_draw_profile(
        rows, cons, [], tz=TZ, c_tank_j_per_k=C_TANK, ua_w_per_k=ua,
        t_ambient_c=AMBIENT, cop_dhw=cop, min_days=5,
    )
    assert fit["status"] == "ok"
    visible_drop_kwh = C_TANK * 4.0 / 3.6e6
    evening = fit["profile_kwh_median"][10]
    # The draw exceeds the visible drop by roughly the reheat's thermal output.
    assert evening > visible_drop_kwh
    assert evening == pytest.approx(visible_drop_kwh + reheat_kwh * cop, abs=0.15)
    # Quiet buckets stay quiet — the balance doesn't invent draw out of standing loss.
    assert fit["profile_kwh_median"][2] == pytest.approx(0.0, abs=0.05)


def test_draw_profile_finds_a_morning_draw_an_evening_window_would_miss():
    """This household's tank visibly drops around 08:30–09:30 while most evenings
    barely move it. An evening-window estimator would measure a small number and
    call it the answer; the per-bucket profile puts the draw where it happens —
    which is what the LP needs in order to time the tank at all."""
    rows: list[tuple[float, float]] = []
    cons: list[dict] = []
    for k in range(8):
        day = date(2026, 7, 6) + timedelta(days=k)
        rows += _full_day_rows(day, {8: 44.0, 9: 40.0, 10: 38.0}, 44.0)
        cons += quiet_buckets(day, {})
    fit = ttl.estimate_draw_profile(
        rows, cons, [], tz=TZ, c_tank_j_per_k=C_TANK, ua_w_per_k=2.0,
        t_ambient_c=AMBIENT, cop_dhw=2.5, min_days=5,
    )
    assert fit["status"] == "ok"
    profile = fit["profile_kwh_median"]
    assert profile[4] == pytest.approx(C_TANK * 6.0 / 3.6e6, abs=0.2)  # 08:00-10:00
    assert profile[10] == pytest.approx(0.0, abs=0.05)  # evening: nothing
    assert profile.index(max(profile)) == 4


def test_daily_total_is_not_the_sum_of_bucket_percentiles():
    """Σ p75(bucket) is an UPPER BOUND on a day's p75, not an estimate of it — no
    day sits at its 75th percentile in all twelve buckets at once. The daily stat
    must come from whole-day totals, and must not publish at all off a thin run
    of complete days."""
    rows: list[tuple[float, float]] = []
    cons: list[dict] = []
    for k in range(8):
        day = date(2026, 7, 6) + timedelta(days=k)
        # Every day draws morning AND evening, but ANTI-CORRELATED: a big-shower
        # morning is followed by a light evening and vice versa. So each bucket
        # has a high 75th percentile, yet NO day is high in both at once — which
        # is exactly the situation where summing the percentiles overshoots.
        morning = 8.0 if k % 2 else 2.0
        evening = 2.0 if k % 2 else 8.0
        rows += _full_day_rows(
            day,
            {8: 55.0, 9: 55.0 - morning, 20: 55.0 - morning, 21: 55.0 - morning - evening},
            55.0,
        )
        cons += quiet_buckets(day, {})
    fit = ttl.estimate_draw_profile(
        rows, cons, [], tz=TZ, c_tank_j_per_k=C_TANK, ua_w_per_k=2.0,
        t_ambient_c=AMBIENT, cop_dhw=2.5, min_days=5,
    )
    assert fit["status"] == "ok"
    assert fit["daily_full_days"] == 8
    sum_of_p75 = sum(fit["profile_kwh_p75"])
    assert sum_of_p75 > 0
    # Every day drew the same TOTAL (8 + 2), so the daily p75 is that total —
    # while Σ p75(bucket) claims 8 + 8. The bound is loose, and publishing it as
    # the daily figure would over-size the tank's demand by ~60%.
    assert fit["daily_kwh_p75"] < sum_of_p75
    assert fit["daily_kwh_p75"] == pytest.approx(C_TANK * 10.0 / 3.6e6, abs=0.35)


def test_draw_profile_gates_on_days_and_skips_null_buckets():
    day = date(2026, 7, 8)
    flat = _full_day_rows(day, {}, 45.0)
    fit = ttl.estimate_draw_profile(
        flat, quiet_buckets(day, {}), [], tz=TZ, c_tank_j_per_k=C_TANK,
        ua_w_per_k=2.0, t_ambient_c=AMBIENT, cop_dhw=2.5, min_days=5,
    )
    assert fit["status"] == "skipped"

    # A NULL counter is unknown, not zero — that bucket contributes nothing.
    rows, cons = [], []
    for k in range(8):
        d = date(2026, 7, 6) + timedelta(days=k)
        rows += _full_day_rows(d, {}, 45.0)
        cons += quiet_buckets(d, {5: None})
    fit = ttl.estimate_draw_profile(
        rows, cons, [], tz=TZ, c_tank_j_per_k=C_TANK, ua_w_per_k=2.0,
        t_ambient_c=AMBIENT, cop_dhw=2.5, min_days=5,
    )
    assert fit["status"] == "ok"
    assert fit["samples_per_bucket"][5] == 0
    assert fit["daily_full_days"] == 0  # no day is whole → no daily figure published


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
