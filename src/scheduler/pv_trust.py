"""PV-sufficiency guard rail (incident 2026-05-15).

Under ``ENERGY_STRATEGY_MODE=strict_savings`` (the household's stated policy:
near-zero grid cost over peak-export profit), the LP previously force-charged
the battery from grid in the cheap morning window even on sunny days,
leaving the battery 100% by midday and exporting subsequent PV at the
Outgoing rate instead of self-consuming. This module decides whether the LP
should add ``chg[i] ≤ pv_use[i]`` to today-slots strictly before the first
peak-tariff slot — i.e. block grid → battery when today's PV forecast is
enough to fill the battery on its own.

Inert under ``savings_first`` (peak-export-enabled mode). The per-hour
forecast bias (the AM-over / PM-under asymmetry the calibration tables
already capture) is OUT OF SCOPE here — see
``src.weather.compute_pv_calibration_hourly_table`` and the daily refresh
cron in ``runner.bulletproof_pv_calibration_refresh_job``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC
from typing import Any

from ..config import config

logger = logging.getLogger(__name__)


@dataclass
class PvSufficiencyGuardDiag:
    """Audit data for the PV-sufficiency guard rail decision.

    PR C: the ``strict_savings`` field is kept for snapshot back-compat
    but is now always reported as ``False`` (the strict_savings mode was
    removed). The guard is enabled by default for every solve.
    """

    enabled: bool = False
    strict_savings: bool = False
    applied: bool = False
    reason: str = ""
    forecast_pv_today_kwh: float = 0.0
    expected_load_today_kwh: float = 0.0
    battery_headroom_kwh: float = 0.0
    demand_kwh: float = 0.0
    margin: float = 1.0
    first_peak_slot_idx: int | None = None
    pre_peak_slot_indices: list[int] = field(default_factory=list)

    def to_snapshot_dict(self) -> dict[str, Any]:
        """Compact dict for ``exogenous_snapshot_json.pv_sufficiency_guard``."""
        return {
            "enabled": self.enabled,
            "strict_savings": self.strict_savings,
            "applied": self.applied,
            "reason": self.reason,
            "forecast_pv_today_kwh": round(self.forecast_pv_today_kwh, 3),
            "expected_load_today_kwh": round(self.expected_load_today_kwh, 3),
            "battery_headroom_kwh": round(self.battery_headroom_kwh, 3),
            "demand_kwh": round(self.demand_kwh, 3),
            "margin": self.margin,
            "first_peak_slot_idx": self.first_peak_slot_idx,
            "n_pre_peak_slots": len(self.pre_peak_slot_indices),
        }


def evaluate_pv_sufficiency_guard(
    *,
    slot_starts_utc: list[Any],  # list[datetime]
    pv_avail: list[float],
    base_load_kwh: list[float],
    price_line: list[float],
    peak_threshold_p: float,
    initial_soc_kwh: float,
    soc_max_kwh: float,
    strict_savings: bool = False,  # kept for back-compat; ignored in PR C
    enabled: bool | None = None,
    margin: float | None = None,
    as_of_utc: Any = None,  # datetime — defaults to slot_starts_utc[0]
) -> PvSufficiencyGuardDiag:
    """Decide whether to apply the PV-sufficiency guard rail and which slots it
    targets. Pure function — no LP constraints added here. Callers fold the
    returned ``pre_peak_slot_indices`` into their MILP.

    Returns ``PvSufficiencyGuardDiag`` for both the auditing snapshot and the
    LP wiring. ``applied=True`` ⇒ caller must add ``chg[i] <= pv_use[i]`` for
    every ``i`` in ``pre_peak_slot_indices``.

    Mathematical rule
    -----------------
    Let ``T = today (UTC) per slot_starts_utc[0]``,
        ``Pv = Σ pv_avail[i] over i where slot_starts_utc[i].date() == T``,
        ``L  = Σ base_load_kwh[i] over the same i``,
        ``H  = max(0, soc_max_kwh - initial_soc_kwh)``,
        ``D  = H + L``.
    Then the guard fires iff ``strict_savings == True`` and ``enabled == True``
    and ``Pv × margin ≥ D``. When it fires, the targeted slots are every
    today-slot strictly before the first today-slot where
    ``price_line[i] >= peak_threshold_p``.

    Why "today-slots before first peak" rather than "all today-slots":
    - Slots IN the peak window are typically discharge slots; the LP wouldn't
      grid-charge there anyway.
    - Including peak slots would block any unusual cheap mid-peak dip from
      becoming a charge opportunity, which is an over-reach.
    - Leaving the after-peak (evening cheap) window unblocked lets the LP
      pre-load battery for the next day if Outgoing Agile is high overnight.
    """
    enabled_eff = bool(
        enabled if enabled is not None
        else getattr(config, "LP_PV_SUFFICIENCY_GUARD", True)
    )
    margin_eff = float(
        margin if margin is not None
        else getattr(config, "LP_PV_SUFFICIENCY_MARGIN", 1.0)
    )
    diag = PvSufficiencyGuardDiag(
        enabled=enabled_eff,
        strict_savings=False,  # PR C — strict_savings removed
        margin=margin_eff,
    )
    if not enabled_eff:
        diag.reason = "disabled"
        return diag
    # PR C — guard is always evaluated when enabled (previously only fired
    # under strict_savings mode). The economic argument is the same in any
    # mode: when forecast PV would already fill the battery, grid-charging
    # before the first peak slot is wasteful.

    n = len(slot_starts_utc)
    if n == 0:
        diag.reason = "empty_horizon"
        return diag

    # "Today" = the UTC date of the FIRST slot in the horizon. The LP solves
    # forward from "now", so slot 0's date is the right anchor; tomorrow's
    # slots are scoped out by the date filter below.
    today_utc = slot_starts_utc[0].astimezone(UTC).date()

    today_idx = [
        i for i in range(n)
        if slot_starts_utc[i].astimezone(UTC).date() == today_utc
    ]
    if not today_idx:
        diag.reason = "no_today_slots"
        return diag

    first_peak: int | None = None
    for i in today_idx:
        if price_line[i] >= peak_threshold_p:
            first_peak = i
            break
    diag.first_peak_slot_idx = first_peak

    pre_peak_today = [
        i for i in today_idx
        if first_peak is None or i < first_peak
    ]
    diag.pre_peak_slot_indices = pre_peak_today

    forecast_pv = sum(float(pv_avail[i]) for i in today_idx)
    expected_load = sum(float(base_load_kwh[i]) for i in today_idx)
    headroom = max(0.0, float(soc_max_kwh) - float(initial_soc_kwh))
    demand = headroom + expected_load
    diag.forecast_pv_today_kwh = forecast_pv
    diag.expected_load_today_kwh = expected_load
    diag.battery_headroom_kwh = headroom
    diag.demand_kwh = demand

    if not pre_peak_today:
        diag.applied = False
        diag.reason = "no_pre_peak_slots"
        return diag

    if forecast_pv * margin_eff >= demand:
        diag.applied = True
        diag.reason = "sufficient_pv"
    else:
        diag.applied = False
        diag.reason = "insufficient_pv"
    return diag
