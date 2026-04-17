"""Household presets for the Bulletproof planner (comfort vs export behaviour)."""

from __future__ import annotations

from enum import Enum


class OperationPreset(str, Enum):
    """Drives planner aggressiveness and away/export rules."""

    NORMAL = "normal"
    GUESTS = "guests"
    TRAVEL = "travel"
    AWAY = "away"
    BOOST = "boost"
