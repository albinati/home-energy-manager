"""Phase 2 — GET /api/v1/cockpit/now aggregator contract.

The hero panel reads from a single coherent snapshot instead of four
parallel fetches. This test pins the payload shape so frontend refactors
don't silently break it. Functional correctness (price matching, current
fox-group detection) is exercised in other layers — here we just confirm
the contract and that the endpoint NEVER raises even when every upstream
source is cold.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src import db
from src.api.main import app


@pytest.fixture(autouse=True)
def _init_db():
    db.init_db()


@pytest.fixture
def client():
    return TestClient(app)


def test_cockpit_now_shape(client):
    r = client.get("/api/v1/cockpit/now")
    assert r.status_code == 200
    body = r.json()

    # Top-level keys.
    assert set(body.keys()) == {
        "now_utc", "planner_tz", "current_slot", "next_transition",
        "state", "freshness", "thresholds", "modes", "plan_date", "_legend",
    }
    # _legend disambiguates sign conventions for LLM consumers (OpenClaw etc.).
    # The signed fields MUST carry a description so a negative value cannot be
    # misread as an unsigned magnitude — see the 2026-05-03 OpenClaw "Daikin
    # off" misread audit for context.
    legend = body["_legend"]
    assert "IMPORTING" in legend["grid_kw"] and "EXPORTING" in legend["grid_kw"]
    assert "CHARGING" in legend["battery_kw"] and "DISCHARGING" in legend["battery_kw"]

    # current_slot always has the 5-key shape.
    assert set(body["current_slot"].keys()) == {
        "t_utc", "t_end_utc", "price_import_p", "price_export_p", "fox_mode",
    }

    # state has the fields the hero panel reads (indoor = rich sensor snapshot).
    assert set(body["state"].keys()) == {
        "soc_pct", "soc_kwh", "solar_kw", "load_kw", "grid_kw", "battery_kw",
        "fox_mode", "tank_c", "indoor_c", "outdoor_c", "lwt_c", "daikin_mode",
        "indoor",
    }

    # freshness — one block per source (indoor sensors #540 W1).
    assert set(body["freshness"].keys()) == {"agile", "fox", "daikin", "plan", "indoor"}
    for block in body["freshness"].values():
        assert set(block.keys()) == {"fetched_at_utc", "age_s", "stale"}

    # thresholds.
    assert set(body["thresholds"].keys()) == {"cheap_p", "peak_p"}

    # modes.
    assert set(body["modes"].keys()) == {
        "daikin_control_mode", "optimization_preset", "energy_strategy_mode",
    }


def test_cockpit_now_returns_iso_utc_and_planner_tz(client):
    r = client.get("/api/v1/cockpit/now")
    body = r.json()
    assert body["now_utc"].endswith("Z")
    assert isinstance(body["planner_tz"], str) and len(body["planner_tz"]) > 0


def test_cockpit_now_survives_cold_state(client):
    # With no cached Fox/Daikin/Octopus data, the endpoint should still 200 and
    # return None in all state slots rather than raising.
    r = client.get("/api/v1/cockpit/now")
    assert r.status_code == 200
    body = r.json()
    for key in ("soc_pct", "soc_kwh", "solar_kw", "load_kw", "tank_c", "indoor_c"):
        # Either populated (if the test env has live caches) or None.
        assert body["state"][key] is None or isinstance(body["state"][key], (int, float))


def test_cockpit_now_never_triggers_cloud_calls(client):
    # Contract: this endpoint MUST be cache-only. If it ever goes async-heavy
    # on cloud calls the quota pills would burn silently. A smoke check on
    # response time guards the worst regressions.
    import time
    t0 = time.monotonic()
    r = client.get("/api/v1/cockpit/now")
    elapsed = time.monotonic() - t0
    assert r.status_code == 200
    # Cold call should be well under a second — no network waits allowed.
    assert elapsed < 1.5, f"endpoint took {elapsed:.2f}s — possible cloud call leak"


# ---------------------------------------------------------------------------
# #674 — current_slot.fox_mode must be derived in LOCAL wall-clock, not UTC.
#
# Scheduler V3 groups are written in BULLETPROOF_TIMEZONE wall-clock
# (scheduler/optimizer.py TZ()), but the old cockpit_now block compared them
# against UTC "now" — one hour off all BST (coincidentally correct in winter).
# The block now delegates to the shared #672 derivation
# (foxess.service.derive_fox_mode_from_schedule) via _fox_plan_fields, with
# the "schedule:" prefix stripped so the response keeps the bare workMode
# contract the SPA string-matches on (landing.tsx: "selfuse" etc.).
# All datetimes are FIXED — no date-relative flakes.
# ---------------------------------------------------------------------------

def _group(sh, sm, eh, em, mode):
    return {
        "startHour": sh, "startMinute": sm,
        "endHour": eh, "endMinute": em,
        "workMode": mode,
        "extraParam": {"minSocOnGrid": 10},
    }


def _pin_state(monkeypatch, groups, enabled=True, uploaded_at="2026-07-08T12:00:00Z"):
    monkeypatch.setattr(
        db, "get_latest_fox_schedule_state",
        lambda: {"groups": groups, "enabled": 1 if enabled else 0,
                 "uploaded_at": uploaded_at},
    )


def test_fox_plan_fields_bst_afternoon_uses_local_clock(monkeypatch):
    """THE #674 regression: 16:34 UTC on a July day is 17:34 BST. A group
    covering 17:00–17:59 local covers *now* — the old UTC comparison saw
    (16, 34) and reported nothing (None / the wrong group)."""
    from datetime import UTC, datetime

    from src.api.main import _fox_plan_fields
    from src.config import config as _cfg

    monkeypatch.setattr(_cfg, "BULLETPROOF_TIMEZONE", "Europe/London", raising=False)
    _pin_state(monkeypatch, [
        _group(17, 0, 17, 59, "ForceDischarge"),
        _group(18, 0, 18, 59, "ForceCharge"),
    ])
    fields = _fox_plan_fields(datetime(2026, 7, 8, 16, 34, tzinfo=UTC))

    # Bare workMode, no "schedule:" prefix — the SPA contract.
    assert fields["current_fox_mode"] == "ForceDischarge"
    # Next transition is the 18:00 BST group start, reported in UTC (17:00Z).
    assert fields["next_fox_mode"] == "ForceCharge"
    assert fields["next_transition_utc"] == "2026-07-08T17:00:00Z"
    assert fields["uploaded_at"] == "2026-07-08T12:00:00Z"


def test_fox_plan_fields_winter_unchanged(monkeypatch):
    """In GMT (local == UTC) the derivation matches the old behaviour."""
    from datetime import UTC, datetime

    from src.api.main import _fox_plan_fields
    from src.config import config as _cfg

    monkeypatch.setattr(_cfg, "BULLETPROOF_TIMEZONE", "Europe/London", raising=False)
    _pin_state(monkeypatch, [_group(17, 0, 17, 59, "ForceDischarge")])
    fields = _fox_plan_fields(datetime(2026, 1, 8, 17, 34, tzinfo=UTC))
    assert fields["current_fox_mode"] == "ForceDischarge"


def test_fox_plan_fields_no_group_covering_now_is_selfuse(monkeypatch):
    """No group over now → the inverter's global default (SelfUse), per the
    shared helper's semantics. Still bare-named for the SPA (its resting-state
    list includes "selfuse")."""
    from datetime import UTC, datetime

    from src.api.main import _fox_plan_fields
    from src.config import config as _cfg

    monkeypatch.setattr(_cfg, "BULLETPROOF_TIMEZONE", "Europe/London", raising=False)
    _pin_state(monkeypatch, [_group(2, 0, 4, 59, "ForceCharge")])
    fields = _fox_plan_fields(datetime(2026, 7, 8, 16, 34, tzinfo=UTC))
    assert fields["current_fox_mode"] == "SelfUse"
    assert fields["next_fox_mode"] is None       # no group starts later today
    assert fields["next_transition_utc"] is None


def test_fox_plan_fields_scheduler_disabled(monkeypatch):
    """Scheduler flag off → groups not in force: firmware default SelfUse and
    NO next transition (advertising a disabled group's start would be a lie)."""
    from datetime import UTC, datetime

    from src.api.main import _fox_plan_fields
    from src.config import config as _cfg

    monkeypatch.setattr(_cfg, "BULLETPROOF_TIMEZONE", "Europe/London", raising=False)
    _pin_state(monkeypatch, [
        _group(17, 0, 17, 59, "ForceDischarge"),
        _group(18, 0, 18, 59, "ForceCharge"),
    ], enabled=False)
    fields = _fox_plan_fields(datetime(2026, 7, 8, 16, 34, tzinfo=UTC))
    assert fields["current_fox_mode"] == "SelfUse"
    assert fields["next_fox_mode"] is None
    assert fields["next_transition_utc"] is None


def test_fox_plan_fields_no_state_is_none(monkeypatch):
    """No schedule ever uploaded — helper says "unknown", the API keeps its
    historical contract of null (no pill in the SPA)."""
    from datetime import UTC, datetime

    from src.api.main import _fox_plan_fields

    monkeypatch.setattr(db, "get_latest_fox_schedule_state", lambda: None)
    fields = _fox_plan_fields(datetime(2026, 7, 8, 16, 34, tzinfo=UTC))
    assert fields["current_fox_mode"] is None
    assert fields["next_fox_mode"] is None
    assert fields["next_transition_utc"] is None
    assert fields["uploaded_at"] is None


def test_cockpit_now_fox_mode_bare_name_end_to_end(monkeypatch, tmp_path):
    """API-level: a persisted all-day group surfaces as its bare workMode in
    current_slot.fox_mode (deterministic at any clock time — full coverage)."""
    from src.config import config as _cfg

    monkeypatch.setattr(_cfg, "DB_PATH", str(tmp_path / "fm.db"), raising=False)
    db.init_db()
    db.save_fox_schedule_state([_group(0, 0, 23, 59, "ForceCharge")], enabled=True)

    body = TestClient(app).get("/api/v1/cockpit/now").json()
    assert body["current_slot"]["fox_mode"] == "ForceCharge"
    assert body["freshness"]["plan"]["fetched_at_utc"] is not None


def test_cockpit_now_fox_mode_scheduler_disabled_end_to_end(monkeypatch, tmp_path):
    from src.config import config as _cfg

    monkeypatch.setattr(_cfg, "DB_PATH", str(tmp_path / "fm2.db"), raising=False)
    db.init_db()
    db.save_fox_schedule_state([_group(0, 0, 23, 59, "ForceCharge")], enabled=False)

    body = TestClient(app).get("/api/v1/cockpit/now").json()
    assert body["current_slot"]["fox_mode"] == "SelfUse"
    assert body["next_transition"]["new_fox_mode"] is None


def test_cockpit_now_indoor_from_sensor(monkeypatch, tmp_path):
    """#540 W1 — the freshest room-sensor reading is folded into the consolidated
    snapshot (same path as Fox/tank) and drives state.indoor_c. Daikin never
    sourced indoor (no room stat)."""
    from datetime import UTC, datetime

    from src.config import config as _cfg

    monkeypatch.setattr(_cfg, "DB_PATH", str(tmp_path / "cn.db"), raising=False)
    db.init_db()
    z = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    db.save_device_reading_log([
        {"captured_at": z, "temp_c": 23.5, "humidity_pct": 61, "mac": "AA", "room": "sala"},
    ])

    body = TestClient(app).get("/api/v1/cockpit/now").json()
    ind = body["state"]["indoor"]
    assert ind is not None and ind["n_rooms"] == 1
    assert ind["mean_c"] == 23.5
    assert ind["rooms"][0]["room"] == "sala"
    assert body["state"]["indoor_c"] == 23.5          # sensor drives indoor_c
    assert body["freshness"]["indoor"]["stale"] is False
