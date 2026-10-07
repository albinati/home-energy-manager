"""Band-aware tariff structure (#803 / #804) — Cosy switch.

Every price classifier in HEM was percentile-based (Agile: 48 levels/day). On
a 3-band Cosy day the 75th percentile IS the day band, so the whole day band
read as "peak". ``energy.tariff_structure`` is the one place that decides
banded vs dynamic and derives MIDPOINT thresholds on a banded tariff so each
existing ``<`` / ``<=`` / ``>`` / ``>=`` consumer classifies the bands right.
Agile (dynamic) must stay bit-for-bit identical.
"""
from __future__ import annotations

import random
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src import db
from src.config import config
from src.energy import tariff_structure as ts

COSY_CHEAP, COSY_DAY, COSY_PEAK = 12.4868, 25.4461, 38.174
TZ = ZoneInfo("Europe/London")


def _cosy_day_prices() -> list[float]:
    """48 slots in LOCAL order: cheap 04-07, 13-16, 22-24; peak 16-19; day otherwise."""
    out = []
    for h in range(24):
        for _m in (0, 30):
            if 4 <= h < 7 or 13 <= h < 16 or h >= 22:
                out.append(COSY_CHEAP)
            elif 16 <= h < 19:
                out.append(COSY_PEAK)
            else:
                out.append(COSY_DAY)
    return out


def _agile_day(seed: int) -> list[float]:
    rnd = random.Random(seed)
    return [round(rnd.uniform(-3.0, 40.0), 2) for _ in range(48)]


@pytest.fixture(autouse=True)
def _auto_mode(monkeypatch):
    monkeypatch.setattr(config, "OCTOPUS_TARIFF_STRUCTURE", "auto", raising=False)
    monkeypatch.setattr(config, "OPTIMIZATION_PEAK_THRESHOLD_PENCE", 25.0)


# ── detection ────────────────────────────────────────────────────────────────


def test_detect_cosy_three_levels_is_banded():
    s = ts.detect(_cosy_day_prices())
    assert s.is_banded
    assert s.levels == (12.49, 25.45, 38.17)
    assert s.cheap_level == 12.49 and s.peak_level == 38.17
    # midpoints: 18.97 / 31.81 — cheap below / peak above regardless of < vs <=
    assert s.cheap_thr == pytest.approx((12.49 + 25.45) / 2, abs=1e-3)
    assert s.peak_thr == pytest.approx((25.45 + 38.17) / 2, abs=1e-3)
    assert COSY_CHEAP < s.cheap_thr < COSY_DAY < s.peak_thr < COSY_PEAK


def test_detect_agile_day_is_dynamic():
    s = ts.detect(_agile_day(1))
    assert s.kind == "dynamic" and not s.is_banded
    assert s.levels == ()


def test_detect_short_horizon_stays_dynamic():
    assert ts.detect(_cosy_day_prices()[:8]).kind == "dynamic"


def test_two_level_go_shape_has_cheap_no_peak():
    """4 h off-peak at 8.5p, 20 h at 27p: the MAJORITY level is standard, the
    minority (cheaper) is cheap — nothing to avoid."""
    prices = [8.5] * 8 + [27.0] * 40
    s = ts.detect(prices)
    assert s.is_banded and s.has_cheap and not s.has_peak
    assert ts.classify(prices).count("cheap") == 8
    assert ts.classify(prices).count("peak") == 0


def test_two_level_evening_block_is_a_peak_not_a_cheap_day():
    """Flat 12p day with a 3 h 32p evening: the minority (dearer) level is the
    peak; the majority 12p is standard, NOT 'cheap' (nothing to prefer)."""
    prices = [12.0] * 42 + [32.0] * 6
    s = ts.detect(prices)
    assert s.has_peak and not s.has_cheap
    assert ts.classify(prices).count("peak") == 6
    assert ts.classify(prices).count("cheap") == 0


def test_flat_tariff_has_no_cheap_no_peak():
    prices = [24.5] * 48
    s = ts.detect(prices)
    assert s.is_banded and not s.has_cheap and not s.has_peak
    assert set(ts.classify(prices)) == {"standard"}
    # thresholds pushed OUTSIDE the range so no comparison can match
    assert s.cheap_thr < 24.5 < s.peak_thr


def test_contrast_gate_blocks_a_noise_level_from_becoming_peak():
    """3 levels but the top is only 5 % above the middle → not a peak."""
    prices = [10.0] * 8 + [25.0] * 34 + [26.0] * 6
    s = ts.detect(prices)
    assert s.has_cheap and not s.has_peak


# ── thresholds: Agile bit-identical ──────────────────────────────────────────


@pytest.mark.parametrize("seed", range(20))
def test_thresholds_bit_identical_to_percentile_for_dynamic(seed):
    prices = _agile_day(seed)
    sorted_p = sorted(prices)
    n = len(sorted_p)
    # the LP's historical formula
    lp_cheap = sorted_p[max(0, n // 4 - 1)]
    lp_peak = sorted_p[min(n - 1, (3 * n) // 4)]
    assert ts.thresholds(prices, dynamic_rule="lp") == (lp_cheap, lp_peak)
    # the legacy classifier's historical formula
    mean_p = sum(sorted_p) / n
    leg_cheap = min(mean_p * 0.85, lp_cheap)
    leg_peak = max(lp_peak, 25.0)
    assert ts.thresholds(prices, dynamic_rule="legacy") == (leg_cheap, leg_peak)
    # and legacy classify() semantics: <= 0 negative, strict < cheap, strict > peak
    kinds = ts.classify(prices, dynamic_rule="legacy")
    for p, k in zip(prices, kinds):
        exp = "negative" if p <= 0 else "cheap" if p < leg_cheap else "peak" if p > leg_peak else "standard"
        assert k == exp


def test_classify_cosy_slots_by_band():
    kinds = ts.classify(_cosy_day_prices())
    assert kinds.count("cheap") == 16
    assert kinds.count("standard") == 26
    assert kinds.count("peak") == 6
    # the heartbeat uses strict '<' / '>' against stored thresholds — midpoints
    # make that correct too (this was the "never cheap" defect).
    s = ts.detect(_cosy_day_prices())
    assert COSY_CHEAP < s.cheap_thr and COSY_PEAK > s.peak_thr and not (COSY_DAY > s.peak_thr)


def test_env_override_dynamic_disables_banding(monkeypatch):
    """Kill switch: the pre-#804 percentile path — documents the defect it
    reinstates (0 cheap slots on a Cosy day under the strict legacy rule)."""
    monkeypatch.setattr(config, "OCTOPUS_TARIFF_STRUCTURE", "dynamic", raising=False)
    s = ts.detect(_cosy_day_prices())
    assert s.kind == "dynamic"
    assert ts.classify(_cosy_day_prices()).count("cheap") == 0


# ── local-clock band profile (horizon filler) ────────────────────────────────


def _seed_cosy_rows(code: str, days: list[date]) -> None:
    rows = []
    for d in days:
        for h in range(24):
            for m in (0, 30):
                s_loc = datetime(d.year, d.month, d.day, h, m, tzinfo=TZ)
                s_utc = s_loc.astimezone(UTC)
                if 4 <= h < 7 or 13 <= h < 16 or h >= 22:
                    p = COSY_CHEAP
                elif 16 <= h < 19:
                    p = COSY_PEAK
                else:
                    p = COSY_DAY
                rows.append({
                    "valid_from": s_utc.isoformat().replace("+00:00", "Z"),
                    "valid_to": (s_utc + timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
                    "value_inc_vat": p,
                })
    db.save_agile_rates(rows, code)


def test_band_profile_local_crosses_dst_fallback():
    """Rows spanning the 2026-10-25 BST→GMT change: 04:00 LOCAL is cheap on
    both sides even though its UTC hour shifts (03:00Z → 04:00Z)."""
    db.init_db()
    code = "E-1R-COSY-22-12-08-H"
    _seed_cosy_rows(code, [date(2026, 10, 24), date(2026, 10, 25), date(2026, 10, 26)])
    prof = ts.band_profile_local(code, now_utc=datetime(2026, 10, 25, 12, 0, tzinfo=UTC), window_days=3)
    assert prof[(4, 0)] == pytest.approx(COSY_CHEAP, abs=0.01)
    assert prof[(6, 30)] == pytest.approx(COSY_CHEAP, abs=0.01)
    assert prof[(16, 0)] == pytest.approx(COSY_PEAK, abs=0.01)
    assert prof[(18, 30)] == pytest.approx(COSY_PEAK, abs=0.01)
    assert prof[(7, 0)] == pytest.approx(COSY_DAY, abs=0.01)
    assert prof[(22, 0)] == pytest.approx(COSY_CHEAP, abs=0.01)
    assert len(prof) == 48


def test_band_profile_local_empty_for_agile_family_even_when_flat():
    """A flat series stored under an AGILE code must NOT become a repeating
    filler — the family gate keeps the Agile prior path."""
    db.init_db()
    code = "E-1R-AGILE-TEST-A"
    base = datetime(2026, 10, 20, 0, 0, tzinfo=UTC)
    rows = [{
        "valid_from": (base + timedelta(minutes=30 * i)).isoformat().replace("+00:00", "Z"),
        "valid_to": (base + timedelta(minutes=30 * (i + 1))).isoformat().replace("+00:00", "Z"),
        "value_inc_vat": 10.0,
    } for i in range(96)]
    db.save_agile_rates(rows, code)
    assert ts.band_profile_local(code, now_utc=base + timedelta(days=1), window_days=3) == {}


def test_band_profile_local_empty_without_code():
    assert ts.band_profile_local("") == {}


# ── display name ─────────────────────────────────────────────────────────────


def test_display_name_from_code(monkeypatch):
    monkeypatch.setattr(config, "TARIFF_DISPLAY_NAME", "", raising=False)
    assert ts.display_name("E-1R-COSY-22-12-08-H") == "Cosy"
    assert ts.display_name("E-1R-AGILE-24-10-01-H") == "Agile"
    assert ts.display_name("E-2R-VAR-22-11-01-H") == "Flexible"
    monkeypatch.setattr(config, "TARIFF_DISPLAY_NAME", "Casa", raising=False)
    assert ts.display_name("E-1R-COSY-22-12-08-H") == "Casa"


# ── review F1: short Agile windows with repeated prices ─────────────────────


def test_configured_agile_code_never_bands(monkeypatch):
    """With an Agile code configured, even a Cosy-shaped series is dynamic."""
    monkeypatch.setattr(config, "OCTOPUS_TARIFF_CODE", "E-1R-AGILE-24-10-01-H")
    assert ts.detect(_cosy_day_prices()).kind == "dynamic"
    monkeypatch.setattr(config, "OCTOPUS_TARIFF_CODE", "E-1R-COSY-22-12-08-H")
    assert ts.detect(_cosy_day_prices()).is_banded


def test_sparse_levels_short_agile_window_stays_dynamic(monkeypatch):
    """16 slots of 8 hourly-paired Agile prices passed the raw-level gate;
    the density guard (≥ 3 slots per level) keeps it dynamic."""
    monkeypatch.setattr(config, "OCTOPUS_TARIFF_CODE", "")
    paired = [19.0, 19.0, 21.5, 21.5, 24.0, 24.0, 27.0, 27.0, 30.0, 30.0, 32.0, 32.0, 34.0, 34.0, 36.0, 36.0]
    assert ts.detect(paired).kind == "dynamic"
    overnight = [13.1, 13.1, 13.4, 13.4, 13.6, 13.6, 13.9, 13.9, 14.0, 14.0, 14.2, 14.2]
    assert ts.detect(overnight).kind == "dynamic"


def test_band_profile_survives_two_reprices_in_window():
    """Three price sets in the stored window: the profile is detected on the
    48 latest-per-bucket values, so it stays banded and uses the newest set."""
    db.init_db()
    code = "E-1R-COSY-22-12-08-H"
    rows = []
    for k, d in enumerate([date(2026, 12, 29), date(2026, 12, 31), date(2027, 1, 2)]):
        f = 1.0 + 0.03 * k
        for h in range(24):
            for m in (0, 30):
                s_loc = datetime(d.year, d.month, d.day, h, m, tzinfo=TZ)
                s_utc = s_loc.astimezone(UTC)
                p = (COSY_CHEAP if (4 <= h < 7 or 13 <= h < 16 or h >= 22) else COSY_PEAK if 16 <= h < 19 else COSY_DAY) * f
                rows.append({
                    "valid_from": s_utc.isoformat().replace("+00:00", "Z"),
                    "valid_to": (s_utc + timedelta(minutes=30)).isoformat().replace("+00:00", "Z"),
                    "value_inc_vat": round(p, 4),
                })
    db.save_agile_rates(rows, code)
    prof = ts.band_profile_local(code, now_utc=datetime(2027, 1, 2, 12, tzinfo=UTC), window_days=7)
    assert len(prof) == 48
    assert prof[(4, 0)] == pytest.approx(COSY_CHEAP * 1.06, abs=0.01)
    assert prof[(16, 0)] == pytest.approx(COSY_PEAK * 1.06, abs=0.01)
