"""Watchdog: refresh Agile rate cache (V7 — daily fetch around 16:00 local).

Caches both import and export rates in memory. Export rates are used by the
solver when OCTOPUS_EXPORT_TARIFF_CODE is configured.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from ..config import config
from ..scheduler.agile import fetch_agile_rates, fetch_agile_export_rates


@dataclass
class AgileRateCache:
    """In-memory cache populated by the watchdog."""

    rates: list[dict] = field(default_factory=list)
    export_rates: list[dict] = field(default_factory=list)
    fetched_at_utc: Optional[datetime] = None
    tariff_code: str = ""
    export_tariff_code: str = ""
    error: Optional[str] = None


_CACHE = AgileRateCache()


def get_agile_cache() -> AgileRateCache:
    return _CACHE


def refresh_agile_rates(*, tariff_code: Optional[str] = None) -> AgileRateCache:
    """Fetch the next 48h of Agile import (and export if configured) rates."""
    global _CACHE
    code = (tariff_code or config.OCTOPUS_TARIFF_CODE or "").strip()
    export_code = config.OCTOPUS_EXPORT_TARIFF_CODE or ""
    if not code:
        _CACHE = AgileRateCache(
            rates=[],
            export_rates=[],
            fetched_at_utc=datetime.now(timezone.utc),
            tariff_code="",
            error="OCTOPUS_TARIFF_CODE not set",
        )
        return _CACHE
    try:
        rates = fetch_agile_rates(tariff_code=code)
        export_rates: list[dict] = []
        if export_code:
            try:
                export_rates = fetch_agile_export_rates(export_tariff_code=export_code) or []
            except Exception as exp_exc:
                import logging
                logging.getLogger(__name__).warning("Export rates fetch failed: %s", exp_exc)
        _CACHE = AgileRateCache(
            rates=rates or [],
            export_rates=export_rates,
            fetched_at_utc=datetime.now(timezone.utc),
            tariff_code=code,
            export_tariff_code=export_code,
            error=None if rates else "empty rates response",
        )
    except Exception as e:  # noqa: BLE001 — cache layer should not raise
        _CACHE = AgileRateCache(
            rates=[],
            export_rates=[],
            fetched_at_utc=datetime.now(timezone.utc),
            tariff_code=code,
            error=str(e),
        )
    return _CACHE
