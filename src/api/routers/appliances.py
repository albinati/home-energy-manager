"""Smart appliance scheduling REST endpoints (Phase 1: SmartThings washer).

Loopback-only — same as every other ``/api/v1/...`` route, no auth middleware.

OAuth bootstrap is **not** done via REST (writing tokens via web endpoint
would defeat the user-consent property of the auth code flow). Use the
one-shot enrollment container instead:

    docker compose -f deploy/compose.smartthings-auth.yaml run --rm smartthings-auth

The endpoints here surface read-only status and let the operator revoke
local OAuth state from the API if needed.
"""
from __future__ import annotations

import logging
import secrets
import sqlite3
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ... import db
from ...config import config
from ...scheduler import appliance_dispatch
from ...smartthings import auth as st_auth
from ...smartthings import service as st_service
from ...smartthings.client import SmartThingsError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["appliances"])


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class RegisterApplianceRequest(BaseModel):
    vendor_device_id: str = Field(..., description="SmartThings deviceId UUID")
    name: str = Field(..., min_length=1, max_length=120)
    device_type: str = Field("washer", description="'washer' | 'dryer' | 'dishwasher'")
    default_duration_minutes: int = Field(120, ge=30, le=480)
    deadline_local_time: str = Field("07:00", description="HH:MM in BULLETPROOF_TIMEZONE")
    typical_kw: float = Field(0.5, gt=0, le=15.0)


class UpdateApplianceRequest(BaseModel):
    name: str | None = None
    device_type: str | None = None
    default_duration_minutes: int | None = Field(None, ge=30, le=480)
    deadline_local_time: str | None = None
    typical_kw: float | None = Field(None, gt=0, le=15.0)
    enabled: bool | None = None


# SetCredentialsRequest removed — OAuth tokens are written by the one-shot
# enrollment container (deploy/compose.smartthings-auth.yaml). The /credentials
# POST endpoint is gone for the same reason: the auth code flow needs a real
# user-agent (browser) to present consent, not a JSON POST.


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _public_appliance(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "vendor": row["vendor"],
        "vendor_device_id": row["vendor_device_id"],
        "name": row["name"],
        "device_type": row["device_type"],
        "default_duration_minutes": row["default_duration_minutes"],
        "deadline_local_time": row["deadline_local_time"],
        "typical_kw": row["typical_kw"],
        "enabled": bool(row["enabled"]),
        "created_at": row["created_at"],
    }


def _public_job(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "appliance_id": row["appliance_id"],
        "status": row["status"],
        "armed_at_utc": row["armed_at_utc"],
        "deadline_utc": row["deadline_utc"],
        "duration_minutes": row["duration_minutes"],
        "planned_start_utc": row["planned_start_utc"],
        "planned_end_utc": row["planned_end_utc"],
        "avg_price_pence": row["avg_price_pence"],
        "actual_start_utc": row["actual_start_utc"],
        "error_msg": row["error_msg"],
        "last_replan_at_utc": row["last_replan_at_utc"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


# ---------------------------------------------------------------------------
# /api/v1/appliances — appliance CRUD + discovery
# ---------------------------------------------------------------------------

@router.get("/appliances")
async def list_appliances() -> dict[str, Any]:
    rows = db.list_appliances()
    return {
        "appliances": [_public_appliance(r) for r in rows],
        "count": len(rows),
    }


@router.post("/appliances/discover")
async def discover_appliances() -> dict[str, Any]:
    """Call SmartThings ``GET /devices`` and return what's visible to the PAT.

    The user picks one and POSTs to ``/api/v1/appliances`` to register it.
    """
    try:
        client = st_service.get_client()
    except SmartThingsError as e:
        raise HTTPException(503, f"SmartThings not configured: {e}") from None
    try:
        devices = client.list_devices()
    except SmartThingsError as e:
        raise HTTPException(502, f"SmartThings error: {e}") from None
    return {"devices": devices, "count": len(devices)}


@router.post("/appliances", status_code=201)
async def register_appliance(req: RegisterApplianceRequest) -> dict[str, Any]:
    try:
        appliance_id = db.add_appliance(
            vendor="smartthings",
            vendor_device_id=req.vendor_device_id,
            name=req.name,
            device_type=req.device_type,
            default_duration_minutes=req.default_duration_minutes,
            deadline_local_time=req.deadline_local_time,
            typical_kw=req.typical_kw,
            enabled=True,
        )
    except sqlite3.IntegrityError:
        raise HTTPException(
            409,
            f"appliance vendor_device_id={req.vendor_device_id!r} already registered",
        ) from None
    row = db.get_appliance(appliance_id)
    if row is None:
        raise HTTPException(500, "appliance row missing after insert")
    return _public_appliance(row)


@router.patch("/appliances/{appliance_id}")
async def update_appliance(
    appliance_id: int,
    req: UpdateApplianceRequest,
) -> dict[str, Any]:
    if db.get_appliance(appliance_id) is None:
        raise HTTPException(404, f"appliance {appliance_id} not found")
    fields = {k: v for k, v in req.model_dump(exclude_none=True).items()}
    if fields:
        db.update_appliance(appliance_id, **fields)
    row = db.get_appliance(appliance_id)
    if row is None:
        raise HTTPException(404, f"appliance {appliance_id} not found")
    return _public_appliance(row)


@router.delete("/appliances/{appliance_id}", status_code=204)
async def delete_appliance(appliance_id: int) -> None:
    if not db.delete_appliance(appliance_id):
        raise HTTPException(404, f"appliance {appliance_id} not found")
    # Also drop any pending APScheduler cron for this appliance.
    appliance_dispatch._remove_cron(appliance_id)


# ---------------------------------------------------------------------------
# /api/v1/appliances/jobs — armed sessions
# ---------------------------------------------------------------------------

@router.get("/appliances/jobs")
async def list_jobs(
    status: str | None = None,
    from_utc: str | None = None,
    to_utc: str | None = None,
    appliance_id: int | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    rows = db.get_appliance_jobs(
        status=status,
        from_utc=from_utc,
        to_utc=to_utc,
        appliance_id=appliance_id,
        limit=max(1, min(500, int(limit))),
    )
    return {
        "jobs": [_public_job(r) for r in rows],
        "count": len(rows),
    }


@router.post("/appliances/jobs/{job_id}/cancel")
async def cancel_job(job_id: int) -> dict[str, Any]:
    job = db.get_appliance_job(job_id)
    if job is None:
        raise HTTPException(404, f"job {job_id} not found")
    if job["status"] != "scheduled":
        raise HTTPException(
            409, f"job {job_id} is in status {job['status']}; only 'scheduled' can be cancelled"
        )
    db.update_appliance_job(
        job_id, status="cancelled", error_msg="cancelled_via_api"
    )
    appliance_dispatch._remove_cron(int(job["appliance_id"]))
    row = db.get_appliance_job(job_id)
    return _public_job(row) if row else {"id": job_id, "status": "cancelled"}


# ---------------------------------------------------------------------------
# /api/v1/integrations/smartthings — credentials + status
# ---------------------------------------------------------------------------

@router.get("/integrations/smartthings/oauth/start")
async def smartthings_oauth_start() -> dict[str, Any]:
    """Return the Samsung consent URL the operator opens in a browser.

    NOTE: this endpoint does NOT actually start the callback server — that
    runs inside the one-shot ``smartthings-auth`` container. Use this when
    you want to visit the URL manually after starting the container, or to
    inspect what scopes will be requested. Generates a fresh ``state`` token
    each call (no persistence — the container's own server validates it).
    """
    if not config.SMARTTHINGS_CLIENT_ID:
        raise HTTPException(412, "SMARTTHINGS_CLIENT_ID not configured")
    state = secrets.token_urlsafe(16)
    return {
        "ok": True,
        "authorize_url": st_auth.authorize_url(state),
        "state": state,
        "scopes": config.SMARTTHINGS_OAUTH_SCOPES,
        "redirect_uri": config.SMARTTHINGS_REDIRECT_URI,
        "_note": (
            "Run `docker compose -f deploy/compose.smartthings-auth.yaml run --rm "
            "smartthings-auth` to start the local callback server, then open "
            "this authorize_url in a browser through an SSH port-forward to :8080."
        ),
    }


@router.delete("/integrations/smartthings/credentials", status_code=204)
async def delete_smartthings_credentials() -> None:
    """Revoke local OAuth state by deleting the token file.

    Does NOT revoke the SmartThings-side authorization — to do that, the user
    must visit Samsung's "Connected Services" UI in the SmartThings app.
    """
    st_service.delete_tokens()


@router.get("/integrations/smartthings/status")
async def smartthings_status() -> dict[str, Any]:
    """Health check for the SmartThings integration. Never returns secrets."""
    present = st_service.tokens_present()
    out: dict[str, Any] = {
        "tokens_present": present,
        "client_id_configured": bool(config.SMARTTHINGS_CLIENT_ID),
        "api_base": config.SMARTTHINGS_API_BASE,
        "dispatch_enabled": bool(config.APPLIANCE_DISPATCH_ENABLED),
        "read_only": bool(config.OPENCLAW_READ_ONLY),
    }
    if not present:
        out["reachable"] = None
        out["device_count"] = None
        return out
    try:
        client = st_service.get_client()
        devices = client.list_devices()
        out["reachable"] = True
        out["device_count"] = len(devices)
    except SmartThingsError as e:
        out["reachable"] = False
        out["error_code"] = e.code
        out["error_http_status"] = e.http_status
    return out
