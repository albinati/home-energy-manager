"""``GET /api/v1/thermal/lwt-learning`` — LWT coast learning summaries (#838)."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter

router = APIRouter(tags=["thermal"])


def _build(days: int) -> dict[str, Any]:
    from ... import db
    from ...analytics.lwt_learning import day_slots_utc
    from ...config import config

    tz = ZoneInfo(str(config.BULLETPROOF_TIMEZONE))
    daily = []
    for r in db.get_lwt_learning_daily(days):
        payload = r.pop("payload", {}) or {}
        daily.append({**r, **payload})
    yday = datetime.now(tz).date() - timedelta(days=1)
    slots = day_slots_utc(yday, tz)
    z = lambda d: d.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    rows = db.get_lwt_learning_rows(z(slots[0]), z(slots[-1] + timedelta(minutes=30))) if slots else []
    return {
        "days": days,
        "coast_mode": str(getattr(config, "DAIKIN_LWT_COAST_MODE", "setback") or "setback"),
        "ua_pinned_w_per_k": float(getattr(config, "BUILDING_UA_W_PER_K", 200)),
        "daily": daily,
        "yesterday": {"date": yday.isoformat(), "slots": rows},
    }


@router.get("/api/v1/thermal/lwt-learning")
async def get_lwt_learning(days: int = 14) -> dict[str, Any]:
    """Daily UA/k estimates + yesterday's per-slot planned-vs-realised rows. Viewer-safe."""
    days = max(1, min(90, int(days)))
    return await asyncio.to_thread(_build, days)
