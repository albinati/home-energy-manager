"""Household presets for the Bulletproof planner (comfort vs export behaviour)."""

from __future__ import annotations

import logging
from enum import Enum

logger = logging.getLogger(__name__)


# Module-level dedup: log each deprecated value once per process so the
# warning surfaces on startup but doesn't spam every heartbeat tick.
_LEGACY_LOGGED: set[str] = set()


class OperationPreset(str, Enum):
    """Drives planner aggressiveness and away/export rules.

    Three valid values as of PR A (mode-collapse):

    * ``NORMAL`` — family at home, baseline DHW + selective peak-export.
    * ``GUESTS`` — extra DHW demand (evening + morning visitor showers).
    * ``VACATION`` — DHW off, battery in pure arbitrage mode (peak-export
      aggressive, grid-charging disabled).

    Legacy values silently map via :meth:`_missing_`:

    * ``"travel"``, ``"away"`` → ``VACATION``
    * ``"boost"`` → ``NORMAL``
    """

    NORMAL = "normal"
    GUESTS = "guests"
    VACATION = "vacation"

    @classmethod
    def _missing_(cls, value: object) -> "OperationPreset | None":
        if not isinstance(value, str):
            return None
        v = value.lower()
        if v in ("travel", "away"):
            if v not in _LEGACY_LOGGED:
                _LEGACY_LOGGED.add(v)
                logger.warning(
                    "OperationPreset %r is deprecated (PR A mode-collapse) — using 'vacation'.",
                    v,
                )
            return cls.VACATION
        if v == "boost":
            if v not in _LEGACY_LOGGED:
                _LEGACY_LOGGED.add(v)
                logger.warning(
                    "OperationPreset 'boost' is deprecated (retired in v10) — using 'normal'."
                )
            return cls.NORMAL
        return None


# Alias for forward-looking call sites. The household intent is the
# primary mental model now; ``OperationPreset`` is the legacy name kept
# to avoid churn in 18+ existing files. Both refer to the same enum.
HouseholdMode = OperationPreset
