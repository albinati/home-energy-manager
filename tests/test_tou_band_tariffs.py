"""Multi-band time-of-use tariffs (Cosy) — fetch + fair-compare pricing.

Two defects made Cosy unusable/invisible:

* ``_tariff_to_product`` only recognised AGILE codes, so a Cosy import code fell
  back to fetching AGILE-24-10-01 prices;
* Octopus publishes TOU prices as one row per BAND (a 3 h Cosy window), but every
  consumer keys prices by the 30-min slot ``valid_from`` — and fair-compare priced
  the single-register fallback at whichever band row the API listed first
  (Cosy Fixed came out as "14.57p all day", ~£34 under its real September cost).
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

import src.energy.octopus_products as op
from src.analytics.fair_compare import _price_flat_or_tou_day
from src.energy.tariff_models import PricingStructure, RateSchedule, TariffProduct
from src.scheduler.agile import _split_into_slots, _tariff_to_product

COSY, DAY, PEAK = 12.4868, 25.4461, 38.174


@pytest.mark.parametrize("code,product", [
    ("E-1R-AGILE-24-10-01-H", "AGILE-24-10-01"),
    ("E-1R-AGILE-OUTGOING-19-05-13-H", "AGILE-OUTGOING-19-05-13"),
    ("E-1R-COSY-22-12-08-H", "COSY-22-12-08"),
    ("E-1R-COSY-FIX-12M-26-09-26-H", "COSY-FIX-12M-26-09-26"),
    ("E-2R-GO-VAR-22-10-14-C", "GO-VAR-22-10-14"),
])
def test_tariff_to_product_is_product_agnostic(code, product):
    assert _tariff_to_product(code) == product


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


def test_band_row_expands_to_half_hour_slots():
    rows = [{"value_inc_vat": COSY, "valid_from": "2026-10-02T03:00:00Z",
             "valid_to": "2026-10-02T06:00:00Z"}]
    out = _split_into_slots(rows, _dt("2026-10-02T00:00"), _dt("2026-10-03T00:00"))
    assert [r["valid_from"] for r in out] == [
        f"2026-10-02T{h:02d}:{m:02d}:00Z" for h in (3, 4, 5) for m in (0, 30)
    ]
    assert all(r["value_inc_vat"] == COSY for r in out)
    assert out[-1]["valid_to"] == "2026-10-02T06:00:00Z"


def test_band_row_is_clipped_to_the_requested_window():
    # Band started before period_from; open-ended flat row runs past period_to.
    rows = [
        {"value_inc_vat": DAY, "valid_from": "2026-10-02T06:00:00Z", "valid_to": "2026-10-02T12:00:00Z"},
        {"value_inc_vat": 24.0, "valid_from": "2026-04-01T00:00:00Z", "valid_to": None},
    ]
    out = _split_into_slots(rows, _dt("2026-10-02T11:00"), _dt("2026-10-02T12:00"))
    assert [(r["valid_from"], r["value_inc_vat"]) for r in out] == [
        ("2026-10-02T11:00:00Z", DAY), ("2026-10-02T11:30:00Z", DAY),
        ("2026-10-02T11:00:00Z", 24.0), ("2026-10-02T11:30:00Z", 24.0),
    ]


def test_half_hourly_rows_pass_through_unchanged():
    rows = [{"value_inc_vat": 21.5, "valid_from": "2026-10-02T03:00:00Z",
             "valid_to": "2026-10-02T03:30:00Z"}]
    assert _split_into_slots(rows, _dt("2026-10-02T00:00"), _dt("2026-10-03T00:00")) == rows


def _cosy_bst_day(date: str) -> list[dict]:
    """One BST day of Cosy bands as the API lists them (newest first, UTC)."""
    nxt = "2026-10-03"
    bands = [
        (f"{date}T03:00", f"{date}T06:00", COSY), (f"{date}T06:00", f"{date}T12:00", DAY),
        (f"{date}T12:00", f"{date}T15:00", COSY), (f"{date}T15:00", f"{date}T18:00", PEAK),
        (f"{date}T18:00", f"{date}T21:00", DAY), (f"{date}T21:00", f"{date}T23:00", COSY),
        (f"{date}T23:00", f"{nxt}T03:00", DAY),
    ]
    return [{"valid_from": f + ":00Z", "valid_to": t + ":00Z", "value_inc_vat": v}
            for f, t, v in reversed(bands)]


def test_band_profile_maps_published_bands_to_local_clock(monkeypatch):
    monkeypatch.setattr(op, "_get_json", lambda url, timeout=10: {"results": _cosy_bst_day("2026-10-02")})
    prof = op._fetch_band_profile("COSY-22-12-08", "E-1R-COSY-22-12-08-H")
    assert prof is not None and len(prof) == 48
    # Local (BST) clock: cosy 04-07, 13-16, 22-00; peak 16-19; day otherwise.
    assert prof[4 * 60] == COSY and prof[6 * 60 + 30] == COSY
    assert prof[7 * 60] == DAY and prof[12 * 60 + 30] == DAY
    assert prof[13 * 60] == COSY and prof[16 * 60] == PEAK and prof[18 * 60 + 30] == PEAK
    assert prof[19 * 60] == DAY and prof[22 * 60] == COSY and prof[23 * 60 + 30] == COSY
    assert prof[0] == DAY and prof[3 * 60 + 30] == DAY


def test_band_profile_is_none_for_a_flat_tariff(monkeypatch):
    flat = [{"valid_from": "2026-10-01T23:00:00Z", "valid_to": "2026-10-02T23:00:00Z", "value_inc_vat": 24.0}]
    monkeypatch.setattr(op, "_get_json", lambda url, timeout=10: {"results": flat})
    assert op._fetch_band_profile("VAR-22-11-01", "E-1R-VAR-22-11-01-H") is None


def test_fair_compare_prices_each_slot_at_its_band(monkeypatch):
    monkeypatch.setattr(op, "_get_json", lambda url, timeout=10: {"results": _cosy_bst_day("2026-10-02")})
    t = TariffProduct(
        product_code="COSY-22-12-08", tariff_code="E-1R-COSY-22-12-08-H",
        display_name="Cosy Octopus", full_name="Cosy Octopus",
        pricing=PricingStructure.TIME_OF_USE,
        rates=RateSchedule(unit_rate_pence=COSY,  # the old "first row" trap
                           slot_rates_local=op._fetch_band_profile("COSY-22-12-08", "E-1R-COSY-22-12-08-H")),
    )
    bucket = {
        "2026-10-02T03:00:00Z": 1.0,  # 04:00 BST -> cosy
        "2026-10-02T08:00:00Z": 1.0,  # 09:00 BST -> day
        "2026-10-02T16:00:00Z": 1.0,  # 17:00 BST -> peak
    }
    cost, kwh = _price_flat_or_tou_day(t, bucket)
    assert kwh == pytest.approx(3.0)
    assert cost == pytest.approx(COSY + DAY + PEAK)
