"""Auto-tune change ledger API (#876 story 1).

Reads are viewer-safe; every POST is admin through ``ApiV1RoleAuth`` (any
non-safe method). The kill switch ``AUTO_TUNE_ENABLED`` only gates automatic
proposals: rollback / freeze stay available to the owner whatever it says.
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException

router = APIRouter(tags=["auto_tune"])


def _require_no_simulation_gate() -> None:
    """Story 1 does not implement the simulate->confirm flow for ledger writes:
    when ``REQUIRE_SIMULATION_ID`` is on they are refused (409) instead of silently
    bypassing the gate that ``PUT /settings/{key}`` honours."""
    from ... import runtime_settings as rts
    if str(rts.get_setting("REQUIRE_SIMULATION_ID") or "false").strip().lower() == "true":
        raise HTTPException(status_code=409, detail={
            "reason": "simulation_required",
            "message": "REQUIRE_SIMULATION_ID is on; ledger writes are not simulatable yet. "
                       "Use PUT /api/v1/settings/{key} with X-Simulation-Id."})


@router.get("/api/v1/tuning/ledger")
async def get_ledger(limit: int = 20, key: str | None = None) -> dict[str, Any]:
    from ... import auto_tune
    limit = max(1, min(int(limit), 200))
    return await asyncio.to_thread(auto_tune.ledger_summary, limit, key)


@router.post("/api/v1/tuning/ledger/propose")
async def post_propose(body: dict) -> dict[str, Any]:
    """Run ``propose_change`` now (testing / owner-driven). ``source`` is one of the
    ledger sources, or ``manual_test`` (recorded as ``tuning_review`` with a note).
    A refusal is a 409 carrying the stable ``reason``."""
    from ... import auto_tune
    _require_no_simulation_gate()
    if "key" not in body or "value" not in body:
        raise HTTPException(status_code=400, detail="body needs 'key' and 'value'")
    source = str(body.get("source") or "manual_test")
    note = body.get("note")
    if source == "manual_test":
        source, note = "tuning_review", (note or "manual_test")
    res = await asyncio.to_thread(auto_tune.propose_change, str(body["key"]), body["value"],
                                  source, note=note)
    if not res.get("applied"):
        raise HTTPException(status_code=422 if res.get("reason") == "invalid" else 409, detail=res)
    return res


@router.post("/api/v1/tuning/ledger/{change_id}/rollback")
async def post_rollback(change_id: int, body: dict | None = None) -> dict[str, Any]:
    from ... import auto_tune
    _require_no_simulation_gate()
    reason = str((body or {}).get("reason") or "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="body needs a non-empty 'reason'")
    res = await asyncio.to_thread(auto_tune.rollback, change_id, reason=reason, actor="api")
    if not res.get("rolled_back"):
        code = 404 if res.get("reason") == "not_found" else 409
        raise HTTPException(status_code=code, detail=res)
    return res


@router.post("/api/v1/tuning/ledger/{change_id}/freeze")
async def post_freeze(change_id: int, body: dict) -> dict[str, Any]:
    from ... import auto_tune
    try:
        days = int(body["days"])
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="body needs integer 'days'") from exc
    if not 0 <= days <= 365:
        raise HTTPException(status_code=400, detail="days must be 0..365")
    res = await asyncio.to_thread(auto_tune.freeze, change_id, days=days)
    if not res.get("frozen"):
        raise HTTPException(status_code=404, detail=res)
    return res
