"""Comfort feedback (#833): ``POST/GET /api/v1/comfort/feedback``.

POST is admin-gated by ``ApiV1RoleAuth`` (any non-GET under /api/v1/); GET is
viewer-safe."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

router = APIRouter(tags=["comfort"])


class ComfortFeedbackIn(BaseModel):
    verdict: Literal["cold", "ok", "hot"]
    room: str | None = Field(default=None, max_length=60)
    note: str | None = Field(default=None, max_length=500)


@router.post("/api/v1/comfort/feedback")
async def post_comfort_feedback(body: ComfortFeedbackIn, source: str = "api") -> dict[str, Any]:
    from ... import db

    src = source if source in ("api", "ui") else "api"
    try:
        return await asyncio.to_thread(
            lambda: db.insert_comfort_feedback(verdict=body.verdict, source=src,
                                               room=body.room, note=body.note))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _get(days: int) -> dict[str, Any]:
    from ... import db
    from ...analytics import comfort_feedback as cf

    rows = db.get_comfort_feedback(days)
    by_room: dict[str, dict[str, int]] = {}
    for r in rows:
        if r.get("room"):
            by_room.setdefault(r["room"], {"cold": 0, "ok": 0, "hot": 0})[r["verdict"]] += 1
    today = datetime.now(UTC).astimezone(cf._tz()).date()
    return {"days": days, "rows": rows, "by_room": by_room,
            "weekly": cf.weekly_summary(today - timedelta(days=6))}


@router.get("/api/v1/comfort/feedback")
async def get_comfort_feedback(days: int = Query(30, ge=1, le=365)) -> dict[str, Any]:
    return await asyncio.to_thread(_get, days)
