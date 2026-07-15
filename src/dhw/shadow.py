"""The economic shadow and enable gate for LP-owned DHW (#714).

The LP-owned regime does not get to prove itself in production by being switched on.
It proves itself in the SHADOW: on every committed solve — while the tank is still owned
by the fixed schedule — the SAME inputs are solved twice more and compared:

* the **baseline arm** pins the tank to a SIMULATION of what the fixed schedule
  actually does under the measured physics (:mod:`src.dhw.baseline`). Not the
  dhw_policy forecast: that forecast plans ~2.4× the household's real DHW energy, and
  a comparison against it credits the LP-owned arm with phantom savings the incumbent
  never actually spends. Against the simulation, both arms serve the same declared
  draw with the same physics, and the delta is pure allocation value — WHEN each
  regime bought the heat.
* the **LP-owned arm** lets the LP time the tank (``force_dhw_lp_owned``).

Nothing either arm plans is dispatched. After a run of days where the LP-owned arm is
both cheaper and comfort-clean, a one-shot notification SUGGESTS enabling it — a human
flips the flag, never the code.

Three numbers per shadow, and all three gate:

* **Δ grid cost** on identical inputs — same solver, same prices, same weather.
* **Comfort deficit** — °C below floor at any shower boundary, read off the LP plan's
  own tank trajectory (not the objective's slack, which also carries the harmless
  over-temperature coast-down). One cold shower disqualifies the day.
* **Heat parity** — the two arms' DHW energy totals. The LP may legitimately spend
  MORE (pre-heat + hold loss is a strategy the owner explicitly blessed) or less
  (skipping the fixed schedule's needless hold), but a wild divergence means the arms
  stopped being comparable and the day must not count.
"""
from __future__ import annotations

import logging
import threading
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# Fail-CLOSED throttle backstop. The DB is the source of truth for "how many shadows
# ran today", but if its read or the insert silently fails, counting on it would let
# the shadow run on EVERY optimizer solve for the rest of the day — doubling the LP
# load in prod. This in-memory counter increments on every attempt regardless of DB
# health, so a persistent DB failure throttles the shadow instead of unleashing it.
# (Resets on process restart, which is fine: it is a backstop, not the ledger.)
_ATTEMPTS_LOCK = threading.Lock()
_ATTEMPTS_BY_DAY: dict[str, int] = {}


def grid_cost_pence(plan, price_pence: list[float],
                    export_price_pence: list[float] | None) -> float:
    """Grid cost of a plan on given prices: imports bought minus exports sold."""
    n = len(plan.import_kwh)
    exp_price = export_price_pence or [0.0] * n
    cost = 0.0
    for i in range(n):
        cost += plan.import_kwh[i] * price_pence[i]
        if i < len(plan.export_kwh):
            cost -= plan.export_kwh[i] * (exp_price[i] if i < len(exp_price) else 0.0)
    return cost


def comfort_deficit_c(plan, tz: ZoneInfo, preset: str) -> float:
    """°C below floor the LP-owned tank sits at any shower boundary — the honest
    comfort signal. Zero means every shower was delivered."""
    from . import comfort as _comfort

    if not getattr(plan, "dhw_lp_owned", False) or not plan.slot_starts_utc:
        return 0.0
    floors = _comfort.comfort_floors_for_slots(list(plan.slot_starts_utc), tz, preset=preset)
    worst = 0.0
    for i, floor in enumerate(floors):
        if floor is not None and i < len(plan.tank_temp_c):
            worst = max(worst, floor - plan.tank_temp_c[i])
    return worst


def record_shadow(*, solve_kwargs: dict, price_pence: list[float],
                  export_price_pence: list[float] | None) -> dict | None:
    """Solve both arms of the comparison on ``solve_kwargs`` and log the result.

    Two extra MILP solves per shadow (baseline + LP-owned), throttled to a few per
    local day. Best effort: any failure returns None and never disturbs the committed
    solve. Nothing here is dispatched.
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
    cap = int(getattr(config, "DHW_LP_OWNED_SHADOW_MAX_PER_DAY", 4))

    # Throttle, fail-closed: the in-memory attempt counter trips even when the DB
    # read/insert is failing (see the module constant's comment).
    with _ATTEMPTS_LOCK:
        attempts = _ATTEMPTS_BY_DAY.get(day, 0)
        if attempts >= cap:
            return None
        _ATTEMPTS_BY_DAY[day] = attempts + 1
        # Keep the dict from growing forever.
        for stale in [d for d in _ATTEMPTS_BY_DAY if d != day]:
            del _ATTEMPTS_BY_DAY[stale]
    try:
        if len([r for r in db.get_dhw_shadow_rows(day) if r["day"] == day]) >= cap:
            return None
    except Exception:  # noqa: BLE001 — memory counter above already bounds us
        pass

    # --- Baseline arm: the fixed schedule, simulated honestly -------------------
    try:
        from . import comfort as _comfort
        from .baseline import legionella_budget_by_slot, simulate_fixed_schedule
        from .params import resolve_tank_params

        starts = list(solve_kwargs["slot_starts_utc"])
        weather = solve_kwargs["weather"]
        preset = (config.OPTIMIZATION_PRESET or "normal").strip().lower()
        p = resolve_tank_params()
        draw = _comfort.declared_draw_kwh_for_slots(
            starts, tz, preset=preset,
            guest_count=int(getattr(config, "DHW_GUEST_COUNT", 2)),
        )
        leg = legionella_budget_by_slot(
            starts, budget_kwh=float(getattr(config, "DHW_LEGIONELLA_BUDGET_KWH", 3.5)),
        ) if bool(getattr(config, "DHW_LEGIONELLA_STANDOFF_ENABLED", True)) else None
        override = simulate_fixed_schedule(
            starts, tz,
            tank0_c=float(solve_kwargs["initial"].tank_temp_c),
            p=p,
            t_out_by_slot=list(weather.temperature_outdoor_c),
            draw_kwh_by_slot=draw,
            legionella_kwh_by_slot=leg,
        )
        baseline_plan = solve_lp(**{**solve_kwargs, "pinned_dhw_override": override})
    except Exception:  # noqa: BLE001 — the shadow must never break the committed solve
        logger.debug("dhw shadow: baseline solve failed", exc_info=True)
        return None
    if not baseline_plan.ok:
        return None

    # --- LP-owned arm -------------------------------------------------------------
    try:
        shadow_plan = solve_lp(**{**solve_kwargs, "force_dhw_lp_owned": True})
    except Exception:  # noqa: BLE001
        logger.debug("dhw shadow: lp-owned solve failed", exc_info=True)
        return None
    if not shadow_plan.ok:
        return None

    cost_fixed = grid_cost_pence(baseline_plan, price_pence, export_price_pence)
    cost_lp = grid_cost_pence(shadow_plan, price_pence, export_price_pence)
    deficit = comfort_deficit_c(shadow_plan, tz, preset)
    e_fixed = float(sum(baseline_plan.dhw_electric_kwh))
    e_lp = float(sum(shadow_plan.dhw_electric_kwh))

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
        "cost_pinned_p": round(cost_fixed, 3),
        "cost_lp_owned_p": round(cost_lp, 3),
        "delta_p": round(cost_lp - cost_fixed, 3),
        "comfort_deficit_c": round(deficit, 3),
        "e_dhw_fixed_kwh": round(e_fixed, 3),
        "e_dhw_lp_kwh": round(e_lp, 3),
        "n_tank_rows": n_rows,
    }
    try:
        db.insert_dhw_shadow(row)
    except Exception:  # noqa: BLE001
        logger.debug("dhw shadow: insert failed", exc_info=True)
    logger.info("dhw shadow: Δ%.1fp comfort=%.1f°C heat fixed/lp %.2f/%.2f kWh rows=%s",
                row["delta_p"], row["comfort_deficit_c"], e_fixed, e_lp, n_rows)
    return row


def evaluate_gate() -> dict:
    """Is the LP-owned regime ready to suggest enabling? Reads the shadow log.

    The bar, all of which must hold over the trailing window:
      * at least ``MIN_DAYS`` distinct days shadowed;
      * the per-day MEDIAN Δ is a saving of at least ``MIN_SAVING_PENCE`` a day;
      * ZERO days with any comfort deficit — comfort is not for sale;
      * heat parity on every day (the arms stayed comparable);
      * EVERY solve within the Daikin row quota (strict: one over-cap plan blocks —
        deliberately harsher than a p90, because rows are writes and writes are quota).
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
    day_deltas = []
    rows_within_quota = True   # every solve's plan compresses to ≤ max_rows
    energy_parity_ok = True    # arms stayed comparable (loose band — see docstring)
    for _d, rs in by_day.items():
        ds = sorted(x["delta_p"] for x in rs)
        day_deltas.append(ds[len(ds) // 2])
        if any((x["n_tank_rows"] or 0) > max_rows for x in rs):
            rows_within_quota = False
        for x in rs:
            ef = x.get("e_dhw_fixed_kwh")
            el = x.get("e_dhw_lp_kwh")
            if ef and el is not None and ef > 0.2 and not (0.4 <= el / ef <= 2.5):
                energy_parity_ok = False
    day_deltas.sort()
    median_delta = day_deltas[len(day_deltas) // 2]
    median_saving = -median_delta  # a saving is a negative delta

    ready = (
        n_days >= min_days
        and not breach_days
        and median_saving >= min_saving
        and rows_within_quota
        and energy_parity_ok
    )
    return {
        "ready": ready,
        "days": n_days,
        "median_saving_pence": round(median_saving, 2),
        "comfort_breach_days": len(breach_days),
        "rows_within_quota": rows_within_quota,
        "energy_parity_ok": energy_parity_ok,
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
        f"{gate['median_saving_pence']:.1f}p/day saved vs the simulated fixed "
        f"schedule, zero comfort breaches, heat parity held. Set "
        f"DHW_LP_OWNED_ENABLED=true to hand the tank to the LP (dhw_policy stays "
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
