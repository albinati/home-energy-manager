"""The economic shadow and enable gate for LP-owned DHW (#714).

The LP-owned regime does not get to prove itself in production by being switched on.
It proves itself in the SHADOW: on every committed solve, while the tank is still owned
by the fixed schedule, we re-solve the SAME inputs with the LP owning the tank and record
what it would have cost and whether it would have kept everyone in hot water. Nothing it
plans is dispatched. After a run of days where the shadow is both cheaper and
comfort-clean, a one-shot notification SUGGESTS enabling it — a human flips the flag,
never the code.

Two numbers per shadow, and both gate:

* **Δ grid cost** on identical inputs. Same solver, same prices, same weather — only the
  DHW regime differs, so the difference is exactly what the regime is worth that solve.
* **Comfort deficit** — how many °C below its floor the LP-owned tank would sit at any
  shower boundary. Read straight off the plan's own trajectory, not off the objective's
  slack (which also carries the harmless over-temperature coast-down). A cheaper plan
  that skimped on a shower is not a saving, and the gate treats a single breach as
  disqualifying.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


def grid_cost_pence(plan, price_pence: list[float],
                    export_price_pence: list[float] | None) -> float:
    """Grid cost of a plan on given prices: imports bought minus exports sold. The
    comparable figure between two plans on the same inputs — the DHW regime only moves
    import (when it heats) and the battery around it; PV export is the same either way,
    but include it so a plan that frees headroom to export is credited honestly."""
    n = len(plan.import_kwh)
    exp_price = export_price_pence or [0.0] * n
    cost = 0.0
    for i in range(n):
        cost += plan.import_kwh[i] * price_pence[i]
        if i < len(plan.export_kwh):
            cost -= plan.export_kwh[i] * (exp_price[i] if i < len(exp_price) else 0.0)
    return cost


def comfort_deficit_c(plan, tz: ZoneInfo, preset: str) -> float:
    """°C below floor the LP-owned tank sits at any shower boundary — the honest comfort
    signal (see module docstring). Zero means every shower was delivered."""
    from . import comfort as _comfort

    if not getattr(plan, "dhw_lp_owned", False) or not plan.slot_starts_utc:
        return 0.0
    floors = _comfort.comfort_floors_for_slots(list(plan.slot_starts_utc), tz, preset=preset)
    worst = 0.0
    for i, floor in enumerate(floors):
        if floor is not None and i < len(plan.tank_temp_c):
            worst = max(worst, floor - plan.tank_temp_c[i])
    return worst


def record_shadow(committed_plan, *, solve_kwargs: dict, price_pence: list[float],
                  export_price_pence: list[float] | None) -> dict | None:
    """Re-solve ``solve_kwargs`` with the LP owning the tank and log the comparison.

    ``committed_plan`` is the plan that actually ran (fixed-schedule / pinned). Best
    effort: any failure returns None and never disturbs the committed solve. Throttled
    to a few per local day so the shadow never becomes the cost centre.
    """
    from .. import db
    from ..config import config
    from ..scheduler.lp_optimizer import solve_lp

    if not bool(getattr(config, "DHW_LP_OWNED_SHADOW_ENABLED", True)):
        return None
    if config.DAIKIN_CONTROL_MODE == "passive":
        return None  # the regime is force-off in passive; nothing to shadow
    if bool(getattr(config, "DHW_LP_OWNED_ENABLED", False)):
        return None  # already committed to LP-owned — no shadow needed

    tz = ZoneInfo(getattr(config, "BULLETPROOF_TIMEZONE", "Europe/London"))
    now = datetime.now(UTC)
    day = now.astimezone(tz).date().isoformat()

    # Throttle: a handful of solves a day is plenty to characterise the regime.
    try:
        todays = [r for r in db.get_dhw_shadow_rows(day) if r["day"] == day]
        if len(todays) >= int(getattr(config, "DHW_LP_OWNED_SHADOW_MAX_PER_DAY", 4)):
            return None
    except Exception:  # noqa: BLE001 — a read failure must not block the shadow
        pass

    try:
        shadow_plan = solve_lp(**{**solve_kwargs, "force_dhw_lp_owned": True})
    except Exception:  # noqa: BLE001 — the shadow must never break the committed solve
        logger.debug("dhw shadow: solve failed", exc_info=True)
        return None
    if not shadow_plan.ok:
        return None

    cost_pinned = grid_cost_pence(committed_plan, price_pence, export_price_pence)
    cost_lp = grid_cost_pence(shadow_plan, price_pence, export_price_pence)
    preset = (config.OPTIMIZATION_PRESET or "normal").strip().lower()
    deficit = comfort_deficit_c(shadow_plan, tz, preset)

    n_rows = None
    try:
        from . import dispatch as _dispatch

        n_rows = len(_dispatch.tank_rows_from_plan(
            list(shadow_plan.slot_starts_utc), list(shadow_plan.tank_temp_c),
            list(shadow_plan.dhw_electric_kwh), list(price_pence)))
    except Exception:  # noqa: BLE001
        pass

    row = {
        "run_at_utc": now.isoformat(),
        "day": day,
        "cost_pinned_p": round(cost_pinned, 3),
        "cost_lp_owned_p": round(cost_lp, 3),
        "delta_p": round(cost_lp - cost_pinned, 3),
        "comfort_deficit_c": round(deficit, 3),
        "n_tank_rows": n_rows,
    }
    try:
        db.insert_dhw_shadow(row)
    except Exception:  # noqa: BLE001
        logger.debug("dhw shadow: insert failed", exc_info=True)
    logger.info("dhw shadow: Δ%.1fp comfort_deficit=%.1f°C rows=%s",
                row["delta_p"], row["comfort_deficit_c"], n_rows)
    return row


def evaluate_gate() -> dict:
    """Is the LP-owned regime ready to suggest enabling? Reads the shadow log.

    The bar, all of which must hold over the trailing window:
      * at least ``MIN_DAYS`` distinct days shadowed;
      * the per-day MEDIAN Δ is a saving of at least ``MIN_SAVING_PENCE`` a day;
      * ZERO days with any comfort deficit — comfort is not for sale;
      * the plan stays within the Daikin quota (p90 tank rows ≤ cap).
    """
    from .. import db
    from ..config import config

    min_days = int(getattr(config, "DHW_LP_OWNED_GATE_MIN_DAYS", 14))
    min_saving = float(getattr(config, "DHW_LP_OWNED_GATE_MIN_SAVING_PENCE", 3.0))
    max_rows = int(getattr(config, "DHW_LP_OWNED_GATE_MAX_ROWS", 6))
    tz = ZoneInfo(getattr(config, "BULLETPROOF_TIMEZONE", "Europe/London"))
    since = (datetime.now(UTC).astimezone(tz).date() - timedelta(days=min_days + 7)).isoformat()

    rows = db.get_dhw_shadow_rows(since)
    if not rows:
        return {"ready": False, "reason": "no shadow data", "days": 0}

    by_day: dict[str, list[dict]] = {}
    for r in rows:
        by_day.setdefault(r["day"], []).append(r)

    n_days = len(by_day)
    breach_days = [d for d, rs in by_day.items() if any(x["comfort_deficit_c"] > 0.5 for x in rs)]
    # Per-day median delta (one representative per day = its median row).
    day_deltas = []
    row_p90_ok = True
    for _d, rs in by_day.items():
        ds = sorted(x["delta_p"] for x in rs)
        day_deltas.append(ds[len(ds) // 2])
        if any((x["n_tank_rows"] or 0) > max_rows for x in rs):
            row_p90_ok = False
    day_deltas.sort()
    median_delta = day_deltas[len(day_deltas) // 2]
    median_saving = -median_delta  # a saving is a negative delta

    ready = (
        n_days >= min_days
        and not breach_days
        and median_saving >= min_saving
        and row_p90_ok
    )
    return {
        "ready": ready,
        "days": n_days,
        "median_saving_pence": round(median_saving, 2),
        "comfort_breach_days": len(breach_days),
        "rows_within_quota": row_p90_ok,
        "min_days": min_days,
        "min_saving_pence": min_saving,
    }


def maybe_suggest_enable() -> None:
    """Nightly: if the gate is met and we have not already said so, send ONE
    notification. Never enables anything — a human flips the flag."""
    from .. import db
    from ..config import config

    if bool(getattr(config, "DHW_LP_OWNED_ENABLED", False)):
        return  # already on
    gate = evaluate_gate()
    if not gate.get("ready"):
        return

    key = "dhw_lp_owned_enable_suggested_at"
    try:
        if db.get_runtime_setting(key):
            return  # already suggested; re-arm by clearing the setting
    except Exception:  # noqa: BLE001
        pass

    msg = (
        f"DHW LP-owned is ready to enable: {gate['days']} shadow days, median "
        f"{gate['median_saving_pence']:.1f}p/day saved, zero comfort breaches. "
        f"Set DHW_LP_OWNED_ENABLED=true to hand the tank to the LP (dhw_policy stays "
        f"as the kill switch)."
    )
    try:
        from ..notifier import notify

        notify(msg)
    except Exception:  # noqa: BLE001
        logger.info("dhw shadow gate met (notify failed): %s", msg)
    try:
        db.log_action(device="daikin", action="dhw_lp_owned_enable_suggested",
                      params=gate, result="suggested", trigger="shadow_gate")
        db.set_runtime_setting(key, datetime.now(UTC).isoformat())
    except Exception:  # noqa: BLE001
        pass
