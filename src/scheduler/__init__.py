"""Agile-aware Daikin ASHP scheduler — adjusts LWT by Octopus Agile price.

Important: avoid importing `runner` at module import time to prevent circular imports
when other packages import `src.scheduler.agile`.
"""

from .agile import fetch_agile_rates, get_current_and_next_slots
from .daikin import compute_lwt_adjustment, apply_scheduler_offset, run_daikin_scheduler_tick


def get_scheduler_status():
    from .runner import get_scheduler_status as _get_scheduler_status
    return _get_scheduler_status()


def pause_scheduler():
    from .runner import pause_scheduler as _pause_scheduler
    return _pause_scheduler()


def resume_scheduler():
    from .runner import resume_scheduler as _resume_scheduler
    return _resume_scheduler()


def run_scheduler_tick():
    from .runner import run_scheduler_tick as _run_scheduler_tick
    return _run_scheduler_tick()


__all__ = [
    "fetch_agile_rates",
    "get_current_and_next_slots",
    "compute_lwt_adjustment",
    "apply_scheduler_offset",
    "get_scheduler_status",
    "pause_scheduler",
    "resume_scheduler",
    "run_scheduler_tick",
]
