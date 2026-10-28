"""``GET /api/v1/insights/recommendations`` — the Insights "O que fazer" list (#879)."""
from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

from fastapi import APIRouter, HTTPException

router = APIRouter(tags=["insights"])


@router.get("/api/v1/insights/recommendations")
async def get_recommendations(end: str | None = None) -> dict[str, Any]:
    """Recommendations + week summary + house numbers. Viewer-safe read; cached
    10 min per (DB, ``end`` day). ``end`` (YYYY-MM-DD, local) is the LAST day of the
    7-day window (default today)."""
    from ...analytics import recommendations as rec

    end_day: date | None = None
    if end:
        try:
            end_day = date.fromisoformat(end)
        except ValueError:
            raise HTTPException(status_code=400, detail="end must be YYYY-MM-DD") from None
    return await asyncio.to_thread(rec.recommendations, end_day)
