"""#819 — the tank coasts toward the HOUSE: UA fitted with the ambient fixed
to the measured indoor (one identifiable parameter), live indoor honoured by
``resolve_tank_params`` only for that fit, coast-check telemetry, and the
consumers passing the live reading through."""
from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src import db
from src.config import config
from src.dhw import calibration as cal
from src.dhw import params
from src.dhw.model import TankParams, coast_rate_c_per_h

TZ = ZoneInfo("UTC")
C_TANK = 192.0 * 4186.0


def _coast_rows(start: datetime, *, t0: float, hours: float, ua: float, ambient: float,
                step_min: int = 30) -> list[tuple[float, float]]:
    tau_h = C_TANK / (ua * 3600.0)
    n = int(hours * 60 / step_min) + 1
    return [
        ((start + timedelta(minutes=k * step_min)).timestamp(),
         ambient + (t0 - ambient) * math.exp(-(k * step_min / 60.0) / tau_h))
        for k in range(n)
    ]


def _indoor_series(start: datetime, hours: float, temp: float, step_min: int = 20):
    n = int(hours * 60 / step_min) + 1
    return [(start + timedelta(minutes=k * step_min), temp) for k in range(n)]


# ── the fit ─────────────────────────────────────────────────────────────────


def test_indoor_fit_recovers_ua_across_two_house_temperatures():
    """Summer (30 °C house) and autumn (20 °C house) coasts with the SAME tank:
    the joint constant-ambient fit cannot place the ambient; the indoor-fixed
    fit recovers UA from both seasons pooled."""
    ua_true = 3.7
    rows: list[tuple[float, float]] = []
    indoor: list[tuple[datetime, float]] = []
    for k in range(6):
        night = datetime(2026, 7, 6, 22, 0, tzinfo=UTC) + timedelta(days=k)
        rows += _coast_rows(night, t0=47.0, hours=9, ua=ua_true, ambient=30.0)
        indoor += _indoor_series(night, 9, 30.0)
    for k in range(6):
        night = datetime(2026, 10, 1, 22, 0, tzinfo=UTC) + timedelta(days=k)
        rows += _coast_rows(night, t0=47.0, hours=9, ua=ua_true, ambient=20.0)
        indoor += _indoor_series(night, 9, 20.0)
    eps = cal.select_coast_episodes(rows, tz=TZ, indoor_by_utc=indoor)
    assert len(eps) == 12 and all(e.indoor_mean_c is not None for e in eps)
    fit = cal.fit_ua_indoor_ambient(eps, c_tank_j_per_k=C_TANK)
    assert fit["status"] == "ok" and fit["ambient_model"] == "indoor_measured"
    assert fit["ua_w_per_k"] == pytest.approx(ua_true, rel=0.03)
    assert fit["ambient_c"] == pytest.approx(25.0, abs=0.5)  # mean of the episode ambients
    assert fit["ambient_min_c"] == pytest.approx(20.0, abs=0.1)
    assert fit["ambient_max_c"] == pytest.approx(30.0, abs=0.1)
    assert fit["r2"] > 0.99


def test_indoor_fit_skips_without_indoor_coverage_and_on_too_few_episodes():
    rows: list[tuple[float, float]] = []
    for k in range(10):
        night = datetime(2026, 7, 6, 22, 0, tzinfo=UTC) + timedelta(days=k)
        rows += _coast_rows(night, t0=47.0, hours=9, ua=2.5, ambient=22.0)
    eps = cal.select_coast_episodes(rows, tz=TZ)  # no indoor series
    assert all(e.indoor_mean_c is None for e in eps)
    fit = cal.fit_ua_indoor_ambient(eps, c_tank_j_per_k=C_TANK)
    assert fit["status"] == "skipped" and "indoor coverage" in fit["reason"]
    # Coverage on only 3 of them → still below the gate.
    indoor = []
    for k in range(3):
        night = datetime(2026, 7, 6, 22, 0, tzinfo=UTC) + timedelta(days=k)
        indoor += _indoor_series(night, 9, 22.0)
    eps = cal.select_coast_episodes(rows, tz=TZ, indoor_by_utc=indoor)
    assert sum(1 for e in eps if e.indoor_mean_c is not None) == 3
    assert cal.fit_ua_indoor_ambient(eps, c_tank_j_per_k=C_TANK)["status"] == "skipped"


def test_indoor_mean_needs_at_least_three_readings_in_the_episode():
    night = datetime(2026, 7, 6, 22, 0, tzinfo=UTC)
    rows = _coast_rows(night, t0=47.0, hours=9, ua=2.5, ambient=22.0)
    eps = cal.select_coast_episodes(rows, tz=TZ, indoor_by_utc=[(night + timedelta(hours=1), 22.0)])
    assert eps and eps[0].indoor_mean_c is None


def test_coast_check_compares_measured_against_model_at_the_episode_ambient():
    ua_true = 3.7
    night = datetime(2026, 10, 1, 22, 0, tzinfo=UTC)
    rows = _coast_rows(night, t0=45.0, hours=8, ua=ua_true, ambient=23.7)
    eps = cal.select_coast_episodes(rows, tz=TZ, indoor_by_utc=_indoor_series(night, 8, 23.7))
    databook = TankParams()  # 2.44 W/K / 22.4 °C
    chk = cal.coast_check(eps, databook)
    assert chk is not None
    # Measured ≈ 3.7 × (44.6 − 23.7) / C; the databook at the same ambient reads
    # 2.44/3.7 of it → ratio ≈ 1.5: the "esfria mais rápido" the owner saw.
    assert chk["measured_c_per_h"] == pytest.approx(
        coast_rate_c_per_h(0.5 * (45.0 + rows[-1][1]), TankParams(ua_w_per_k=ua_true), ambient_c=23.7),
        rel=0.05,
    )
    assert chk["ratio_measured_over_model"] == pytest.approx(ua_true / 2.44, rel=0.08)
    assert chk["ambient_used_c"] == pytest.approx(23.7, abs=0.1)
    fitted = TankParams(ua_w_per_k=ua_true, ambient_c=23.7, source="measured_indoor")
    assert cal.coast_check(eps, fitted)["ratio_measured_over_model"] == pytest.approx(1.0, abs=0.05)
    assert cal.coast_check([], databook) is None


# ── resolve_tank_params ─────────────────────────────────────────────────────


@pytest.fixture
def _db():
    db.init_db()


def test_live_indoor_is_honoured_only_for_the_indoor_fitted_ua(_db, monkeypatch):
    monkeypatch.setattr(config, "DHW_CALIBRATION_ENABLED", True, raising=False)
    db.upsert_dhw_calibration("ua_ambient", status="ok", payload={
        "ambient_model": "indoor_measured", "ua_w_per_k": 3.7, "ambient_c": 23.0,
        "ambient_min_c": 18.0, "ambient_max_c": 26.0, "r2": 0.9,
    }, n_samples=12, r2=0.9)
    p = params.resolve_tank_params(ambient_c=19.5)
    assert p.source == "measured_indoor"
    assert p.ua_w_per_k == pytest.approx(3.7) and p.ambient_c == pytest.approx(19.5)
    # No live reading → the fit's mean indoor.
    assert params.resolve_tank_params().ambient_c == pytest.approx(23.0)
    # An implausible live value is CLAMPED to the fitted range (+3), never dropped.
    assert params.resolve_tank_params(ambient_c=60.0).ambient_c == pytest.approx(29.0)
    assert params.resolve_tank_params(ambient_c=60.0).source == "measured_indoor"
    # The JOINT fit's effective ambient is NOT replaced by the live indoor.
    db.upsert_dhw_calibration("ua_ambient", status="ok", payload={
        "ambient_model": "constant", "ua_w_per_k": 2.0, "ambient_c": 13.2, "r2": 0.7,
    }, n_samples=27, r2=0.7)
    q = params.resolve_tank_params(ambient_c=19.5)
    assert q.source == "measured" and q.ambient_c == pytest.approx(13.2)
    # Databook fallback ignores it too (its UA was not fitted against the room).
    db.upsert_dhw_calibration("ua_ambient", status="skipped", payload={"reason": "x"})
    assert params.resolve_tank_params(ambient_c=19.5).source == "databook"


def test_indoor_fit_ua_up_to_six_w_per_k_is_accepted(_db, monkeypatch):
    monkeypatch.setattr(config, "DHW_CALIBRATION_ENABLED", True, raising=False)
    db.upsert_dhw_calibration("ua_ambient", status="ok", payload={
        "ambient_model": "indoor_measured", "ua_w_per_k": 5.5, "ambient_c": 21.0, "r2": 0.8,
    }, n_samples=12, r2=0.8)
    assert params.resolve_tank_params().ua_w_per_k == pytest.approx(5.5)
    db.upsert_dhw_calibration("ua_ambient", status="ok", payload={
        "ambient_model": "constant", "ua_w_per_k": 5.5, "ambient_c": 21.0, "r2": 0.8,
    }, n_samples=12, r2=0.8)
    assert params.resolve_tank_params().source == "databook"  # joint fit keeps the 5 W/K cap


def test_live_indoor_ambient_reads_the_fresh_house_mean(_db, monkeypatch):
    monkeypatch.setattr(config, "INDOOR_SENSOR_STALE_MINUTES", 30, raising=False)
    assert params.live_indoor_ambient_c() is None
    now = datetime.now(UTC)
    db.save_indoor_readings([
        {"captured_at": now.isoformat(), "room": "corredor", "temp_c": 21.0},
        {"captured_at": now.isoformat(), "room": "cozinha", "temp_c": 19.0},
    ])
    assert params.live_indoor_ambient_c() == pytest.approx(20.0)


# ── consumers pass the live reading through ─────────────────────────────────


def test_refresh_persists_the_indoor_fit_and_a_coast_check(_db, monkeypatch):
    """End-to-end on the nightly job: live tank rows + indoor readings in the
    DB → ``ua_ambient`` row carries ``ambient_model=indoor_measured`` and a
    ``coast_check`` row exists."""
    monkeypatch.setattr(config, "BULLETPROOF_TIMEZONE", "UTC")
    monkeypatch.setattr(config, "DHW_CALIBRATION_ENABLED", True, raising=False)
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    ua_true = 3.4
    indoor_rows = []
    for k in range(1, 12):
        night = (now - timedelta(days=k)).replace(hour=22)
        for ts, t in _coast_rows(night, t0=46.0, hours=9, ua=ua_true, ambient=21.0):
            db.insert_daikin_telemetry({"fetched_at": ts, "source": "live", "tank_temp_c": t})
        for ts, t in _indoor_series(night, 9, 21.0):
            indoor_rows.append({"captured_at": ts.isoformat(), "room": "corredor", "temp_c": t})
    db.save_indoor_readings(indoor_rows)
    out = cal.refresh_dhw_calibration()
    row = db.get_dhw_calibration("ua_ambient")
    assert row and row["status"] == "ok", (out, row)
    assert row["payload"]["ambient_model"] == "indoor_measured"
    assert row["payload"]["ua_w_per_k"] == pytest.approx(ua_true, rel=0.05)
    chk = db.get_dhw_calibration("coast_check")
    assert chk and chk["status"] == "ok"
    # Out-of-sample by construction: the first refresh checks the DATABOOK
    # that was steering (UA 2.44) against a UA-3.4 tank → ratio ≈ 1.4.
    assert chk["payload"]["params_source"] == "databook"
    assert chk["payload"]["ratio_median_recent"] == pytest.approx(ua_true / 2.44, rel=0.15)


# ── review follow-ups ───────────────────────────────────────────────────────


def test_live_indoor_outside_the_old_bounds_is_clamped_not_rejected(_db, monkeypatch):
    """A 30.5 °C summer house or a 9.5 °C heating-fault house must keep the
    indoor-fitted UA (clamped ambient), never flip to the databook pair."""
    monkeypatch.setattr(config, "DHW_CALIBRATION_ENABLED", True, raising=False)
    db.upsert_dhw_calibration("ua_ambient", status="ok", payload={
        "ambient_model": "indoor_measured", "ua_w_per_k": 3.3, "ambient_c": 23.0,
        "ambient_min_c": 21.0, "ambient_max_c": 25.0, "r2": 0.8,
    }, n_samples=12, r2=0.8)
    hot = params.resolve_tank_params(ambient_c=31.0)
    assert hot.source == "measured_indoor" and hot.ambient_c == pytest.approx(28.0)  # max+3
    cold = params.resolve_tank_params(ambient_c=9.0)
    assert cold.source == "measured_indoor" and cold.ambient_c == pytest.approx(18.0)  # min−3
    mid = params.resolve_tank_params(ambient_c=22.2)
    assert mid.ambient_c == pytest.approx(22.2)
    # The STORED fit's own ambient is still gated: a joint fit at 29 °C stays rejected.
    db.upsert_dhw_calibration("ua_ambient", status="ok", payload={
        "ambient_model": "constant", "ua_w_per_k": 2.5, "ambient_c": 29.0, "r2": 0.8,
    }, n_samples=12, r2=0.8)
    assert params.resolve_tank_params().source == "databook"


def test_multi_room_indoor_mean_is_equal_weight_across_rooms():
    """A room on a 1-min cadence must not out-vote one on 10 min; and three
    rooms reporting one instant are not a sample of the night."""
    night = datetime(2026, 10, 1, 22, 0, tzinfo=UTC)
    rows = _coast_rows(night, t0=45.0, hours=8, ua=3.3, ambient=21.5)
    fast = [(night + timedelta(minutes=k), "sala", 24.0) for k in range(0, 8 * 60, 1)]
    slow = [(night + timedelta(minutes=k), "cozinha", 19.0) for k in range(0, 8 * 60, 10)]
    eps = cal.select_coast_episodes(rows, tz=TZ, indoor_by_utc=fast + slow)
    assert eps and eps[0].indoor_mean_c == pytest.approx(21.5, abs=0.01)
    one_instant = [(night + timedelta(hours=1), r, 21.0) for r in ("a", "b", "c")]
    eps2 = cal.select_coast_episodes(rows, tz=TZ, indoor_by_utc=one_instant)
    assert eps2 and eps2[0].indoor_mean_c is None
    edge_only = [(night + timedelta(minutes=m), "a", 21.0) for m in (0, 5, 10)]  # 10 min of an 8 h night
    eps3 = cal.select_coast_episodes(rows, tz=TZ, indoor_by_utc=edge_only)
    assert eps3 and eps3[0].indoor_mean_c is None


def test_indoor_fit_is_unbiased_on_one_degree_quantised_readings():
    """The PR's motivation: 1 °C-rounded tank readings at 10-min polling."""
    import random

    rnd = random.Random(819)
    ua_true = 3.3
    rows: list[tuple[float, float]] = []
    indoor: list[tuple[datetime, float]] = []
    for k in range(20):
        night = datetime(2026, 9, 1, 22, 0, tzinfo=UTC) + timedelta(days=k)
        amb = 20.0 + rnd.uniform(-1.5, 1.5)
        for ts, t in _coast_rows(night, t0=46.0 + rnd.uniform(-2, 4), hours=9, ua=ua_true,
                                 ambient=amb, step_min=10):
            rows.append((ts, float(round(t))))
        indoor += _indoor_series(night, 9, amb)
    eps = cal.select_coast_episodes(rows, tz=TZ, indoor_by_utc=indoor)
    fit = cal.fit_ua_indoor_ambient(eps, c_tank_j_per_k=C_TANK)
    assert fit["status"] == "ok", fit
    assert fit["ua_w_per_k"] == pytest.approx(ua_true, rel=0.12)


def test_lp_block_ambient_prefers_the_initial_state_then_the_live_reading(monkeypatch):
    from src.scheduler import lp_optimizer as lpo

    monkeypatch.setattr(params, "live_indoor_ambient_c", lambda: 19.5)
    assert lpo.dhw_block_ambient_c(lpo.LpInitialState(soc_kwh=1.0, tank_temp_c=45.0, indoor_temp_c=22.5)) == 22.5
    assert lpo.dhw_block_ambient_c(lpo.LpInitialState(soc_kwh=1.0, tank_temp_c=45.0)) == 19.5
    monkeypatch.setattr(params, "live_indoor_ambient_c", lambda: None)
    assert lpo.dhw_block_ambient_c(lpo.LpInitialState(soc_kwh=1.0, tank_temp_c=45.0)) is None


def test_consumers_pass_the_live_ambient_into_resolve_tank_params(_db, monkeypatch):
    """The dynamic window resolver and the deadband force both call
    resolve_tank_params with the live house temperature."""
    seen: list[float | None] = []
    real = params.resolve_tank_params

    def spy(*, ambient_c=None):
        seen.append(ambient_c)
        return real(ambient_c=ambient_c)

    monkeypatch.setattr(params, "resolve_tank_params", spy)
    monkeypatch.setattr(params, "live_indoor_ambient_c", lambda: 19.5)
    from src import dhw_policy, state_machine
    from types import SimpleNamespace

    monkeypatch.setattr(config, "DHW_DYNAMIC_WINDOW_ENABLED", True, raising=False)
    try:
        dhw_policy.resolve_window_decision_local(datetime.now(UTC).date() + timedelta(days=40), None, t_out_c=10.0)
    except Exception:  # noqa: BLE001 — static fallback paths may bail before the params read
        pass
    dev = SimpleNamespace(tank_temperature=42.0)
    state_machine._warmup_deadband_force_reason(dev, {"tank_temp": 45.0}, datetime.now(UTC))
    assert 19.5 in seen


def test_coast_check_audit_row_lands_in_action_log(_db, monkeypatch):
    monkeypatch.setattr(config, "BULLETPROOF_TIMEZONE", "UTC")
    monkeypatch.setattr(config, "DHW_CALIBRATION_ENABLED", True, raising=False)
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    indoor_rows = []
    for k in range(1, 12):
        night = (now - timedelta(days=k)).replace(hour=22)
        for ts, t in _coast_rows(night, t0=46.0, hours=9, ua=3.4, ambient=21.0):
            db.insert_daikin_telemetry({"fetched_at": ts, "source": "live", "tank_temp_c": t})
        for ts, t in _indoor_series(night, 9, 21.0):
            indoor_rows.append({"captured_at": ts.isoformat(), "room": "corredor", "temp_c": t})
    db.save_indoor_readings(indoor_rows)
    cal.refresh_dhw_calibration()
    conn = db.get_connection()
    try:
        n = conn.execute("SELECT COUNT(*) FROM action_log WHERE action='tank_coast_check'").fetchone()[0]
    finally:
        conn.close()
    assert n == 1
    # First refresh: the check ran against the DATABOOK (what was steering) → ratio ≈ UA_true/2.44.
    chk = db.get_dhw_calibration("coast_check")["payload"]
    assert chk["params_source"] == "databook"
    assert chk["ratio_median_recent"] == pytest.approx(3.4 / 2.44, rel=0.15)
