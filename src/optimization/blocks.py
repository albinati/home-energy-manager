"""Normalize Octopus Agile `results` into consecutive half-hour slots for the solver."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from ..scheduler.agile import _parse_iso


def _floor_to_half_hour(dt: datetime) -> datetime:
    """UTC-aware floor to 00 or 30 minutes."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    minute = 0 if dt.minute < 30 else 30
    return dt.replace(minute=minute, second=0, microsecond=0)


def _price_at(
    cursor: datetime,
    intervals: list[tuple[datetime, datetime, float]],
) -> float:
    """Pick the Agile price covering ``cursor``; else last interval starting at or before cursor."""
    for vf, vt, p in intervals:
        if vf <= cursor < vt:
            return p
    for vf, _vt, p in reversed(intervals):
        if cursor >= vf:
            return p
    return intervals[0][2] if intervals else 0.0


def build_slot_list(
    rates: list[dict],
    *,
    period_from: Optional[datetime] = None,
    num_slots: int = 48,
) -> list[dict]:
    """Return ``num_slots`` consecutive half-hours with ``value_inc_vat`` from Agile data."""
    if not rates:
        return []

    now = datetime.now(timezone.utc)
    start = _floor_to_half_hour(period_from or now)

    intervals: list[tuple[datetime, datetime, float]] = []
    for r in rates:
        vf = _parse_iso(r.get("valid_from"))
        if vf is None:
            continue
        vt = _parse_iso(r.get("valid_to"))
        if vt is None:
            vt = vf + timedelta(minutes=30)
        intervals.append((vf, vt, float(r.get("value_inc_vat") or 0)))
    intervals.sort(key=lambda x: x[0])
    if not intervals:
        return []

    slots: list[dict] = []
    cursor = start
    for _ in range(num_slots):
        slot_end = cursor + timedelta(minutes=30)
        price = _price_at(cursor, intervals)
        slots.append(
            {
                "value_inc_vat": price,
                "valid_from": cursor.isoformat().replace("+00:00", "Z"),
                "valid_to": slot_end.isoformat().replace("+00:00", "Z"),
                "_from_dt": cursor,
                "_to_dt": slot_end,
            }
        )
        cursor = slot_end

    return slots
