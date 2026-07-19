"""The economic shadow and its enable gate (#714).

The gate is the last thing between the regime and production, so its refusals matter as
much as its approvals: cheaper is not enough — a single cold shower disqualifies, and so
does a thin run of days or a quota breach.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from src import db as _db
from src.config import config
from src.dhw import shadow


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    path = tmp_path / "shadow.db"
    monkeypatch.setenv("DB_PATH", str(path))
    monkeypatch.setattr(_db, "_db_path", lambda: path)
    _db.init_db()
    monkeypatch.setattr(config, "BULLETPROOF_TIMEZONE", "UTC", raising=False)
    monkeypatch.setattr(config, "DHW_LP_OWNED_GATE_MIN_DAYS", 14, raising=False)
    monkeypatch.setattr(config, "DHW_LP_OWNED_GATE_MIN_SAVING_PENCE", 3.0, raising=False)
    monkeypatch.setattr(config, "DHW_LP_OWNED_GATE_MAX_ROWS", 6, raising=False)
    return path


def _seed(day: str, *, delta_p: float, deficit: float = 0.0, rows: int = 4,
          horizon_days: float = 1.0):
    _db.insert_dhw_shadow({
        "run_at_utc": f"{day}T12:00:00+00:00",
        "day": day, "cost_pinned_p": 100.0, "cost_lp_owned_p": 100.0 + delta_p,
        "delta_p": delta_p, "comfort_deficit_c": deficit,
        "horizon_days": horizon_days, "n_tank_rows": rows,
    })


def _days(n: int) -> list[str]:
    base = datetime.now(UTC).date() - timedelta(days=n)
    return [(base + timedelta(days=i)).isoformat() for i in range(n)]


# ---------------------------------------------------------------------------
# The cost metric
# ---------------------------------------------------------------------------


def test_grid_cost_is_imports_bought_minus_exports_sold():
    plan = SimpleNamespace(
        import_kwh=[1.0, 2.0, 0.0], export_kwh=[0.0, 0.0, 1.0],
    )
    cost = shadow.grid_cost_pence(plan, [10.0, 20.0, 30.0], [5.0, 5.0, 15.0])
    assert cost == pytest.approx(1.0 * 10 + 2.0 * 20 - 1.0 * 15)  # 35


def test_comfort_deficit_reads_the_tank_against_the_floor(monkeypatch):
    from zoneinfo import ZoneInfo

    starts = [datetime(2026, 7, 8, 20, 0, tzinfo=UTC)]  # a 20:00 shower boundary
    plan = SimpleNamespace(
        dhw_lp_owned=True, slot_starts_utc=starts, tank_temp_c=[41.0, 41.0],
    )
    # A tank at 41 against a 45 floor is 4 °C short — a cold shower.
    d = shadow.comfort_deficit_c(plan, ZoneInfo("UTC"), "normal")
    assert d == pytest.approx(4.0, abs=0.5)

    plan.tank_temp_c = [46.0, 46.0]  # hot enough
    assert shadow.comfort_deficit_c(plan, ZoneInfo("UTC"), "normal") == 0.0


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_gate_opens_on_a_clean_cheaper_run(tmp_db):
    for d in _days(14):
        _seed(d, delta_p=-10.0)  # 10p/day cheaper, comfort-clean
    gate = shadow.evaluate_gate()
    assert gate["ready"] is True
    assert gate["median_saving_pence"] == pytest.approx(10.0)
    assert gate["comfort_breach_days"] == 0


def test_deltas_are_normalised_per_day_of_horizon(tmp_db):
    """The units bug found on deploy day one: a solve's delta covers the whole ~48h
    horizon (two evenings), so comparing it raw against a per-day threshold doubles
    the bar. A −8p delta over a 2-day horizon is a 4p/day saving — above the 3p bar —
    and rows without the column (pre-migration) conservatively assume 2 days."""
    for d in _days(14):
        _seed(d, delta_p=-8.0, horizon_days=2.0)
    gate = shadow.evaluate_gate()
    assert gate["median_saving_pence"] == pytest.approx(4.0)
    assert gate["ready"] is True

    # Same raw delta claimed as ONE day would read 8p/day — the normalisation is
    # what separates the two, not the threshold.
    _db.get_connection().execute("DELETE FROM dhw_lp_shadow_log").connection.commit()


def test_a_single_cold_shower_disqualifies_the_whole_run(tmp_db):
    """Comfort is not for sale. Fourteen cheaper days, but one left the tank cold — the
    gate must refuse, no matter how large the saving."""
    days = _days(14)
    for d in days:
        _seed(d, delta_p=-15.0)
    _seed(days[7], delta_p=-15.0, deficit=3.0)  # one breach
    gate = shadow.evaluate_gate()
    assert gate["ready"] is False
    assert gate["comfort_breach_days"] == 1


def test_cheaper_but_not_by_enough_does_not_open(tmp_db):
    for d in _days(14):
        _seed(d, delta_p=-1.0)  # cheaper, but below the 3p bar
    assert shadow.evaluate_gate()["ready"] is False


def test_a_thin_run_does_not_open_however_good(tmp_db):
    for d in _days(5):
        _seed(d, delta_p=-20.0)
    gate = shadow.evaluate_gate()
    assert gate["ready"] is False
    assert gate["days"] == 5


def test_a_quota_breach_blocks_the_gate(tmp_db):
    for d in _days(14):
        _seed(d, delta_p=-10.0, rows=9)  # cheaper + clean, but too many Daikin rows
    assert shadow.evaluate_gate()["ready"] is False


def test_the_suggestion_fires_once(tmp_db, monkeypatch):
    for d in _days(14):
        _seed(d, delta_p=-10.0)
    monkeypatch.setattr(config, "DHW_LP_OWNED_ENABLED", False, raising=False)

    sent = []
    import src.notifier as notifier
    monkeypatch.setattr(notifier, "notify",
                        lambda *a, **k: sent.append(a), raising=False)

    shadow.maybe_suggest_enable()
    shadow.maybe_suggest_enable()  # second call must be a no-op
    assert len(sent) == 1


def test_no_suggestion_when_already_enabled(tmp_db, monkeypatch):
    for d in _days(14):
        _seed(d, delta_p=-10.0)
    monkeypatch.setattr(config, "DHW_LP_OWNED_ENABLED", True, raising=False)
    sent = []
    import src.notifier as notifier
    monkeypatch.setattr(notifier, "notify", lambda *a, **k: sent.append(a), raising=False)
    shadow.maybe_suggest_enable()
    assert sent == []


def test_a_row_crossing_the_legionella_window_is_trimmed_not_dropped(tmp_db, monkeypatch):
    """A Saturday-night setback block can span straight through the Sunday 11:00 UTC
    stand-off. HEM must not write into the firmware-owned window — but dropping the
    whole row would leave the tank with no target for hours either side. The row is
    CUT AROUND the window instead."""
    from datetime import UTC, datetime, timedelta

    from src.scheduler.lp_dispatch import _trim_rows_around_legionella
    from src.dhw.dispatch import TankRow

    monkeypatch.setattr(config, "DHW_LEGIONELLA_STANDOFF_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "DHW_LEGIONELLA_STANDOFF_DOW", 6, raising=False)
    monkeypatch.setattr(config, "DHW_LEGIONELLA_STANDOFF_START_HOUR_UTC", 11, raising=False)
    monkeypatch.setattr(config, "DHW_LEGIONELLA_STANDOFF_DURATION_MINUTES", 120, raising=False)

    # 2026-07-12 is a Sunday; a long setback row 07:00 -> 18:00 UTC crosses 11-13.
    starts = [datetime(2026, 7, 12, 7, 0, tzinfo=UTC) + i * timedelta(minutes=30)
              for i in range(22)]
    row = TankRow(action_type="tank_setback",
                  start_utc=datetime(2026, 7, 12, 7, 0, tzinfo=UTC),
                  end_utc=datetime(2026, 7, 12, 18, 0, tzinfo=UTC),
                  tank_temp_c=37, tank_powerful=False)
    out = _trim_rows_around_legionella([row], starts)
    assert len(out) == 2
    assert out[0].end_utc == datetime(2026, 7, 12, 11, 0, tzinfo=UTC)
    assert out[1].start_utc == datetime(2026, 7, 12, 13, 0, tzinfo=UTC)
    # Nothing HEM writes overlaps the firmware-owned window.
    for r in out:
        assert r.end_utc <= datetime(2026, 7, 12, 11, 0, tzinfo=UTC) or (
            r.start_utc >= datetime(2026, 7, 12, 13, 0, tzinfo=UTC))


def test_the_shadow_throttle_fails_closed(tmp_db, monkeypatch):
    """If the shadow-log read/insert is silently failing, counting on the DB would let
    the shadow run on EVERY optimizer solve for the rest of the day — doubling the LP
    load in prod. The in-memory attempt counter must trip the cap regardless of DB
    health."""
    from src.dhw import shadow as sh

    monkeypatch.setattr(config, "DHW_LP_OWNED_SHADOW_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "DHW_LP_OWNED_ENABLED", False, raising=False)
    monkeypatch.setattr(config, "DHW_LP_OWNED_SHADOW_MAX_PER_DAY", 2, raising=False)
    monkeypatch.setitem(config._overrides, "DAIKIN_CONTROL_MODE", "active")
    sh._ATTEMPTS_BY_DAY.clear()

    # The DB read explodes every time — the fail-open path of the old code.
    def _boom(day):
        raise RuntimeError("db gone")

    monkeypatch.setattr(_db, "get_dhw_shadow_rows", _boom)

    solves = []

    def _fake_solve(**kwargs):
        solves.append(1)
        raise RuntimeError("stop here — attempt already counted")

    monkeypatch.setattr("src.scheduler.lp_optimizer.solve_lp", _fake_solve)

    for _ in range(6):
        sh.record_shadow(solve_kwargs={"slot_starts_utc": [], "weather": None,
                                       "initial": None},
                         price_pence=[], export_price_pence=None)
    # Only the first `cap` attempts may reach the solver; the rest are throttled by
    # the in-memory counter even though the DB never answered.
    assert len(solves) <= 2


# ---------------------------------------------------------------------------
# Winter watch
# ---------------------------------------------------------------------------


def _seed_outdoor(day: str, temp_c: float):
    """Live telemetry rows so the day's mean outdoor temp classifies its season."""
    conn = _db.get_connection()
    try:
        base = datetime.fromisoformat(day + "T12:00:00+00:00").timestamp()
        for k in range(3):
            conn.execute(
                "INSERT INTO daikin_telemetry (fetched_at, source, outdoor_temp_c) "
                "VALUES (?, 'live', ?)", (base + k * 3600, temp_c))
        conn.commit()
    finally:
        conn.close()


def test_winter_ping_fires_once_when_enough_cold_days_exist(tmp_db, monkeypatch):
    """The owner's ask: 'me avisa quando o shadow tiver dados do inverno'. Fourteen
    scored days below the winter threshold → ONE Telegram message with the
    winter-only numbers, then silence (re-arm by clearing the runtime setting)."""
    monkeypatch.setattr(config, "DHW_SHADOW_WINTER_TEMP_C", 12.0, raising=False)
    monkeypatch.setattr(config, "DHW_SHADOW_WINTER_MIN_DAYS", 14, raising=False)
    monkeypatch.setitem(config._overrides, "DAIKIN_CONTROL_MODE", "active")
    for d in _days(15):
        _seed(d, delta_p=-8.0, horizon_days=2.0)
        _seed_outdoor(d, 7.0)  # a proper winter day

    sent = []
    import src.notifier as notifier
    monkeypatch.setattr(notifier, "notify", lambda *a, **k: sent.append(a[0]), raising=False)

    shadow.maybe_notify_winter_data()
    shadow.maybe_notify_winter_data()  # second call: already notified → silent
    assert len(sent) == 1
    assert "WINTER" in sent[0]
    assert "14" in sent[0] or "15" in sent[0]


def test_summer_days_do_not_trigger_the_winter_ping(tmp_db, monkeypatch):
    """Twenty scored days at 22 °C outdoors are not winter evidence, however good the
    deltas look — the whole point is not to declare the thesis answered in July."""
    monkeypatch.setitem(config._overrides, "DAIKIN_CONTROL_MODE", "active")
    for d in _days(20):
        _seed(d, delta_p=-8.0, horizon_days=2.0)
        _seed_outdoor(d, 22.0)
    sent = []
    import src.notifier as notifier
    monkeypatch.setattr(notifier, "notify", lambda *a, **k: sent.append(a[0]), raising=False)
    shadow.maybe_notify_winter_data()
    assert sent == []


def test_a_stalled_shadow_warns_once_and_rearms_on_resume(tmp_db, monkeypatch):
    """Silent stall = discovering in November that October never got measured. No rows
    for 7+ days while enabled+active → one warning; rows resuming clears the key so a
    FUTURE stall warns again."""
    monkeypatch.setattr(config, "DHW_LP_OWNED_SHADOW_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "DHW_LP_OWNED_ENABLED", False, raising=False)
    monkeypatch.setitem(config._overrides, "DAIKIN_CONTROL_MODE", "active")

    sent = []
    import src.notifier as notifier
    monkeypatch.setattr(notifier, "notify", lambda *a, **k: sent.append(a[0]), raising=False)

    shadow.maybe_notify_winter_data()   # empty log → stall warning
    shadow.maybe_notify_winter_data()   # still stalled → no second warning
    assert len(sent) == 1
    assert "nothing" in sent[0] or "recorded" in sent[0]

    _seed(_days(2)[-1], delta_p=-5.0)   # a fresh row resumes the shadow
    shadow.maybe_notify_winter_data()   # clears the stall key
    assert _db.get_runtime_setting("dhw_shadow_stall_notified_at") in ("", None)


def test_the_baseline_sim_follows_the_configured_windows(tmp_db, monkeypatch):
    """The incumbent's windows are TUNABLE (tuned 2026-07-15: setback 15:00, target
    47°C after the chained window study). The shadow's baseline must mirror whatever
    is configured — a sim frozen on the old 13/22/45 defaults would stop representing
    what prod actually does, and the winter gate would compare against a ghost."""
    captured = {}

    import src.dhw.baseline as B

    orig = B.simulate_fixed_schedule

    def _spy(*a, **k):
        captured.update(k)
        return orig(*a, **k)

    monkeypatch.setattr(B, "simulate_fixed_schedule", _spy)
    monkeypatch.setitem(config._overrides, "DHW_SETBACK_START_HOUR_LOCAL", 15)
    monkeypatch.setattr(config, "DHW_TEMP_NORMAL_C", 47.0, raising=False)
    monkeypatch.setattr(config, "DHW_LP_OWNED_SHADOW_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "DHW_LP_OWNED_ENABLED", False, raising=False)
    monkeypatch.setattr(config, "DHW_FIXED_SCHEDULE_ENABLED", True, raising=False)
    monkeypatch.setitem(config._overrides, "DAIKIN_CONTROL_MODE", "active")
    shadow._ATTEMPTS_BY_DAY.clear()

    from types import SimpleNamespace

    starts = [datetime(2026, 7, 8, 0, 0, tzinfo=UTC) + timedelta(minutes=30 * i)
              for i in range(8)]
    weather = SimpleNamespace(temperature_outdoor_c=[15.0] * 8)
    initial = SimpleNamespace(tank_temp_c=45.0)

    def _fake_solve(**kwargs):
        raise RuntimeError("stop after the sim ran")

    monkeypatch.setattr("src.scheduler.lp_optimizer.solve_lp", _fake_solve)
    shadow.record_shadow(
        solve_kwargs={"slot_starts_utc": starts, "weather": weather,
                      "initial": initial, "micro_climate_offset_c": 0.0,
                      "micro_climate_offset_by_hour_c": {}},
        price_pence=[10.0] * 8, export_price_pence=None)

    assert captured.get("setback_hour_local") == 15.0
    assert captured.get("target_c") == 47.0
    # #732 — the sim's thermostat must use the firmware's measured reheat
    # deadband (fallback 6.0 °C when unfitted), not the old 1 °C guess that
    # simulated daily top-ups the real firmware skips.
    assert captured.get("hysteresis_c") == 6.0
