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
from pydantic import BaseModel, ConfigDict, Field

from ... import db
from ...config import config

logger = logging.getLogger(__name__)

router = APIRouter(tags=["sensors"])

# The LP / thermal model only trusts an indoor temperature inside this band; a
# reading outside it (F/C slip, sensor fault) is still LOGGED per-device but is
# NOT routed into room_temperature_history where the solver would read it.
_TEMP_LP_MIN_C = -20.0
_TEMP_LP_MAX_C = 45.0


class IndoorReading(BaseModel):
    # extra="allow" — any field a device sends that isn't named here (a 2nd
    # temperature, a battery level, RSSI, …) is preserved and lands in the raw
    # per-device log (#540 W1c). Nothing a sensor reports is dropped.
    model_config = ConfigDict(extra="allow")

    captured_at: str = Field(..., description="ISO-8601 UTC timestamp")
    # temp_c is optional now: a humidity/pressure-only device can still log. When
    # present AND in-band it feeds the thermal model; otherwise it's log-only.
    temp_c: float | None = None
    humidity_pct: float | None = Field(default=None, ge=0, le=100)
    pressure_hpa: float | None = Field(default=None, ge=250, le=1200)
    room: str = "home"
    source: str | None = None
    device_id: str | None = None
    mac: str | None = None
    quality: str | None = None


class IndoorReadingsBody(BaseModel):
    readings: list[IndoorReading] = Field(..., min_length=1, max_length=2000)


@router.post("/api/v1/sensors/indoor")
async def post_indoor_readings(body: IndoorReadingsBody) -> dict[str, Any]:
    """Ingest sensor readings (admin / scoped ingest token). TWO sinks:

    * ``device_reading_log`` — the FULL raw payload of every reading, per device
      (temp, humidity, pressure, MAC, device_id, and any extra field). Lossless.
    * ``room_temperature_history`` — only the in-band ``temp_c`` values, which
      the LP + thermal model read.

    Both are idempotent on their keys, so a retry doesn't double-count. Returns
    the received count plus rows written to each sink."""
    raw = [r.model_dump() for r in body.readings]
    logged = db.save_device_reading_log(raw)
    temp_rows = [
        r for r in raw
        if r.get("temp_c") is not None and _TEMP_LP_MIN_C <= r["temp_c"] <= _TEMP_LP_MAX_C
    ]
    written = db.save_indoor_readings(temp_rows)
    logger.info(
        "sensors: %d readings → %d device-log rows, %d indoor temp rows (%d out-of-band skipped)",
        len(raw), logged, written, sum(1 for r in raw if r.get("temp_c") is not None) - len(temp_rows),
    )
    return {"received": len(raw), "written": written, "logged": logged}


@router.get("/api/v1/sensors/devices")
async def get_sensor_devices() -> dict[str, Any]:
    """One row per device ever seen (viewer): identity (device_key/mac/device_id/
    room), first/last-seen, reading count, and the latest metric values."""
    devices = db.list_sensor_devices()
    return {"n_devices": len(devices), "devices": devices}


@router.get("/api/v1/sensors/device-log")
async def get_sensor_device_log(device: str | None = None, hours: int = 24) -> dict[str, Any]:
    """Recent raw per-device readings, newest first (viewer). ``device`` filters
    to one device_key; ``hours`` bounds the window (1..168). Each row carries the
    full original ``payload``."""
    hours = max(1, min(int(hours), 168))
    rows = db.get_device_reading_log(device_key=device, hours=hours)
    return {"device": device, "hours": hours, "n_rows": len(rows), "rows": rows}


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


@router.get("/api/v1/sensors/thermal-calibration")
async def get_thermal_calibration() -> dict[str, Any]:
    """W2 building thermal calibration (#540): the learned row (or null before
    the sensors have produced enough clean decay nights) plus the EFFECTIVE
    values the estimator resolves through the bounded readers — so the UI can
    always show what the system is actually using and where it came from."""
    from ...analytics import thermal_learning as tl

    row = None
    try:
        row = db.get_building_thermal_calibration()
    except Exception:
        logger.debug("thermal-calibration read failed", exc_info=True)
    learned_tau = row is not None and row.get("tau_hours") is not None
    return {
        "calibration": row,  # null until the learner's quality gates pass
        "effective": {
            "tau_hours": round(tl.get_building_tau_hours(), 2),
            "ua_w_per_k": round(tl.get_building_ua_w_per_k(), 1),
            "c_kwh_per_k": round(tl.get_building_thermal_mass_kwh_per_k(), 2),
            "source": "learned" if (
                learned_tau and bool(getattr(config, "THERMAL_LEARNED_VALUES_ENABLED", True))
            ) else "env",
        },
        "learning_enabled": bool(getattr(config, "THERMAL_LEARNING_ENABLED", True)),
        "learned_values_enabled": bool(
            getattr(config, "THERMAL_LEARNED_VALUES_ENABLED", True)
        ),
    }
