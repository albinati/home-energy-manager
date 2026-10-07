"""``GET /api/v1/plan/fronts`` — the Home page's one-stop plan read (#821)."""
from __future__ import annotations

import asyncio
import logging
import datetime as _dt
from typing import Any

from fastapi import APIRouter, HTTPException

logger = logging.getLogger(__name__)

router = APIRouter(tags=["plan"])


@router.get("/api/v1/plan/fronts")
async def get_plan_fronts(date: str | None = None) -> dict[str, Any]:  # noqa: A002 — query name per contract
    """What each front (battery / tank / heating) will do on ``date`` (local,
    default today), consumption probability, spend score, tariff comparison.
    Viewer-safe; every section is independently guarded; cached 60 s."""
    from ...analytics.plan_fronts import plan_fronts

    day: _dt.date | None = None
    if date:
        try:
            day = _dt.date.fromisoformat(date)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD") from exc
    return await asyncio.to_thread(plan_fronts, day)
