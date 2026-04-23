"""Household presets for the Bulletproof planner (comfort vs export behaviour)."""

from __future__ import annotations

import logging
from enum import Enum

logger = logging.getLogger(__name__)


class OperationPreset(str, Enum):
    """Drives planner aggressiveness and away/export rules."""

    NORMAL = "normal"
    GUESTS = "guests"
    TRAVEL = "travel"
    AWAY = "away"

    @classmethod
    def _missing_(cls, value: object) -> "OperationPreset | None":
        # v10: 'boost' was retired — silently map to NORMAL with a one-time deprecation log.
        if isinstance(value, str) and value.lower() == "boost":
            logger.warning(
                "OperationPreset 'boost' is deprecated (retired in v10) — using 'normal'."
            )
            return cls.NORMAL
        return None
