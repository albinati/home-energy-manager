"""``GET /api/v1/scorecard/cosy`` — the daily Cosy scorecard rows (#831)."""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter

router = APIRouter(tags=["scorecard"])


def _rows(days: int) -> list[dict[str, Any]]:
    from ...analytics.cosy_scorecard import get_scorecards

    out = []
    for r in get_scorecards(days):
        payload = r.pop("payload", {}) or {}
        out.append({**r, **payload})
    return out


@router.get("/api/v1/scorecard/cosy")
async def get_cosy_scorecard(days: int = 14) -> dict[str, Any]:
    """Last ``days`` (clamped 1..90) scored days, newest first. Viewer-safe,
    read-only. Each row carries the indexed columns plus the full payload
    sections (spend, bands, battery, comfort, tank, lwt, ops, money)."""
    days = max(1, min(90, int(days)))
    rows = await asyncio.to_thread(_rows, days)
    return {"days": days, "rows": rows}
