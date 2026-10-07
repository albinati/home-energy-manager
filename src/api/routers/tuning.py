"""Weekly fine-tuning review API (#832) — suggestions only, nothing is applied."""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException

router = APIRouter(tags=["tuning"])


@router.get("/api/v1/tuning/suggestions")
async def get_tuning_suggestions(weeks: int = 4) -> dict[str, Any]:
    """Persisted suggestions for the last ``weeks`` reviews (viewer-safe read)."""
    from ... import db
    weeks = max(1, min(int(weeks), 26))
    rows = await asyncio.to_thread(db.list_tuning_suggestions, weeks)
    return {"weeks": weeks, "suggestions": rows}


@router.post("/api/v1/tuning/run")
async def post_tuning_run() -> dict[str, Any]:
    """Run the review now (admin via the role middleware: any non-safe method).
    Replays ~12 variants x 7 days, so it can take minutes; single-flight (409
    when one is already running). Persists and returns the rows."""
    from ... import tuning_review as tr
    try:
        return await asyncio.to_thread(tr.run_review)
    except tr.ReviewBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
