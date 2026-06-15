"""Indoor temperature ingestion (#540 W1).

The Altherma has no room stat, so the house's indoor temperature has never been
measured. This is the pipe the user's room sensors push into — a generic batch
POST (admin-gated by the role middleware) plus a viewer-readable recent feed.
Downstream: the LP initial state + the dispatch comfort guard read the freshest
fresh reading; the W2 thermal learner reads the history.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from ... import db
from ...config import config

logger = logging.getLogger(__name__)

router = APIRouter(tags=["sensors"])


class IndoorReading(BaseModel):
    captured_at: str = Field(..., description="ISO-8601 UTC timestamp")
    temp_c: float = Field(..., ge=-20, le=45)  # indoor room; tight ceiling catches F/C slips
    room: str = "home"
    source: str | None = None
    quality: str | None = None


class IndoorReadingsBody(BaseModel):
    readings: list[IndoorReading] = Field(..., min_length=1, max_length=2000)


@router.post("/api/v1/sensors/indoor")
async def post_indoor_readings(body: IndoorReadingsBody) -> dict[str, Any]:
    """Ingest one or more indoor temperature readings (admin). Idempotent on
    (captured_at, room). Returns how many new rows were written."""
    written = db.save_indoor_readings([r.model_dump() for r in body.readings])
    logger.info("indoor sensors: ingested %d/%d new readings", written, len(body.readings))
    return {"received": len(body.readings), "written": written}


@router.get("/api/v1/sensors/indoor")
async def get_indoor_readings(hours: int = 24) -> dict[str, Any]:
    """Recent indoor readings (viewer) + the freshest fresh house temperature and
    its staleness, for the cockpit chart / a staleness chip."""
    hours = max(1, min(int(hours), 168))
    now = datetime.now(UTC)
    start = (now - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    # Small future buffer so a reading that just arrived this second (end is
    # exclusive) is still included.
    end = (now + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = db.get_indoor_readings_range(start, end)
    stale_min = int(getattr(config, "INDOOR_SENSOR_STALE_MINUTES", 30))
    latest = db.get_latest_indoor_reading(max_age_minutes=stale_min)
    # Also report the single newest row regardless of staleness, so the UI can say
    # "last seen 4h ago" even when it's beyond the fresh window.
    newest = rows[-1] if rows else None
    return {
        "hours": hours,
        "n_readings": len(rows),
        "readings": rows,
        "latest_fresh": latest,             # None when nothing within the stale window
        "newest_at": newest["captured_at"] if newest else None,
        "stale_minutes": stale_min,
        "configured": bool(rows),
    }
