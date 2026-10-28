"""``GET /api/v1/insights/recommendations`` — the Insights "O que fazer" list (#879)."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException

router = APIRouter(tags=["insights"])


def _parse_day(name: str, v: str) -> date:
    try:
        return date.fromisoformat(v)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"{name} must be YYYY-MM-DD") from None


@router.get("/api/v1/insights/recommendations")
async def get_recommendations(start: str | None = None, end: str | None = None,
                              fresh: bool = False) -> dict[str, Any]:
    """Recommendations + week summary + house numbers. Viewer-safe read; cached 10 min
    per (DB, start, end) unless ``fresh=1``. ``start``/``end`` (YYYY-MM-DD, local) bound
    the week (at most 7 days; ``end`` defaults to today and may not be in the future or
    before the first smart-tariff day; ``start`` defaults to that week's Monday)."""
    from ...analytics import pnl
    from ...analytics import recommendations as rec
    from ...config import config

    try:
        tz = ZoneInfo(str(config.BULLETPROOF_TIMEZONE))
    except Exception:  # noqa: BLE001
        tz = ZoneInfo("UTC")
    today = datetime.now(tz).date()
    end_day = _parse_day("end", end) if end else today
    start_day = _parse_day("start", start) if start else end_day - timedelta(days=end_day.weekday())
    if end_day > today:
        raise HTTPException(status_code=400, detail="end is in the future")
    floor = pnl._agile_start_date()  # noqa: SLF001 — first day of HEM-managed smart-tariff history
    if floor is not None and end_day < floor:
        raise HTTPException(status_code=400, detail=f"end is before the first data day ({floor.isoformat()})")
    if start_day > end_day or (end_day - start_day).days > 6:
        raise HTTPException(status_code=400, detail="start..end must be 1 to 7 days")
    return await asyncio.to_thread(rec.recommendations, end_day, start=start_day, use_cache=not fresh)
