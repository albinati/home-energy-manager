"""fair_compare: current-tariff standing charge prefers the live Octopus
catalogue over the stale MANUAL_STANDING_CHARGE config.

The configured MANUAL value drifted (59.26p) below the household's real live
AGILE-24-10-01 standing (62.22p), flattering Agile in every comparison by
~3p/day. The fair-compare current row must price with the live value when the
catalogue carries the household's product, and fall back to MANUAL only when it
doesn't (or the catalogue is offline).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.analytics.fair_compare import _current_standing_per_day
from src.config import config


def _product(code: str, standing: float):
    return SimpleNamespace(
        product_code=code,
        rates=SimpleNamespace(standing_charge_pence_per_day=standing),
    )


def test_prefers_live_catalogue_standing_for_current_product():
    cands = [_product("VAR-22-11-01", 44.35), _product("AGILE-24-10-01", 62.22)]
    assert _current_standing_per_day(cands, "AGILE-24-10-01") == pytest.approx(62.22)


def test_falls_back_to_manual_when_product_absent(monkeypatch):
    monkeypatch.setattr(config, "MANUAL_STANDING_CHARGE_PENCE_PER_DAY", 59.26, raising=False)
    cands = [_product("VAR-22-11-01", 44.35)]
    assert _current_standing_per_day(cands, "AGILE-24-10-01") == pytest.approx(59.26)


def test_falls_back_to_manual_when_catalogue_empty(monkeypatch):
    monkeypatch.setattr(config, "MANUAL_STANDING_CHARGE_PENCE_PER_DAY", 59.26, raising=False)
    assert _current_standing_per_day([], "AGILE-24-10-01") == pytest.approx(59.26)


def test_zero_or_missing_catalogue_standing_falls_back_to_manual(monkeypatch):
    # A catalogue row that exists but reports 0 standing isn't a real value.
    monkeypatch.setattr(config, "MANUAL_STANDING_CHARGE_PENCE_PER_DAY", 59.26, raising=False)
    cands = [_product("AGILE-24-10-01", 0.0)]
    assert _current_standing_per_day(cands, "AGILE-24-10-01") == pytest.approx(59.26)
