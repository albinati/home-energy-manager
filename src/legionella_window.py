"""Single source of truth for the weekly legionella stand-off window.

The Onecta firmware runs its thermal-shock cycle on the unit's LOCAL clock
(Europe/London), so a fixed UTC window is wrong by 1 h for half the year.
Two modes, selected by ``DHW_LEGIONELLA_STANDOFF_START_LOCAL``:

* empty (default) -- the UTC knobs ``DHW_LEGIONELLA_STANDOFF_DOW`` /
  ``_START_HOUR_UTC`` / ``_START_MINUTE_UTC`` / ``_DURATION_MINUTES``,
  evaluated on the UTC calendar (the historical behaviour, bit-for-bit).
* ``"HH:MM"`` -- the window starts at that wall-clock time in
  ``BULLETPROOF_TIMEZONE`` on the LOCAL weekday ``..._DOW``; duration unchanged.
  An unparsable value logs one warning and falls back to the UTC knobs.

Assumption (both modes): the window does NOT cross midnight -- UTC midnight in
UTC mode, LOCAL midnight in local mode. Every reader (reconciler guard, LP
budget, bucket-bias exclusion, schedule views) goes through this module.

Deliberately imports only ``config`` so any module can use it without cycles.
"""
from __future__ import annotations

import logging
import re
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from .config import config

logger = logging.getLogger(__name__)

_WARNED_BAD_LOCAL: set[str] = set()
_HHMM = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$")


def _local_start() -> tuple[int, int] | None:
    """Parsed ``(hour, minute)`` of START_LOCAL, or None for UTC mode / invalid."""
    raw = str(getattr(config, "DHW_LEGIONELLA_STANDOFF_START_LOCAL", "") or "").strip()
    if not raw:
        return None
    m = _HHMM.match(raw)
    if m and int(m.group(1)) <= 23 and int(m.group(2)) <= 59:
        return int(m.group(1)), int(m.group(2))
    if raw not in _WARNED_BAD_LOCAL:
        _WARNED_BAD_LOCAL.add(raw)
        logger.warning(
            "DHW_LEGIONELLA_STANDOFF_START_LOCAL=%r is not HH:MM; "
            "falling back to the UTC stand-off knobs", raw,
        )
    return None


def _tz() -> ZoneInfo:
    return ZoneInfo(str(getattr(config, "BULLETPROOF_TIMEZONE", "") or "Europe/London"))


def is_local_mode() -> bool:
    return _local_start() is not None


def standoff_window_for_date(d: date) -> tuple[datetime, datetime] | None:
    """Window ``(start_utc, end_utc)`` that STARTS on calendar day ``d`` (the
    local date in local mode, the UTC date in UTC mode), or None when ``d`` is
    not the configured weekday. Does not check ``DHW_LEGIONELLA_STANDOFF_ENABLED``.
    """
    dow = int(getattr(config, "DHW_LEGIONELLA_STANDOFF_DOW", 6))
    dur = max(1, int(getattr(config, "DHW_LEGIONELLA_STANDOFF_DURATION_MINUTES", 120)))
    if d.weekday() != dow:
        return None
    loc = _local_start()
    if loc is not None:
        start = datetime(d.year, d.month, d.day, loc[0], loc[1], tzinfo=_tz()).astimezone(UTC)
    else:
        start = datetime(
            d.year, d.month, d.day,
            int(getattr(config, "DHW_LEGIONELLA_STANDOFF_START_HOUR_UTC", 11)),
            int(getattr(config, "DHW_LEGIONELLA_STANDOFF_START_MINUTE_UTC", 0)),
            tzinfo=UTC,
        )
    return start, start + timedelta(minutes=dur)


def standoff_windows(start_day: date, end_day: date) -> list[tuple[datetime, datetime]]:
    """All windows starting on days ``start_day..end_day`` inclusive."""
    out = []
    d = start_day
    while d <= end_day:
        w = standoff_window_for_date(d)
        if w is not None:
            out.append(w)
        d += timedelta(days=1)
    return out


def in_standoff_window(when: datetime) -> bool:
    """True when the aware/naive-UTC instant ``when`` lies inside a window."""
    when = when.replace(tzinfo=UTC) if when.tzinfo is None else when.astimezone(UTC)
    day = when.astimezone(_tz()).date() if is_local_mode() else when.date()
    w = standoff_window_for_date(day)
    return w is not None and w[0] <= when < w[1]
