"""Dispatch-decision and plan-timeline endpoints.

Three read-only endpoints that surface what the LP solver decided, what made
it onto Fox V3, and a plan-vs-live diff. Each is callable from OpenClaw via
the matching MCP tool (see :mod:`src.mcp_server`).

* ``GET /api/v1/optimization/decisions/{run_id}`` — per-slot dispatch
  decisions for one LP run, including the three scenario export values.
* ``GET /api/v1/scheduler/timeline`` — the active plan partitioned into
  executed / ongoing / planned slots, with realised values where available.
* ``GET /api/v1/foxess/schedule_diff`` — live Fox V3 scheduler state vs. the
  last recorded upload (drift detector).
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException

from ... import db
from ...config import config
from ...foxess.client import FoxESSClient

logger = logging.getLogger(__name__)

router = APIRouter(tags=["dispatch"])


def _resolve_run_id(run_id_param: str | int) -> int:
    """Accept ``"latest"`` or an integer; raise 404 if no run exists."""
    if isinstance(run_id_param, str) and run_id_param.strip().lower() == "latest":
        rid = db.find_latest_optimizer_run_id()
        if rid is None:
            raise HTTPException(404, "No optimizer runs on file")
        return rid
    try:
        return int(run_id_param)
    except (TypeError, ValueError):
        raise HTTPException(400, f"Invalid run_id: {run_id_param!r}")


@router.get("/api/v1/optimization/decisions/{run_id}")
async def get_dispatch_decisions(run_id: str) -> dict[str, Any]:
    """Per-slot dispatch decisions for an LP run.

    ``run_id`` accepts either an integer or the literal string ``"latest"``.

    Each decision row records the LP-emitted ``lp_kind`` (e.g. ``peak_export``),
    the ``dispatched_kind`` after the robustness filter (e.g. ``standard``
    when pessimistic disagrees), a boolean ``committed`` flag, the textual
    ``reason``, and per-scenario export kWh values for ``optimistic`` /
    ``nominal`` / ``pessimistic``.
    """
    rid = _resolve_run_id(run_id)
    rows = db.get_dispatch_decisions(rid)
    return {
        "run_id": rid,
        "decisions": rows,
        "summary": {
            "total_slots": len(rows),
            "peak_export_committed": sum(
                1 for r in rows if r["lp_kind"] == "peak_export" and r["committed"]
            ),
            "peak_export_dropped": sum(
                1 for r in rows if r["lp_kind"] == "peak_export" and not r["committed"]
            ),
            "drop_reasons": {
                reason: sum(1 for r in rows if not r["committed"] and r["reason"] == reason)
                for reason in {r["reason"] for r in rows if not r["committed"]}
            },
        },
    }


@router.get("/api/v1/scheduler/timeline")
async def get_scheduler_timeline() -> dict[str, Any]:
    """Active plan split into executed / ongoing / planned slots.

    Top-level fields:

    * ``run_id`` / ``run_at`` / ``plan_date`` — provenance of the active plan.
    * ``tariff_code`` — the import tariff used in the LP objective.
    * ``peak_threshold_pence`` / ``cheap_threshold_pence`` — daily price quartiles
      from the LP solution.
    * ``executed[]`` — slots already past, joined with ``execution_log`` so
      callers can compare planned vs. realised values.
    * ``ongoing`` — single slot containing now (or null if outside horizon).
    * ``planned[]`` — slots in the future, with the dispatch decision attached
      so OpenClaw can answer "is X scheduled to discharge?" without separate
      calls to ``/optimization/decisions``.
    """
    rid = db.find_latest_optimizer_run_id()
    if rid is None:
        return {
            "ok": False,
            "error": "No optimizer runs on file",
            "executed": [], "ongoing": None, "planned": [],
        }
    inputs = db.get_lp_inputs(rid) or {}
    slots = db.get_lp_solution_slots(rid)
    decisions = {d["slot_time_utc"]: d for d in db.get_dispatch_decisions(rid)}
    now = datetime.now(UTC)

    executed: list[dict[str, Any]] = []
    planned: list[dict[str, Any]] = []
    ongoing: dict[str, Any] | None = None

    for s in slots:
        try:
            st = datetime.fromisoformat(s["slot_time_utc"].replace("Z", "+00:00"))
        except (ValueError, KeyError):
            continue
        end = st.replace(microsecond=0)
        # Decision row (if any) — gives committed/dropped flag + reason.
        d = decisions.get(s["slot_time_utc"])
        slot_view = {
            "slot_time_utc": s["slot_time_utc"],
            "price_p": s.get("price_p"),
            "import_kwh": s.get("import_kwh"),
            "export_kwh": s.get("export_kwh"),
            "charge_kwh": s.get("charge_kwh"),
            "discharge_kwh": s.get("discharge_kwh"),
            "soc_kwh": s.get("soc_kwh"),
            "tank_temp_c": s.get("tank_temp_c"),
            "indoor_temp_c": s.get("indoor_temp_c"),
            "outdoor_temp_c": s.get("outdoor_temp_c"),
            "lp_kind": d["lp_kind"] if d else None,
            "dispatched_kind": d["dispatched_kind"] if d else None,
            "committed": bool(d["committed"]) if d else None,
            "decision_reason": d["reason"] if d else None,
        }

        # Position relative to now: slot is 30 min long.
        slot_end = st.replace(minute=st.minute, second=0, microsecond=0)
        # We only have start; assume 30 min duration matches LP slot grid.
        from datetime import timedelta as _td
        slot_end = st + _td(minutes=30)

        if slot_end <= now:
            executed.append(slot_view)
        elif st <= now < slot_end:
            ongoing = slot_view
        else:
            planned.append(slot_view)

    return {
        "ok": True,
        "run_id": rid,
        "run_at": inputs.get("run_at_utc"),
        "plan_date": inputs.get("plan_date"),
        "tariff_code": (config.OCTOPUS_TARIFF_CODE or "").strip() or None,
        "peak_threshold_pence": inputs.get("peak_threshold_p"),
        "cheap_threshold_pence": (
            inputs.get("cheap_threshold_p") if "cheap_threshold_p" in inputs else None
        ),
        "now_utc": now.isoformat(),
        "executed": executed,
        "ongoing": ongoing,
        "planned": planned,
        "counts": {
            "executed": len(executed),
            "ongoing": 1 if ongoing else 0,
            "planned": len(planned),
        },
    }


def _normalise_group(g: dict[str, Any]) -> dict[str, Any]:
    """Reduce a Fox group (live or recorded) to a comparable shape.

    Live groups arrive from the FoxESS API as :class:`SchedulerGroup` instances.
    Recorded groups arrive from ``fox_schedule_state.groups_json`` as the
    ``to_api_dict`` shape (``startHour`` etc. with nested ``extraParam``).
    """
    if isinstance(g, dict):
        # Recorded shape from to_api_dict()
        ep = g.get("extraParam") or {}
        return {
            "start": f"{int(g.get('startHour', 0)):02d}:{int(g.get('startMinute', 0)):02d}",
            "end": f"{int(g.get('endHour', 0)):02d}:{int(g.get('endMinute', 0)):02d}",
            "work_mode": g.get("workMode"),
            "min_soc_on_grid": ep.get("minSocOnGrid"),
            "fd_soc": ep.get("fdSoc"),
            "fd_pwr": ep.get("fdPwr"),
            "max_soc": ep.get("maxSoc"),
        }
    # SchedulerGroup dataclass instance
    return {
        "start": f"{g.start_hour:02d}:{g.start_minute:02d}",
        "end": f"{g.end_hour:02d}:{g.end_minute:02d}",
        "work_mode": g.work_mode,
        "min_soc_on_grid": g.min_soc_on_grid,
        "fd_soc": g.fd_soc,
        "fd_pwr": g.fd_pwr,
        "max_soc": g.max_soc,
    }


@router.get("/api/v1/optimization/scenarios/{batch_id}")
async def get_scenario_batch(batch_id: str) -> dict[str, Any]:
    """Per-scenario solve summary for one batch.

    ``batch_id`` accepts an integer or the literal ``"latest"``. The batch
    id equals the canonical (nominal) run's ``optimizer_log.id``. Each row
    records the LP status, objective pence, perturbation deltas applied,
    peak-export slot count, and wall-clock duration — useful for spotting
    "scenario X took 4× as long as the others" or "pessimistic objective is
    £1 worse than nominal so robustness is biting".
    """
    rid = _resolve_run_id(batch_id)
    rows = db.get_scenario_solve_batch(rid)
    if not rows:
        # Run exists but no scenarios were logged for it (trigger reason
        # not in allow-list, or plan had no peak_export slots).
        return {
            "ok": True,
            "batch_id": rid,
            "scenarios": [],
            "note": "No scenario solves logged for this run (LP_SCENARIOS_ON_TRIGGER_REASONS or empty peak_export plan).",
        }
    by_kind = {r["scenario_kind"]: r for r in rows}
    return {
        "ok": True,
        "batch_id": rid,
        "nominal_run_id": rows[0]["nominal_run_id"],
        "scenarios": rows,
        "summary": {
            "objectives_pence": {
                k: by_kind[k]["objective_pence"]
                for k in ("optimistic", "nominal", "pessimistic")
                if k in by_kind
            },
            "peak_export_slot_counts": {
                k: by_kind[k]["peak_export_slot_count"]
                for k in ("optimistic", "nominal", "pessimistic")
                if k in by_kind
            },
            "max_duration_ms": max((r["duration_ms"] or 0) for r in rows),
            "any_failure": any(r.get("error") or r.get("lp_status", "").startswith("error") for r in rows),
        },
    }


@router.get("/api/v1/foxess/schedule_diff")
async def get_foxess_schedule_diff() -> dict[str, Any]:
    """Compare live Fox V3 scheduler state against the last recorded upload.

    Returns ``any_drift=True`` when the live state differs from what the LP
    last asked for — symptoms could be: a manual edit via the Fox app, a
    failed previous upload, or a Fox firmware quirk. The diff is structural
    (per-group fingerprint comparison), not just count-based.
    """
    state = db.get_latest_fox_schedule_state()
    recorded_groups = (state or {}).get("groups", []) or []

    live_groups: list[dict[str, Any]] = []
    live_error: str | None = None
    try:
        fox = FoxESSClient(**config.foxess_client_kwargs())
        live_state = fox.get_scheduler_v3()
        live_groups = [_normalise_group(g) for g in live_state.groups]
    except Exception as e:
        live_error = str(e)
        logger.warning("schedule_diff: live Fox read failed: %s", e)

    rec_norm = [_normalise_group(g) for g in recorded_groups]

    # Compare on a fingerprint of (start, end, work_mode, min_soc_on_grid, fd_soc, fd_pwr, max_soc).
    def _fp(g: dict[str, Any]) -> tuple:
        return (
            g.get("start"), g.get("end"), g.get("work_mode"),
            g.get("min_soc_on_grid"),
            None if g.get("fd_soc") is None else float(g["fd_soc"]),
            None if g.get("fd_pwr") is None else float(g["fd_pwr"]),
            None if g.get("max_soc") is None else float(g["max_soc"]),
        )

    live_fps = {_fp(g) for g in live_groups}
    rec_fps = {_fp(g) for g in rec_norm}
    only_live = [g for g in live_groups if _fp(g) not in rec_fps]
    only_recorded = [g for g in rec_norm if _fp(g) not in live_fps]

    any_drift = bool(only_live or only_recorded)

    return {
        "ok": live_error is None,
        "any_drift": any_drift,
        "live_groups": live_groups,
        "recorded_groups": rec_norm,
        "diffs": {
            "only_live": only_live,
            "only_recorded": only_recorded,
        },
        "recorded_uploaded_at": (state or {}).get("uploaded_at"),
        "live_error": live_error,
    }
