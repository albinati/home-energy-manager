"""#818 — probabilistic load: Daikin outdoor fallback (full-window profile),
p90 tier + quantile lookup, band-aware scenario quantile, cheap-exit charge
floors (see test_google_calendar_bands) and the /load/expected surface."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from src import db
from src.config import config
from src.energy.tariff_structure import detect
from src.scheduler import optimizer as opt_mod

LON = ZoneInfo("Europe/London")
COSY = "E-1R-COSY-22-12-08-H"


def _price(h: int) -> float:
    if h in (4, 5, 6, 13, 14, 15, 22, 23):
        return 12.4868
    if h in (16, 17, 18):
        return 38.174
    return 25.4461


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    db.init_db()
    db.clear_residual_profile_cache()
    from src.analytics import load_expected

    load_expected.clear_cache()
    monkeypatch.setattr(config, "OCTOPUS_TARIFF_CODE", COSY)
    monkeypatch.setattr(config, "OCTOPUS_TARIFF_STRUCTURE", "auto", raising=False)
    monkeypatch.setattr(config, "TARIFF_DISPLAY_NAME", "", raising=False)
    monkeypatch.setattr(config, "BULLETPROOF_TIMEZONE", "Europe/London")
    monkeypatch.setattr(config, "HEM_UI_AUTH_REQUIRED", False, raising=False)


def _z(t: datetime) -> str:
    return t.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _recent_days(weekday: int, n: int, *, start_ago: int = 3) -> list[date]:
    out: list[date] = []
    d = datetime.now(LON).date() - timedelta(days=start_ago)
    while len(out) < n:
        if d.weekday() == weekday:
            out.append(d)
        d -= timedelta(days=1)
    return out


def _seed_day_loads(d: date, kw_by_hour: dict[int, float], *, outdoor_via: str) -> None:
    """One pv sample per half-hour on local day ``d``; outdoor temperature via
    the meteo history (``meteo``) or ONLY the Daikin heartbeat (``daikin``)."""
    for h in range(24):
        for m in (0, 30):
            t = datetime(d.year, d.month, d.day, h, m, tzinfo=LON).astimezone(UTC)
            db.save_pv_realtime_sample(_z(t), load_power_kw=kw_by_hour.get(h, 0.5))
            if outdoor_via == "meteo":
                db.save_meteo_forecast_history(
                    _z(t - timedelta(minutes=5)),
                    [{"slot_time": t.replace(minute=0).isoformat(), "temp_c": 12.0, "solar_w_m2": 0.0}],
                )
            else:
                db.log_execution({
                    "timestamp": _z(t + timedelta(minutes=2)),
                    "consumption_kwh": 0.0, "agile_price_pence": 20.0,
                    "daikin_outdoor_temp": 12.0, "source": "test",
                })


# ── residual profile: Daikin outdoor fallback + p90 ─────────────────────────


def test_profile_keeps_samples_whose_outdoor_comes_only_from_the_daikin_sensor():
    """Samples older than the meteo retention used to be DROPPED (prod learned
    31 of 120 days). With the execution_log fallback they are retained."""
    tuesdays = _recent_days(1, 6)
    for d in tuesdays[:3]:
        _seed_day_loads(d, {18: 2.0}, outdoor_via="meteo")
    for d in tuesdays[3:]:
        _seed_day_loads(d, {18: 2.0}, outdoor_via="daikin")
    prof = db.residual_load_profile_v2(window_days=120, use_cache=False)
    assert prof["day_counts"]["weekday"] == 6
    assert prof["outdoor_from_daikin_samples"] >= 3 * 48
    assert prof["no_outdoor_dropped_samples"] == 0


def test_profile_still_drops_samples_with_no_outdoor_source_at_all():
    d = _recent_days(1, 1)[0]
    for h in range(24):
        t = datetime(d.year, d.month, d.day, h, 0, tzinfo=LON).astimezone(UTC)
        db.save_pv_realtime_sample(_z(t), load_power_kw=0.5)
    prof = db.residual_load_profile_v2(window_days=120, use_cache=False)
    assert prof["no_outdoor_dropped_samples"] == 24
    assert prof["day_counts"]["total"] == 0


def test_p90_tier_sits_above_p75_and_quantile_lookup_falls_back():
    tuesdays = _recent_days(1, 10)
    # 18:00: eight quiet Tuesdays at 0.6 kW, two cooking Tuesdays at 3.0 kW.
    for i, d in enumerate(tuesdays):
        _seed_day_loads(d, {18: 3.0 if i < 2 else 0.6}, outdoor_via="meteo")
    prof = db.residual_load_profile_v2(window_days=120, use_cache=False)
    p50 = db.lookup_residual_quantile_kwh(prof, 1, 18, 0, "p50")
    p75 = db.lookup_residual_quantile_kwh(prof, 1, 18, 0, "p75")
    p90 = db.lookup_residual_quantile_kwh(prof, 1, 18, 0, "p90")
    assert p50 == pytest.approx(db.lookup_residual_kwh(prof, 1, 18, 0))
    assert p75 == pytest.approx(db.lookup_residual_spread_kwh(prof, 1, 18, 0))
    assert p50 <= p75 <= p90
    assert p90 > p75  # the two cooking days live in the top decile only
    # A profile without the p90 tier (older cache / thin bucket) degrades to p75.
    legacy = {"profile": prof["profile"], "spread": prof["spread"], "flat": prof["flat"]}
    assert db.lookup_residual_quantile_kwh(legacy, 1, 18, 0, "p90") == pytest.approx(p75)
    # Thin (dow,h,m) bucket (< 8 samples) carries NO p90 so the lookup falls
    # through to the richer weekday tier instead of a p75 in disguise.
    wed = _recent_days(2, 5)
    for d in wed:
        _seed_day_loads(d, {18: 1.0}, outdoor_via="meteo")
    prof2 = db.residual_load_profile_v2(window_days=120, use_cache=False)
    assert (2, 18, 0) not in prof2["spread_p90"]
    assert ("weekday", 18, 0) in prof2["spread_p90"]
    assert db.lookup_residual_quantile_kwh(prof2, 2, 18, 0, "p90") == pytest.approx(
        prof2["spread_p90"][("weekday", 18, 0)])
    # The p90 never extrapolates above the observed maximum (exclusive quantiles
    # on 8–9 samples do: [0.3×7, 1.0] → 1.07).
    for key, vs_max in ((1, 18, 0), 1.5), :
        assert prof2["spread_p90"][key] <= vs_max + 1e-9


# ── optimizer: band-aware scenario quantile ─────────────────────────────────


def _cosy_prices() -> list[float]:
    return [_price(h) for h in range(24) for _ in (0, 30)]


def test_default_quantile_is_p75_everywhere(monkeypatch):
    monkeypatch.setattr(config, "LP_LOAD_EXPENSIVE_BAND_QUANTILE", "p75", raising=False)
    q, struct = opt_mod._expensive_band_quantile(_cosy_prices())
    assert (q, struct) == ("p75", None)
    assert opt_mod._spread_quantile_for_slot(struct, 38.17, q) == "p75"


def test_p90_applies_to_day_and_peak_bands_only_on_a_banded_tariff(monkeypatch):
    monkeypatch.setattr(config, "LP_LOAD_EXPENSIVE_BAND_QUANTILE", "p90", raising=False)
    q, struct = opt_mod._expensive_band_quantile(_cosy_prices())
    assert q == "p90" and struct is not None and struct.is_banded
    assert opt_mod._spread_quantile_for_slot(struct, 12.49, q) == "p75"   # cheap
    assert opt_mod._spread_quantile_for_slot(struct, 25.45, q) == "p90"   # day
    assert opt_mod._spread_quantile_for_slot(struct, 38.17, q) == "p90"   # peak


def test_p90_never_engages_on_a_dynamic_tariff(monkeypatch):
    monkeypatch.setattr(config, "LP_LOAD_EXPENSIVE_BAND_QUANTILE", "p90", raising=False)
    agile = [5.0 + (i * 7.3) % 31.0 for i in range(48)]  # 48 distinct levels
    assert not detect(agile).is_banded
    q, struct = opt_mod._expensive_band_quantile(agile)
    assert q == "p90" and struct is None
    assert opt_mod._spread_quantile_for_slot(struct, 35.0, q) == "p75"


def test_unknown_quantile_name_degrades_to_p75(monkeypatch):
    monkeypatch.setattr(config, "LP_LOAD_EXPENSIVE_BAND_QUANTILE", "p99", raising=False)
    assert opt_mod._expensive_band_quantile(_cosy_prices()) == ("p75", None)


# ── /load/expected ──────────────────────────────────────────────────────────


def _seed_cosy_rates(d: date) -> None:
    rows = []
    for h in range(24):
        for m in (0, 30):
            t = datetime(d.year, d.month, d.day, h, m, tzinfo=LON).astimezone(UTC)
            rows.append({"valid_from": _z(t), "valid_to": _z(t + timedelta(minutes=30)),
                         "value_inc_vat": _price(h)})
    db.save_agile_rates(rows, COSY)


def test_expected_load_by_band_uses_band_sum_quantiles_of_same_day_type():
    from src.analytics.load_expected import expected_load_by_band

    today = _recent_days(1, 1, start_ago=0)[0]  # most recent Tuesday (maybe today)
    _seed_cosy_rates(today)
    # History: 10 earlier Tuesdays — peak block 16-19 at 1.0 kW except two
    # cooking days at 3.0 kW; one Saturday with a huge peak that must NOT leak in.
    tuesdays = [d for d in _recent_days(1, 11, start_ago=1) if d < today][:10]
    for i, d in enumerate(tuesdays):
        _seed_day_loads(d, {16: 3.0 if i < 2 else 1.0, 17: 3.0 if i < 2 else 1.0,
                            18: 3.0 if i < 2 else 1.0}, outdoor_via="meteo")
    sat = _recent_days(5, 1, start_ago=1)[0]
    _seed_day_loads(sat, {16: 9.0, 17: 9.0, 18: 9.0}, outdoor_via="meteo")
    now = datetime(today.year, today.month, today.day, 14, 30, tzinfo=LON).astimezone(UTC)
    # Realised today: 13:00-14:30 at 2.0 kW.
    for h, m in ((13, 0), (13, 30), (14, 0)):
        t = datetime(today.year, today.month, today.day, h, m, tzinfo=LON).astimezone(UTC)
        db.save_pv_realtime_sample(_z(t), load_power_kw=2.0)
    db.save_pv_realtime_sample(_z(now), load_power_kw=2.0)

    r = expected_load_by_band(today, history_days=90, now_utc=now, use_cache=False)
    assert r["tariff_structure"] == "banded" and r["day_type"] == "weekday"
    labels = [(b["label"], b["start_local"]) for b in r["bands"]]
    assert labels == [("day", "00:00"), ("cheap", "04:00"), ("day", "07:00"), ("cheap", "13:00"),
                      ("peak", "16:00"), ("day", "19:00"), ("cheap", "22:00")]
    peak = r["bands"][4]
    assert peak["status"] == "upcoming" and peak["realised_kwh"] is None
    # 3 h × 1.0 kW ≈ 3.0 kWh on 8 days, ≈ 9.0 kWh on 2 days (the trapezoid
    # shares the last half-hour with the 19:00 sample, hence the tolerance) →
    # p50 at the quiet level, the two cooking days only in the top decile.
    assert peak["expected_kwh"]["n_days"] == 10
    assert peak["expected_kwh"]["p50"] == pytest.approx(3.0, abs=0.2)
    assert peak["expected_kwh"]["p50"] <= peak["expected_kwh"]["p75"] <= peak["expected_kwh"]["p90"]
    assert peak["expected_kwh"]["p90"] > 5.0
    assert peak["expected_kwh"]["max"] == pytest.approx(9.0, abs=0.7)
    assert peak["expected_kwh"]["max"] < 20.0  # the Saturday never leaks into a weekday
    cheap_pm = r["bands"][3]
    assert cheap_pm["status"] == "ongoing" and 0.4 < cheap_pm["progress"] < 0.6
    # realised so far: three completed half-hours at 2.0 kW = 3.0 kWh (the
    # ongoing 14:30 slot is excluded until it completes).
    assert cheap_pm["realised_kwh"] == pytest.approx(3.0, abs=0.1)
    assert r["day"]["expected_kwh"]["n_days"] == 10


def test_expected_load_fills_an_unpublished_day_from_the_band_profile():
    from src.analytics.load_expected import band_windows_for_day

    today = datetime.now(LON).date()
    _seed_cosy_rates(today)
    future = today + timedelta(days=5)  # no rates stored for it
    windows, kind = band_windows_for_day(future, LON)
    assert kind == "banded"
    assert [w.label for w in windows] == ["day", "cheap", "day", "cheap", "peak", "day", "cheap"]
    assert all(w.start_utc.astimezone(LON).date() == future for w in windows)


def test_expected_load_endpoint_shape(monkeypatch):
    from src.api.main import app

    today = datetime.now(LON).date()
    _seed_cosy_rates(today)
    client = TestClient(app)
    r = client.get("/api/v1/load/expected")
    assert r.status_code == 200
    body = r.json()
    assert body["tariff_display_name"] == "Cosy"
    assert body["tariff_structure"] == "banded"
    assert len(body["bands"]) == 7
    for b in body["bands"]:
        for k in ("key", "label", "start_local", "end_local", "price_p", "status", "progress",
                  "expected_kwh", "committed_kwh", "realised_kwh", "forecast_error_kwh"):
            assert k in b
    assert "day" in body and "expected_kwh" in body["day"]
    r2 = client.get("/api/v1/load/expected?date=2026-13-40")
    assert r2.status_code in (400, 422)


# ── review follow-ups: BST first hour, DST band fill, p90 clamp ─────────────


def test_expected_load_counts_the_first_local_hour_of_a_bst_day():
    """In BST, local 00:00–01:00 is 23:00Z of the previous day. The old per-UTC-day
    bucketing dropped it: the 00–04 band failed the coverage gate on every
    history day (n_days=0) and 'realised so far' was one hour short."""
    from src.analytics.load_expected import expected_load_by_band

    today = date(2026, 9, 29)  # Tuesday, BST
    _seed_cosy_rates(today)
    tuesdays = [today - timedelta(days=7 * k) for k in range(1, 9)]
    for d in tuesdays:
        _seed_day_loads(d, {h: 1.0 for h in range(0, 4)}, outdoor_via="meteo")
    now = datetime(2026, 9, 29, 3, 30, tzinfo=LON).astimezone(UTC)
    for h, m in ((0, 0), (0, 30), (1, 0), (1, 30), (2, 0), (2, 30), (3, 0)):
        t = datetime(2026, 9, 29, h, m, tzinfo=LON).astimezone(UTC)
        db.save_pv_realtime_sample(_z(t), load_power_kw=1.0)
    db.save_pv_realtime_sample(_z(now), load_power_kw=1.0)
    r = expected_load_by_band(today, history_days=90, now_utc=now, use_cache=False)
    night = r["bands"][0]
    assert (night["label"], night["start_local"]) == ("day", "00:00")
    assert night["expected_kwh"]["n_days"] == 8
    assert night["expected_kwh"]["p50"] == pytest.approx(4.0, abs=0.3)
    assert night["status"] == "ongoing"
    # 00:00 → 03:30 completed = 7 half-hours at 1 kW = 3.5 kWh (first hour INCLUDED).
    assert night["realised_kwh"] == pytest.approx(3.5, abs=0.1)
    assert r["day"]["realised_kwh"] == pytest.approx(3.5, abs=0.1)


@pytest.mark.parametrize(("day", "n_slots"), [(date(2027, 3, 28), 46), (date(2026, 10, 25), 50)])
def test_band_fill_for_unpublished_dst_day_has_the_real_slot_count(day, n_slots):
    from src.analytics.load_expected import band_windows_for_day

    _seed_cosy_rates(datetime.now(LON).date())  # the profile source; `day` itself has no rates
    windows, kind = band_windows_for_day(day, LON)
    assert kind == "banded"
    total = sum(int((w.end_utc - w.start_utc).total_seconds() // 1800) for w in windows)
    assert total == n_slots
    # Contiguous in UTC: no collapsed or duplicated hour.
    for a, b in zip(windows, windows[1:]):
        assert a.end_utc == b.start_utc
    assert windows[0].start_utc == datetime(day.year, day.month, day.day, tzinfo=LON).astimezone(UTC)


def test_history_scan_finds_n_same_type_days_for_a_weekend():
    from src.analytics.load_expected import _same_group_history_days

    sat = date(2026, 10, 10)
    days = _same_group_history_days(sat, 20)
    assert len(days) == 20 and all(d.weekday() >= 5 for d in days)
    wd = _same_group_history_days(date(2026, 10, 6), 20)
    assert len(wd) == 20 and all(d.weekday() < 5 for d in wd)
