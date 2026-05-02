"""Publish Octopus rate windows to a shared Google Calendar.

**Wipe-and-recreate per re-publish.** Events are tagged with
``extendedProperties.private.hem_publisher = "1"`` so any HEM publisher
(prod, sim box, manual one-shot) can list and sweep them. On every run:

1. List existing tagged events for the horizon dates.
2. Compute new windows from current Octopus rates.
3. Hash-compare. If unchanged → no-op (zero API mutations).
4. If changed → delete all tagged events for that day, then create fresh.

This keeps the family calendar clean (no leftover stale events) and is
robust to DB loss / sim-prod handoff (no SQLite state needed for diff).

All API mutations send ``sendUpdates="none"`` so the family receives no
notifications when events are created, updated, or deleted.

Called from ``scheduler.runner.bulletproof_calendar_publish_job``. A
failure here is logged and swallowed by the caller — must never break
the long-running scheduler.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .. import db
from ..config import config
from .auth import GoogleCalendarAuthError, load_credentials
from .tiers import Slot, Window, classify_day, format_event

logger = logging.getLogger(__name__)

# Single key/value used as the "this event was created by HEM" marker.
# Identical across prod/sim/manual runs so cleanup is universal.
HEM_TAG_KEY = "hem_publisher"
HEM_TAG_VALUE = "1"


def _to_rfc3339(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.isoformat().replace("+00:00", "Z")


def _parse_slot_dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _build_event_body(window: Window, tz_name: str, plan_date_iso: str) -> dict[str, Any]:
    summary, description = format_event(window)
    return {
        "summary": summary,
        "description": description,
        "colorId": window.tier.color_id,
        "start": {"dateTime": _to_rfc3339(window.start_utc), "timeZone": tz_name},
        "end": {"dateTime": _to_rfc3339(window.end_utc), "timeZone": tz_name},
        "transparency": "transparent",
        "reminders": {"useDefault": False, "overrides": []},
        "extendedProperties": {
            "private": {
                HEM_TAG_KEY: HEM_TAG_VALUE,
                "hem_horizon_date": plan_date_iso,
                "hem_tier": window.tier.key,
            },
        },
    }


def _list_existing_for_day(service, cal_id: str, local_date: date, tz: ZoneInfo) -> list[dict[str, Any]]:
    """Return all HEM-tagged events whose start falls on ``local_date``."""
    day_start = datetime.combine(local_date, datetime.min.time(), tzinfo=tz)
    day_end = day_start + timedelta(days=1)
    items: list[dict[str, Any]] = []
    page_token: str | None = None
    while True:
        resp = service.events().list(
            calendarId=cal_id,
            privateExtendedProperty=f"{HEM_TAG_KEY}={HEM_TAG_VALUE}",
            timeMin=_to_rfc3339(day_start.astimezone(UTC)),
            timeMax=_to_rfc3339(day_end.astimezone(UTC)),
            singleEvents=True,
            orderBy="startTime",
            maxResults=100,
            pageToken=page_token,
        ).execute()
        items.extend(resp.get("items", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return items


def _events_match_windows(existing: list[dict[str, Any]], windows: list[Window]) -> bool:
    """True when the calendar already shows exactly these windows.

    Compared on summary + start + end + colorId (everything visible to the
    family). When this returns True, the publisher does nothing — no API
    calls, no event-update notifications.
    """
    if len(existing) != len(windows):
        return False
    sorted_existing = sorted(existing, key=lambda e: e["start"]["dateTime"])
    sorted_windows = sorted(windows, key=lambda w: w.start_utc)
    for ev, w in zip(sorted_existing, sorted_windows):
        new_summary, _ = format_event(w)
        if ev.get("summary") != new_summary:
            return False
        if ev.get("colorId") != w.tier.color_id:
            return False
        if _parse_slot_dt(ev["start"]["dateTime"]) != w.start_utc:
            return False
        if _parse_slot_dt(ev["end"]["dateTime"]) != w.end_utc:
            return False
    return True


@dataclass
class _DayResult:
    date: str
    windows: int = 0
    deleted: int = 0
    created: int = 0
    skipped_unchanged: bool = False
    skipped_reason: str | None = None


def _publish_day(service, local_date: date, tz: ZoneInfo) -> _DayResult:
    result = _DayResult(date=local_date.isoformat())
    cal_id = config.GOOGLE_CALENDAR_ID
    tariff = (config.OCTOPUS_TARIFF_CODE or "").strip()

    rows = db.get_agile_rates_slots_for_local_day(tariff, local_date, tz_name=str(tz))
    if not rows:
        result.skipped_reason = "no_rates"
        return result

    slots = [
        Slot(
            start_utc=_parse_slot_dt(r["valid_from"]),
            end_utc=_parse_slot_dt(r["valid_to"]),
            price_p=float(r["value_inc_vat"]),
        )
        for r in rows
    ]
    windows = classify_day(slots)
    result.windows = len(windows)

    existing = _list_existing_for_day(service, cal_id, local_date, tz)

    if _events_match_windows(existing, windows):
        result.skipped_unchanged = True
        return result

    # Wipe-and-recreate. Cheaper for a single batch than incremental diff
    # given the small per-day window count and simpler to reason about.
    for ev in existing:
        try:
            service.events().delete(
                calendarId=cal_id, eventId=ev["id"], sendUpdates="none",
            ).execute()
            result.deleted += 1
        except Exception as e:
            logger.debug("delete %s failed (already gone?): %s", ev.get("id"), e)

    for w in windows:
        body = _build_event_body(w, str(tz), local_date.isoformat())
        try:
            event = service.events().insert(
                calendarId=cal_id, body=body, sendUpdates="none",
            ).execute()
            # Audit-only: record what we created. The diff path no longer
            # consults this table, but it's a useful trail for debugging.
            db.upsert_calendar_event(
                calendar_id=cal_id,
                plan_date=local_date.isoformat(),
                slot_start_utc=_to_rfc3339(w.start_utc),
                slot_end_utc=_to_rfc3339(w.end_utc),
                tier=w.tier.key,
                price_min=w.price_min,
                price_max=w.price_max,
                price_mean=w.price_mean,
                google_event_id=event["id"],
            )
            result.created += 1
        except Exception as e:
            logger.warning("insert failed for window %s: %s", w.tier.key, e)

    return result


def publish_horizon() -> dict[str, Any]:
    """Publish today + tomorrow when Octopus rates are available.

    Returns a per-day summary. ``skipped_reason="no_rates"`` on a day
    means Octopus hasn't published it yet — the next scheduled retry
    cron firing will pick it up.
    """
    if not config.GOOGLE_CALENDAR_ENABLED:
        return {"ok": False, "skipped": "GOOGLE_CALENDAR_ENABLED=false"}
    if not config.GOOGLE_CALENDAR_ID:
        return {"ok": False, "error": "GOOGLE_CALENDAR_ID not set"}

    creds = load_credentials()

    try:
        from googleapiclient.discovery import build
    except ImportError as e:
        raise GoogleCalendarAuthError(
            "google-api-python-client not installed; pip install google-api-python-client"
        ) from e

    service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    tz = ZoneInfo(config.GOOGLE_CALENDAR_TIMEZONE)
    today = datetime.now(tz).date()
    tomorrow = today + timedelta(days=1)

    days: list[dict[str, Any]] = []
    for d in (today, tomorrow):
        r = _publish_day(service, d, tz)
        days.append({
            "date": r.date,
            "windows": r.windows,
            "deleted": r.deleted,
            "created": r.created,
            "skipped_unchanged": r.skipped_unchanged,
            "skipped_reason": r.skipped_reason,
        })
    return {"ok": True, "days": days}


def cleanup_legacy_events() -> dict[str, int]:
    """One-shot helper: delete HEM events created BEFORE this tag-and-sweep
    refactor (tracked in SQLite ``calendar_events`` but lacking the
    ``extendedProperties.private`` tag). Safe to run multiple times — only
    deletes IDs that still exist on the calendar.

    Used once during the upgrade from the v1 (SQLite-only diff) publisher
    to v2 (tag-and-sweep). After the sweep the SQLite table is truncated.
    """
    if not config.GOOGLE_CALENDAR_ENABLED or not config.GOOGLE_CALENDAR_ID:
        return {"deleted": 0, "skipped": -1}

    creds = load_credentials()
    from googleapiclient.discovery import build  # noqa: PLC0415
    service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    cal_id = config.GOOGLE_CALENDAR_ID

    import sqlite3
    conn = sqlite3.connect(str(config.DB_PATH))
    rows = conn.execute("SELECT google_event_id FROM calendar_events").fetchall()
    deleted, missing = 0, 0
    for (eid,) in rows:
        try:
            service.events().delete(
                calendarId=cal_id, eventId=eid, sendUpdates="none",
            ).execute()
            deleted += 1
        except Exception:
            missing += 1
    conn.execute("DELETE FROM calendar_events")
    conn.commit()
    conn.close()
    return {"deleted": deleted, "missing": missing}
