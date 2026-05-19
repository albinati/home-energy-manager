"""When the LP solver returns Infeasible (or any non-Optimal status), the
``_run_optimizer_lp`` path must HOLD the previously-uploaded Fox V3 schedule
and Daikin actions rather than calling the heuristic classifier.

Background: the heuristic builds Fox V3 ForceCharge groups from a slot list
with no LP-derived ``lp_grid_import_w`` / ``target_soc_pct`` hints. The
``_slot_fox_tuple`` defaults (``fdPwr=3000 W``, ``fdSoc=95``) then ship to
the inverter, which grid-charges aggressively until 95 % SoC — empirically
+£0.35-1.30/day vs the LP objective on every day a fallback fired during
the 2026-05-18 audit. Holding the previous (last-successful-LP) schedule is
strictly safer: Fox V3 is daily-cyclic, so the previous plan remains in
effect on the inverter until the next successful solve overwrites it.

Explicit ``OPTIMIZER_BACKEND=heuristic`` is a separate code path and is
unaffected by this change — see ``test_heuristic_fallback.py`` for that.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from src import db
from src.config import config as app_config
from src.scheduler import optimizer
from src.scheduler.lp_optimizer import LpPlan

TARIFF = "E-1R-AGILE-TEST-LP-HOLD"


@pytest.fixture(autouse=True)
def _init_db() -> None:
    db.init_db()


@pytest.fixture(autouse=True)
def _lp_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_config, "BULLETPROOF_TIMEZONE", "Europe/London")
    monkeypatch.setattr(app_config, "OCTOPUS_TARIFF_CODE", TARIFF)
    monkeypatch.setattr(app_config, "OPTIMIZER_BACKEND", "lp")
    monkeypatch.setattr(app_config, "OPENCLAW_READ_ONLY", True)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _seed_realistic_day(start: datetime) -> None:
    rows = []
    vf = start
    for _ in range(48):
        if 1 <= vf.hour < 5:
            price = -2.0
        elif 5 <= vf.hour < 16:
            price = 10.0
        elif 16 <= vf.hour < 19:
            price = 35.0
        else:
            price = 12.0
        vt = vf + timedelta(minutes=30)
        rows.append({"valid_from": _iso(vf), "valid_to": _iso(vt), "value_inc_vat": price})
        vf = vt
    db.save_agile_rates(rows, TARIFF)


def _stub_infeasible_solve(*_args: Any, **_kwargs: Any) -> LpPlan:
    return LpPlan(ok=False, status="Infeasible", objective_pence=0.0)


def test_lp_infeasible_returns_hold_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    """LP infeasible → return dict marks ``fallback="hold_previous_schedule"``."""
    now = datetime(2026, 4, 22, 18, 0, tzinfo=UTC)
    monkeypatch.setattr(optimizer, "_now_utc", lambda: now)
    _seed_realistic_day(datetime(2026, 4, 22, 0, 0, tzinfo=UTC))

    monkeypatch.setattr("src.scheduler.lp_optimizer.solve_lp", _stub_infeasible_solve)

    result = optimizer.run_optimizer(fox=None, daikin=None)
    assert result["ok"] is False
    assert result["fallback"] == "hold_previous_schedule"
    assert result["error"] == "LP Infeasible"
    assert result["optimizer_backend"] == "lp"


def test_lp_infeasible_does_not_call_heuristic(monkeypatch: pytest.MonkeyPatch) -> None:
    """LP infeasible must NOT invoke ``_run_optimizer_heuristic``.

    This is the regression we're protecting against — the heuristic ships
    ForceCharge groups with non-LP-aware defaults that overcharge from grid.
    """
    now = datetime(2026, 4, 22, 18, 0, tzinfo=UTC)
    monkeypatch.setattr(optimizer, "_now_utc", lambda: now)
    _seed_realistic_day(datetime(2026, 4, 22, 0, 0, tzinfo=UTC))

    monkeypatch.setattr("src.scheduler.lp_optimizer.solve_lp", _stub_infeasible_solve)

    heuristic_calls: list[Any] = []

    def _fail_heuristic(*args: Any, **kwargs: Any) -> dict[str, Any]:
        heuristic_calls.append((args, kwargs))
        raise AssertionError(
            "_run_optimizer_heuristic must not be invoked on LP infeasible "
            "(would emit bad Fox V3 ForceCharge groups)"
        )

    monkeypatch.setattr(optimizer, "_run_optimizer_heuristic", _fail_heuristic)

    result = optimizer.run_optimizer(fox=None, daikin=None)
    assert result["ok"] is False
    assert heuristic_calls == []


def test_lp_infeasible_does_not_upload_to_fox(monkeypatch: pytest.MonkeyPatch) -> None:
    """LP infeasible must NOT trigger any Fox V3 upload — the previous
    schedule on the inverter is daily-cyclic and remains in effect.
    """
    now = datetime(2026, 4, 22, 18, 0, tzinfo=UTC)
    monkeypatch.setattr(optimizer, "_now_utc", lambda: now)
    _seed_realistic_day(datetime(2026, 4, 22, 0, 0, tzinfo=UTC))

    monkeypatch.setattr("src.scheduler.lp_optimizer.solve_lp", _stub_infeasible_solve)

    from src.scheduler import lp_dispatch

    upload_calls: list[Any] = []

    def _capture_upload(fox: Any, groups: Any) -> bool:
        upload_calls.append(groups)
        return False

    monkeypatch.setattr(lp_dispatch, "upload_fox_if_operational", _capture_upload)
    monkeypatch.setattr(optimizer, "upload_fox_if_operational", _capture_upload, raising=False)

    fox = type("FakeFox", (), {"api_key": "x"})()
    result = optimizer.run_optimizer(fox=fox, daikin=None)
    assert result["ok"] is False
    assert upload_calls == [], f"unexpected Fox upload on LP infeasible: {upload_calls}"


def test_lp_infeasible_writes_audit_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """LP infeasible must write an ``optimizer_log`` row so the gap is
    visible in briefs / dashboards.
    """
    now = datetime(2026, 4, 22, 18, 0, tzinfo=UTC)
    monkeypatch.setattr(optimizer, "_now_utc", lambda: now)
    _seed_realistic_day(datetime(2026, 4, 22, 0, 0, tzinfo=UTC))

    monkeypatch.setattr("src.scheduler.lp_optimizer.solve_lp", _stub_infeasible_solve)

    captured: list[dict[str, Any]] = []
    real_log = db.log_optimizer_run

    def _capture_log(row: dict[str, Any]) -> int:
        captured.append(row)
        return real_log(row)

    monkeypatch.setattr(db, "log_optimizer_run", _capture_log)

    result = optimizer.run_optimizer(fox=None, daikin=None)
    assert result["ok"] is False
    assert captured, "optimizer_log row was not written during LP infeasible"
    row = captured[-1]
    assert row["fox_schedule_uploaded"] is False
    assert row["daikin_actions_count"] == 0
    assert "Infeasible" in (row["strategy_summary"] or "")
    assert "held previous schedule" in (row["strategy_summary"] or "")


# ---------------------------------------------------------------------------
# Infeasible-snapshot persistence — the inputs row must land in
# lp_inputs_snapshot with lp_status='Infeasible' so the constraint set can be
# replayed offline. Background: a residual class of above-reserve Infeasibles
# survives PR #339 (most likely appliance+PV-down-revision interaction; see
# 2026-05-19 prod incident). Without a snapshot we can only see "Infeasible"
# in optimizer_log; with it we can reload solve_lp inputs and reproduce.
# ---------------------------------------------------------------------------


def _stub_infeasible_solve_realistic(
    slot_starts_utc: list[datetime],
    price_pence: list[float],
    *_args: Any,
    **_kwargs: Any,
) -> LpPlan:
    """Mirror real solve_lp's shape on Infeasible: slot_starts_utc + price_pence
    + temp_outdoor_c populated, per-slot decision lists empty."""
    plan = LpPlan(
        ok=False,
        status="Infeasible",
        objective_pence=0.0,
        peak_threshold_pence=18.0,
        cheap_threshold_pence=8.0,
    )
    plan.slot_starts_utc = list(slot_starts_utc)
    plan.price_pence = list(price_pence)
    plan.temp_outdoor_c = [12.0] * len(slot_starts_utc)
    return plan


def test_lp_infeasible_persists_inputs_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Infeasible branch must persist an lp_inputs_snapshot row so the
    audit/replay path can reload the exact inputs that broke. The row carries
    ``lp_status='Infeasible'`` to distinguish from successful solves."""
    now = datetime(2026, 4, 22, 18, 0, tzinfo=UTC)
    monkeypatch.setattr(optimizer, "_now_utc", lambda: now)
    _seed_realistic_day(datetime(2026, 4, 22, 0, 0, tzinfo=UTC))

    monkeypatch.setattr(
        "src.scheduler.lp_optimizer.solve_lp", _stub_infeasible_solve_realistic,
    )

    result = optimizer.run_optimizer(fox=None, daikin=None)
    assert result["ok"] is False, result

    # The optimizer_log row was written first; its id is the run_id we'd join on.
    import sqlite3 as _sql
    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT id, strategy_summary FROM optimizer_log ORDER BY id DESC LIMIT 1"
        ).fetchone()
        assert row is not None, "optimizer_log was not written"
        run_id = int(row["id"])
        assert "Infeasible" in (row["strategy_summary"] or "")

        snap = conn.execute(
            "SELECT * FROM lp_inputs_snapshot WHERE run_id = ?", (run_id,),
        ).fetchone()
        assert snap is not None, (
            f"lp_inputs_snapshot row missing for infeasible run_id={run_id} — "
            f"replay capability is broken"
        )
        assert dict(snap)["lp_status"] == "Infeasible", (
            f"lp_status not tagged: {dict(snap)['lp_status']!r}"
        )
        # Replay-critical inputs must be present.
        assert snap["soc_initial_kwh"] is not None
        assert snap["tank_initial_c"] is not None
        assert snap["base_load_json"], "base_load_json empty — replay impossible"
        assert snap["config_snapshot_json"], "config_snapshot_json empty"
        # And the per-slot solution rows must be EMPTY — there is no decision
        # vector to write when the LP didn't solve. Writing NULL-filled rows
        # would pollute the History view (which assumes lp_solution_snapshot
        # rows describe a real solution).
        n_solution = conn.execute(
            "SELECT COUNT(*) FROM lp_solution_snapshot WHERE run_id = ?", (run_id,),
        ).fetchone()[0]
        assert n_solution == 0, (
            f"lp_solution_snapshot wrote {n_solution} rows for an Infeasible "
            f"solve — expected 0 (no decision vector exists)"
        )
    finally:
        conn.close()
