"""Viewer-activity tracker for the freshness boost (see runner.py's
``bulletproof_viewer_boost_job``).

The cockpit SPA polls ``/api/v1/cockpit/now`` every ~10 s while a tab is
visible (usePoll pauses hidden tabs), so that request stream is a reliable
"someone is actually looking" signal. The handler calls
:func:`mark_viewer_active` on every hit; the background boost job asks
:func:`viewer_active` to decide whether spending vendor quota on a fresher
snapshot would be seen by anyone.

Monotonic clock only — wall-clock jumps (NTP, suspend) must not fake or
kill activity. Module-level state, same singleton pattern as the vendor
service caches; a bare float write/read is atomic under the GIL so no lock.

TOPOLOGY ASSUMPTION: the API handler (writer) and the APScheduler boost job
(reader) share one process — true today (single uvicorn worker, scheduler
started in-process by the API lifespan). If the app ever moves to multiple
workers or an external scheduler process, this signal splits brain and the
boost silently stops firing; it would need to move to SQLite/runtime_settings.
"""
from __future__ import annotations

import time

_last_viewer_hit_monotonic: float | None = None


def mark_viewer_active() -> None:
    """Record a viewer-driven request (called from the /cockpit/now handler)."""
    global _last_viewer_hit_monotonic
    _last_viewer_hit_monotonic = time.monotonic()


def viewer_active(window_seconds: float) -> bool:
    """True when a viewer request landed within the last *window_seconds*."""
    if _last_viewer_hit_monotonic is None:
        return False
    return (time.monotonic() - _last_viewer_hit_monotonic) < window_seconds


def seconds_since_last_viewer() -> float | None:
    """Age of the last viewer hit, or None if never seen (for status surfaces)."""
    if _last_viewer_hit_monotonic is None:
        return None
    return time.monotonic() - _last_viewer_hit_monotonic
