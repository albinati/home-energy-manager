"""Watchdog: refresh Agile rate cache (V7 — daily fetch around 16:00 local)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from ..config import config
from ..scheduler.agile import fetch_agile_rates


@dataclass
class AgileRateCache:
    """In-memory cache populated by the watchdog."""

    rates: list[dict] = field(default_factory=list)
    fetched_at_utc: Optional[datetime] = None
    tariff_code: str = ""
    error: Optional[str] = None


_CACHE = AgileRateCache()


def get_agile_cache() -> AgileRateCache:
    return _CACHE


def refresh_agile_rates(*, tariff_code: Optional[str] = None) -> AgileRateCache:
    """Fetch the next 48h of Agile half-hourly rates and store in process memory."""
    global _CACHE
    code = (tariff_code or config.OCTOPUS_TARIFF_CODE or "").strip()
    if not code:
        _CACHE = AgileRateCache(
            rates=[],
            fetched_at_utc=datetime.now(timezone.utc),
            tariff_code="",
            error="OCTOPUS_TARIFF_CODE not set",
        )
        return _CACHE
    try:
        rates = fetch_agile_rates(tariff_code=code)
        _CACHE = AgileRateCache(
            rates=rates or [],
            fetched_at_utc=datetime.now(timezone.utc),
            tariff_code=code,
            error=None if rates else "empty rates response",
        )
    except Exception as e:  # noqa: BLE001 — cache layer should not raise
        _CACHE = AgileRateCache(
            rates=[],
            fetched_at_utc=datetime.now(timezone.utc),
            tariff_code=code,
            error=str(e),
        )
    return _CACHE
