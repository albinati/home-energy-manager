"""#900 — an owner write beats a stuck in-memory override; applying a setting or
a replan drops the 60 s /plan/fronts response cache."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src import runtime_settings as rts
from src import vacation
from src.analytics import plan_fronts as pf
from src.config import config


@pytest.fixture(autouse=True)
def _db():
    from src import db

    db.init_db()


@pytest.fixture
def client(monkeypatch):
    from src.api.main import app

    monkeypatch.setattr(config, "HEM_UI_AUTH_REQUIRED", False, raising=False)
    monkeypatch.setenv("REQUIRE_SIMULATION_ID", "false")
    yield TestClient(app)
    config.clear_override("INDOOR_SETPOINT_C")


def test_persist_setting_clears_any_override():
    config.INDOOR_SETPOINT_C = 20.0   # what a Workbench what-if leaves behind
    assert config.has_override("INDOOR_SETPOINT_C")
    vacation.persist_setting("INDOOR_SETPOINT_C", 21.5)
    assert not config.has_override("INDOOR_SETPOINT_C")
    assert config.INDOOR_SETPOINT_C == 21.5


def test_batch_apply_wins_over_override_and_drops_fronts_cache(client):
    config.INDOOR_SETPOINT_C = 20.0
    pf._cache[("x", "2026-01-01")] = (1e18, {"stale": True})
    r = client.post("/api/v1/settings/batch", json={"changes": {"INDOOR_SETPOINT_C": 22.0}})
    assert r.status_code == 200, r.text
    assert rts.get_setting("INDOOR_SETPOINT_C") == 22.0
    assert config.INDOOR_SETPOINT_C == 22.0
    assert pf._cache == {}


def test_put_drops_fronts_cache(client):
    pf._cache[("x", "d")] = (1e18, {})
    r = client.put("/api/v1/settings/INDOOR_SETPOINT_C", json={"value": 21.5})
    assert r.status_code == 200, r.text
    assert config.INDOOR_SETPOINT_C == 21.5
    assert pf._cache == {}


def test_propose_drops_fronts_cache(client, monkeypatch):
    from src.api import main as m

    monkeypatch.setattr(config, "OCTOPUS_TARIFF_CODE", "X", raising=False)
    monkeypatch.setattr(m, "_run_optimizer_serialized", lambda fox, x: {"ok": True, "strategy": "s"})
    monkeypatch.setattr(m, "FoxESSClient", lambda **kw: object())
    pf._cache[("x", "d")] = (1e18, {})
    r = client.post("/api/v1/optimization/propose")
    assert r.status_code == 200, r.text
    assert pf._cache == {}


def test_invalidate_cache_is_idempotent():
    pf.invalidate_cache()
    pf.invalidate_cache()
    assert pf._cache == {}
