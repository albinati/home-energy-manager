"""Nightly post-hoc reconciliation of ``execution_log`` against Octopus's
half-hourly smart-meter readings.

The heartbeat writes per-slot rows tagged ``source="estimated"`` using a
single ``Fox.load_power`` sample multiplied by 0.5 h. That's a fast,
quota-free input for the live cockpit, but it's noisy enough that the
morning/night brief PnL (read straight from ``execution_log``) can
disagree with reality by 30-50 % on any given day — a heavy appliance
running mid-slot but not at the heartbeat tick is invisible.

This module pulls yesterday's actual half-hourly consumption from the
Octopus API and rewrites the affected rows with ``source="metered"``,
recalculating ``cost_realised_pence``, ``cost_svt_shadow_pence``,
``cost_fixed_shadow_pence``, and the two ``delta_*`` columns from the
prices that were locked in at heartbeat write time.

Usage from the scheduler: a daily cron at ~04:00 local fires
:func:`backfill_yesterday`, which is the no-args helper used by
:func:`src.scheduler.runner.bulletproof_consumption_backfill_job`.
Tests and ad-hoc backfills call :func:`backfill_for_date(target_local_date)`.

Octopus's consumption endpoint is typically delayed ~24 h relative to
the slot. We fire at 04:00 local (= ~03:00–04:00 UTC) so yesterday's
data is reliably available.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .. import db
from ..config import config

logger = logging.getLogger(__name__)


@dataclass
class BackfillResult:
    target_date: str          # local-date ISO
    slots_fetched: int        # rows returned by Octopus
    slots_updated: int        # execution_log rows successfully rewritten
    slots_missing: int        # Octopus had data but no execution_log row matched
    error: str | None = None


def _octopus_credentials_ready() -> bool:
    return bool(
        getattr(config, "OCTOPUS_API_KEY", None)
        and (
            getattr(config, "OCTOPUS_MPAN_IMPORT", None)
            or getattr(config, "OCTOPUS_MPAN_1", None)
            or getattr(config, "OCTOPUS_ACCOUNT_NUMBER", None)
        )
    )


def _local_day_window_utc(target_date: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """Return ``(period_from_utc, period_to_utc)`` covering the local day,
    DST-safe via ZoneInfo.fold semantics."""
    local_start = datetime.combine(target_date, time(0, 0), tzinfo=tz)
    local_end = local_start + timedelta(days=1)
    return local_start.astimezone(UTC), local_end.astimezone(UTC)


def _slot_iso_for(interval_start: datetime) -> str:
    """Match ``execution_log.timestamp`` formatting written by the heartbeat.

    The heartbeat stores ``datetime.now(UTC).isoformat()`` which yields
    ``2026-04-29T13:30:00.123456+00:00``. The Octopus endpoint returns
    aligned slot starts at exact half-hour ticks (no microseconds), so we
    truncate to seconds and accept any matching row whose ``timestamp``
    starts with our normalised prefix.

    Returns the truncated ISO string (with microseconds zeroed) so the
    caller can pass it straight to ``db.update_execution_log_metered``.
    """
    aligned = interval_start.astimezone(UTC).replace(microsecond=0)
    return aligned.isoformat()


def backfill_for_date(target_local_date: date) -> BackfillResult:
    """Fetch half-hourly consumption for ``target_local_date`` and rewrite
    ``execution_log`` rows. Returns a structured result for logging /
    test assertions; never raises."""
    iso = target_local_date.isoformat()
    if not _octopus_credentials_ready():
        return BackfillResult(
            target_date=iso, slots_fetched=0, slots_updated=0, slots_missing=0,
            error="octopus_credentials_missing",
        )

    try:
        from ..energy.octopus_client import fetch_consumption, get_mpan_roles
    except Exception as e:
        return BackfillResult(
            target_date=iso, slots_fetched=0, slots_updated=0, slots_missing=0,
            error=f"import_error: {e}",
        )

    try:
        roles = get_mpan_roles()
    except Exception as e:
        return BackfillResult(
            target_date=iso, slots_fetched=0, slots_updated=0, slots_missing=0,
            error=f"mpan_role_resolution_failed: {e}",
        )

    if not (roles.import_mpan and roles.import_serial):
        return BackfillResult(
            target_date=iso, slots_fetched=0, slots_updated=0, slots_missing=0,
            error="import_meter_not_configured",
        )

    tz = ZoneInfo(getattr(config, "BULLETPROOF_TIMEZONE", "Europe/London"))
    period_from, period_to = _local_day_window_utc(target_local_date, tz)

    try:
        slots = fetch_consumption(
            mpan=str(roles.import_mpan),
            serial=str(roles.import_serial),
            period_from=period_from,
            period_to=period_to,
        )
    except Exception as e:
        return BackfillResult(
            target_date=iso, slots_fetched=0, slots_updated=0, slots_missing=0,
            error=f"octopus_fetch_failed: {e}",
        )

    updated = 0
    missing = 0
    for s in slots:
        ts = _slot_iso_for(s.interval_start)
        ok = db.update_execution_log_metered(ts, s.consumption_kwh)
        if ok:
            updated += 1
        else:
            missing += 1

    logger.info(
        "consumption_backfill: date=%s fetched=%d updated=%d missing=%d "
        "(import_mpan=%s)",
        iso, len(slots), updated, missing, roles.import_mpan,
    )

    return BackfillResult(
        target_date=iso,
        slots_fetched=len(slots),
        slots_updated=updated,
        slots_missing=missing,
    )


def backfill_yesterday() -> BackfillResult:
    """Default cron entry point — backfill the local-date that is 1 day before now."""
    tz = ZoneInfo(getattr(config, "BULLETPROOF_TIMEZONE", "Europe/London"))
    target = (datetime.now(tz) - timedelta(days=1)).date()
    return backfill_for_date(target)
