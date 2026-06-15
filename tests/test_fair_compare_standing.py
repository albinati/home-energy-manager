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


# --- current_import_standing_pence (cached SoT shared with pnl) --------------

import src.analytics.fair_compare as fc


@pytest.fixture(autouse=True)
def _reset_standing_cache():
    fc._STANDING_CACHE["value"] = 0.0
    fc._STANDING_CACHE["ts"] = 0.0
    yield
    fc._STANDING_CACHE["value"] = 0.0
    fc._STANDING_CACHE["ts"] = 0.0


def test_current_import_standing_prefers_live_catalogue(monkeypatch):
    monkeypatch.setattr(config, "OCTOPUS_TARIFF_CODE", "E-1R-AGILE-24-10-01-A", raising=False)
    monkeypatch.setattr(config, "MANUAL_STANDING_CHARGE_PENCE_PER_DAY", 59.26, raising=False)
    monkeypatch.setattr(
        "src.energy.octopus_products.get_available_tariffs",
        lambda **_: [_product("VAR-22-11-01", 44.35), _product("AGILE-24-10-01", 62.22)],
    )
    assert fc.current_import_standing_pence() == pytest.approx(62.22)


def test_current_import_standing_caches_within_ttl(monkeypatch):
    monkeypatch.setattr(config, "OCTOPUS_TARIFF_CODE", "E-1R-AGILE-24-10-01-A", raising=False)
    calls = {"n": 0}

    def _fetch(**_):
        calls["n"] += 1
        return [_product("AGILE-24-10-01", 62.22)]

    monkeypatch.setattr("src.energy.octopus_products.get_available_tariffs", _fetch)
    assert fc.current_import_standing_pence() == pytest.approx(62.22)
    assert fc.current_import_standing_pence() == pytest.approx(62.22)
    assert calls["n"] == 1  # second call served from cache, no second fetch


def test_current_import_standing_refetches_after_ttl_expiry(monkeypatch):
    monkeypatch.setattr(config, "OCTOPUS_TARIFF_CODE", "E-1R-AGILE-24-10-01-A", raising=False)
    calls = {"n": 0}

    def _fetch(**_):
        calls["n"] += 1
        return [_product("AGILE-24-10-01", 62.22)]

    monkeypatch.setattr("src.energy.octopus_products.get_available_tariffs", _fetch)
    assert fc.current_import_standing_pence() == pytest.approx(62.22)
    assert calls["n"] == 1
    # Age the cached entry past the TTL → next call re-fetches.
    fc._STANDING_CACHE["ts"] -= fc.STANDING_CACHE_TTL_SECONDS + 1
    assert fc.current_import_standing_pence() == pytest.approx(62.22)
    assert calls["n"] == 2


def test_current_import_standing_offline_falls_back_without_caching(monkeypatch):
    monkeypatch.setattr(config, "OCTOPUS_TARIFF_CODE", "E-1R-AGILE-24-10-01-A", raising=False)
    monkeypatch.setattr(config, "MANUAL_STANDING_CHARGE_PENCE_PER_DAY", 59.26, raising=False)

    def _boom(**_):
        raise RuntimeError("octopus offline")

    monkeypatch.setattr("src.energy.octopus_products.get_available_tariffs", _boom)
    assert fc.current_import_standing_pence() == pytest.approx(59.26)
    # Fallback is NOT cached — a recovered catalogue is picked up next call.
    monkeypatch.setattr(
        "src.energy.octopus_products.get_available_tariffs",
        lambda **_: [_product("AGILE-24-10-01", 62.22)],
    )
    assert fc.current_import_standing_pence() == pytest.approx(62.22)
