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
        merged = {**r, **payload}
        # #843: the coast-only UA is C x decay-rate with C = tau x UA_pin -> circular.
        merged.setdefault("ua_from_tau_scaled_w_per_k", r.get("ua_est_w_per_k"))
        merged.setdefault("ua_from_tau_scaled_night_w_per_k", payload.get("ua_est_night_w_per_k"))
        merged["ua_est_circular"] = True
        daily.append(merged)
    yday = datetime.now(tz).date() - timedelta(days=1)
    slots = day_slots_utc(yday, tz)
    z = lambda d: d.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    rows = db.get_lwt_learning_rows(z(slots[0]), z(slots[-1] + timedelta(minutes=30))) if slots else []
    return {
        "days": days,
        "timezone": str(config.BULLETPROOF_TIMEZONE),
        "coast_mode": str(getattr(config, "DAIKIN_LWT_COAST_MODE", "setback") or "setback"),
        "ua_pinned_w_per_k": float(getattr(config, "BUILDING_UA_W_PER_K", 200)),
        # #854: the gains the LP is steering with right now (0 = off), next to the learned ones
        "internal_gain_pinned_kw": float(getattr(config, "LP_W3_INTERNAL_GAIN_KW", 0.0)),
        "solar_gain_pinned_kw_per_pv_kw": float(getattr(config, "LP_W3_SOLAR_GAIN_KW_PER_PV_KW", 0.0)),
        "daily": daily,
        "yesterday": {"date": yday.isoformat(), "slots": rows},
    }


@router.get("/api/v1/thermal/lwt-learning")
async def get_lwt_learning(days: int = 14) -> dict[str, Any]:
    """Daily UA/k estimates + yesterday's per-slot planned-vs-realised rows. Viewer-safe."""
    days = max(1, min(90, int(days)))
    return await asyncio.to_thread(_build, days)
