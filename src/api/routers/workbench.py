"""v10.2 E3 — Workbench: tune LP knobs, simulate, promote.

All endpoints live under ``/api/v1/workbench``. The router is mounted from
``api/main.py``. Three flows:

1. **Simulate** — ``POST /simulate`` with ``{overrides: {KEY: value, ...}}``
   patches the config singleton, runs ``run_lp_simulation`` (no cloud calls),
   restores config, and returns the LP result alongside the override audit.

2. **Promote** — ``POST /promote/simulate`` returns an :class:`ActionDiff` for
   the subset of overrides that are promotable (have a matching
   ``runtime_settings.SCHEMA`` entry); ``POST /promote`` consumes the
   ``X-Simulation-Id`` and applies the promotable subset via the existing
   batch-settings rollback path.

3. **Profiles** — named override snapshots. List / save / load / delete.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header, HTTPException

from ...config import config
from ...scheduler import lp_overrides
from ...scheduler.lp_simulation import run_lp_simulation
from ..simulate_diffs import diff_settings_batch
from ..simulation import ActionDiff, get_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/workbench", tags=["workbench"])


def _profiles_dir() -> Path:
    """Profile snapshot dir lives next to the config snapshots."""
    base = Path(getattr(config, "CONFIG_SNAPSHOT_DIR", "data/snapshots"))
    d = base / "workbench_profiles"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _profile_path(name: str) -> Path:
    safe = "".join(c for c in name if c.isalnum() or c in ("-", "_")).strip()
    if not safe:
        raise HTTPException(status_code=400, detail="profile name must contain only [A-Za-z0-9_-]")
    return _profiles_dir() / f"{safe}.json"


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

@router.get("/schema")
async def workbench_schema():
    """Return the override whitelist with metadata for the editor."""
    return {
        "groups": ["comfort", "battery", "hardware", "penalty", "solver", "schedule", "mode"],
        "fields": lp_overrides.schema_for_response(),
    }


# ---------------------------------------------------------------------------
# Simulate
# ---------------------------------------------------------------------------

@router.post("/simulate")
async def workbench_simulate(body: dict | None = None):
    """Run the LP with ``overrides`` patched onto config (no DB/cloud writes).

    Body: ``{overrides: {KEY: value, ...}}``. Returns the simulation result
    plus an audit of what was applied vs ignored.
    """
    body = body or {}
    overrides_raw = body.get("overrides") or {}
    try:
        overrides = lp_overrides.validate_overrides(overrides_raw)
    except lp_overrides.OverrideValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    with lp_overrides.patched_config(overrides) as prior:
        result = run_lp_simulation(allow_daikin_refresh=False)
        applied = {k: overrides[k] for k in overrides if lp_overrides.WHITELIST[k].config_attr in prior}

    payload: dict[str, Any] = {
        "ok": result.ok,
        "error": result.error,
        "plan_date": result.plan_date,
        "plan_window": result.plan_window,
        "slot_count": result.slot_count,
        "objective_pence": result.objective_pence,
        "status": result.status,
        "actual_mean_agile_pence": result.actual_mean_agile_pence,
        "forecast_solar_kwh_horizon": result.forecast_solar_kwh_horizon,
        "mu_load_kwh_per_slot": result.mu_load_kwh,
        "applied_overrides": applied,
        "ignored_overrides": {k: v for k, v in overrides.items() if k not in applied},
    }
    if result.plan and result.ok:
        # Per-slot summary mirrors the shape /optimization/plan returns
        payload["slots"] = [
            {
                "t": (result.slot_starts_utc[i].isoformat().replace("+00:00", "Z")) if result.slot_starts_utc else None,
                "price_p": result.plan.price_pence[i] if i < len(result.plan.price_pence) else None,
                "import_kwh": result.plan.import_kwh[i] if i < len(result.plan.import_kwh) else None,
                "export_kwh": result.plan.export_kwh[i] if i < len(result.plan.export_kwh) else None,
                "battery_charge_kwh": result.plan.battery_charge_kwh[i] if i < len(result.plan.battery_charge_kwh) else None,
                "battery_discharge_kwh": result.plan.battery_discharge_kwh[i] if i < len(result.plan.battery_discharge_kwh) else None,
                "soc_kwh": result.plan.soc_kwh[i] if i < len(result.plan.soc_kwh) else None,
            }
            for i in range(result.slot_count)
        ]
    return payload


# ---------------------------------------------------------------------------
# Promote (uses E5 batch endpoint under the hood)
# ---------------------------------------------------------------------------

def _filter_promotable(overrides: dict[str, Any]) -> dict[str, Any]:
    promotable = lp_overrides.promotable_keys()
    return {k: v for k, v in overrides.items() if k in promotable}


@router.post("/promote/simulate")
async def workbench_promote_simulate(body: dict | None = None):
    """Return an ActionDiff for the promotable subset of ``overrides``.

    Non-promotable overrides are still surfaced (under
    ``non_promotable_overrides``) so the operator can see what won't persist.
    """
    body = body or {}
    overrides_raw = body.get("overrides") or {}
    try:
        overrides = lp_overrides.validate_overrides(overrides_raw)
    except lp_overrides.OverrideValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    promotable = _filter_promotable(overrides)
    if not promotable:
        raise HTTPException(
            status_code=400,
            detail="no promotable keys in overrides — nothing to write to prod",
        )

    diff = diff_settings_batch(promotable)
    diff.action = "workbench.promote"
    diff.human_summary = (
        f"Promote {len(promotable)} workbench tweak(s) to prod settings. "
        f"({len(overrides) - len(promotable)} non-promotable overrides ignored.)"
    )
    sid = get_store().register(diff)
    payload = diff.to_response_dict()
    payload["non_promotable_overrides"] = {
        k: v for k, v in overrides.items() if k not in promotable
    }
    return payload


@router.post("/promote")
async def workbench_promote(
    body: dict | None = None,
    x_simulation_id: str | None = Header(None, alias="X-Simulation-Id"),
):
    """Apply promotable overrides via the runtime_settings layer.

    Pair with ``/promote/simulate``; same X-Simulation-Id flow as
    ``/api/v1/settings/batch``. Optional ``profile_name`` snapshot saved.
    """
    # Lazy-import to avoid circular: workbench → main → workbench
    from ..main import _enforce_simulation_id
    _enforce_simulation_id("workbench.promote", x_simulation_id)

    body = body or {}
    overrides_raw = body.get("overrides") or {}
    try:
        overrides = lp_overrides.validate_overrides(overrides_raw)
    except lp_overrides.OverrideValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    promotable = _filter_promotable(overrides)
    if not promotable:
        raise HTTPException(status_code=400, detail="no promotable keys to write")

    from ... import runtime_settings as rts
    applied: list[tuple[str, Any]] = []
    results: list[dict] = []
    for key, value in promotable.items():
        try:
            prior = rts.get_setting(key)
        except Exception:
            prior = None
        try:
            canonical = rts.set_setting(key, value, actor="workbench_promote")
            applied.append((key, prior))
            results.append({"key": key, "ok": True, "value": canonical})
        except Exception as exc:
            rollback_errors: list[dict] = []
            for ok_key, ok_prior in applied:
                try:
                    if ok_prior is not None:
                        rts.set_setting(ok_key, ok_prior, actor="workbench_promote_rollback")
                    else:
                        rts.delete_setting(ok_key, actor="workbench_promote_rollback")
                except Exception as rb_exc:
                    rollback_errors.append({"key": ok_key, "error": str(rb_exc)})
            results.append({"key": key, "ok": False, "error": str(exc)})
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "WorkbenchPromotePartialFailure",
                    "failed_at_key": key,
                    "results": results,
                    "rollback_errors": rollback_errors,
                },
            )

    profile_name = (body.get("profile_name") or "").strip() or None
    if profile_name:
        try:
            _save_profile(profile_name, overrides)
        except HTTPException:
            raise
        except Exception as exc:
            logger.warning("workbench_promote: profile save failed: %s", exc)

    return {
        "ok": True,
        "promoted": results,
        "non_promotable_overrides": {k: v for k, v in overrides.items() if k not in promotable},
        "profile_name": profile_name,
    }


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------

def _save_profile(name: str, overrides: dict[str, Any]) -> Path:
    path = _profile_path(name)
    path.write_text(json.dumps({
        "profile_name": name,
        "saved_at": datetime.now(UTC).isoformat(),
        "overrides": overrides,
    }, indent=2))
    return path


@router.get("/profiles")
async def list_profiles():
    out = []
    for p in sorted(_profiles_dir().glob("*.json")):
        try:
            d = json.loads(p.read_text())
            out.append({
                "name": d.get("profile_name") or p.stem,
                "saved_at": d.get("saved_at"),
                "key_count": len(d.get("overrides") or {}),
            })
        except Exception as exc:
            out.append({"name": p.stem, "error": str(exc)})
    return {"profiles": out}


@router.post("/profiles/{name}")
async def save_profile(name: str, body: dict | None = None):
    """Save a named profile of overrides (does NOT apply — call /promote for that)."""
    body = body or {}
    overrides_raw = body.get("overrides") or {}
    try:
        overrides = lp_overrides.validate_overrides(overrides_raw)
    except lp_overrides.OverrideValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    path = _save_profile(name, overrides)
    return {"ok": True, "name": name, "path": str(path), "key_count": len(overrides)}


@router.get("/profiles/{name}")
async def load_profile(name: str):
    """Return a profile's overrides (does NOT apply)."""
    path = _profile_path(name)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"profile {name!r} not found")
    return json.loads(path.read_text())


@router.delete("/profiles/{name}")
async def delete_profile(name: str):
    path = _profile_path(name)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"profile {name!r} not found")
    path.unlink()
    return {"ok": True, "name": name}
