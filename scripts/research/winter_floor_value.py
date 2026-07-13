#!/usr/bin/env python
"""Newsvendor evaluation of the pessimistic charge floor on SIMULATED WINTER days.

Question (#684 follow-up): the floor's summer premium is ~£7.89/yr of which ~£4.93/yr
is provably un-payable (the battery fills from free PV anyway). Owner: "£5/yr is
irrelevant — but what about WINTER?" We have no winter prod data (prod starts
2026-04), so we SIMULATE winter with REAL winter inputs.

WHAT IS REAL HERE
-----------------
  * PRICES  — real Octopus Agile import (E-1R-AGILE-24-10-01-<GSP>) and Outgoing
    Agile export (E-1R-AGILE-OUTGOING-19-05-13-<GSP>) half-hourly rates for
    Dec 2025 / Jan 2026, fetched from the public Octopus API through the repo's
    own client (``src.scheduler.agile._fetch_rates``). Cached to JSON.
  * WEATHER — real measured Dec 2025 / Jan 2026 weather for the site
    (WEATHER_LAT/LON) from Open-Meteo's ARCHIVE API: temperature_2m, cloud_cover,
    shortwave_radiation. PV is derived by the repo's own ``forecast_to_lp_inputs``
    (same calibration chain the LP uses in prod) — not hand-rolled.
  * LP / SCENARIOS / FLOOR — the production code paths, unmodified:
    ``solve_lp``, ``solve_scenarios_with_nominal``, ``_apply_pessimistic_charge_floor``.

WHAT IS ASSUMED (see ASSUMPTIONS block at the bottom of the report)
-------------------------------------------------------------------
  * BASE LOAD — ``db.residual_load_profile_v2()`` is used WHEN the DB actually holds
    day data. On this checkout it does not (day_counts == 0 → it returns a FLAT
    0.588 kWh/slot fallback = 1.18 kW average, which is whole-house power INCLUDING
    the heat pump and would double-count heating against the LP's own heat model).
    So we fall back to a documented synthetic residual:
        residual_kw[h] = p25(load_power_kw | hour=h) from the repo's real
                         ``pv_realtime_history`` (Mar–Apr 2026, ~0.38 kW — the
                         heat-pump-free floor of the house's own telemetry)
                       + an evening/morning activity uplift
                         (0.30 kW 07–09, 0.60 kW 17–22 local)
        → ~12.8 kWh/day residual, then × ``--winter-load-scale`` (default 1.0).
    Heating is modelled by the LP itself from outdoor temp, so the residual must NOT
    carry it. This is the single biggest assumption in the study — sensitivity is
    reported via --winter-load-scale.
  * INITIAL STATE — SoC 25 %, tank 40 °C at 00:00 UTC (a plausible post-evening
    winter start).
  * OUTCOME GRID — realised PV factor × realised load factor, with a prior
    (documented below) for the probability weights.

THE SETTLEMENT MODEL (the crux — read this)
-------------------------------------------
A committed plan does NOT fix the grid flows: it fixes the *commands* the hardware
gets. Reality (PV, load) then lands differently and the residual is settled at the
grid at that slot's real price. We emulate the Fox H1 the way prod actually drives it
(see reference_h1_selfuse_floor_ignored / fox group conventions):

For each slot i, the plan fixes THREE commands:
  1. ``grid_charge[i]``   = max(0, chg[i] − pv_surplus_planned[i])   (ForceCharge)
  2. ``batt_export[i]``   = max(0, exp[i] − pv_surplus_planned[i])   (ForceDischarge)
  3. ``e_hp[i]``          = e_dhw[i] + e_space[i]                    (Daikin schedule)
     where pv_surplus_planned[i] = max(0, pv_use[i] − base_load[i] − e_hp[i]).

Everything else is the hardware's SelfUse policy, executed against the REALISED
PV and load:
  a. PV → house load first.
  b. ForceCharge command: import from grid into the battery (capped by SoC headroom
     and the per-slot inverter energy budget).
  c. ForceDischarge command: battery → grid (capped by SoC above the reserve floor
     and the remaining inverter budget).
  d. Surplus PV → battery (headroom / budget), then → export.
  e. Residual load → battery discharge down to the reserve floor, then → grid import
     — UNLESS the slot is a HOLD slot.

HOLD slots: a slot where the LP planned NO battery discharge even though the planned
residual (base + heat − pv) was positive is a deliberate hold: prod expresses it as a
Fox **Backup** group (the H1 ignores a SelfUse min-SoC floor — see
reference_h1_selfuse_floor_ignored — so hold has to be Backup). In a hold slot the
battery does not serve load; the house imports. Modelling every slot as greedy SelfUse
(the naive settlement) drains the battery overnight in BOTH plans and structurally
hides the floor's value, so this distinction matters.

Battery energy accounting mirrors the LP exactly: ``soc += chg·√η`` /
``soc −= dis/√η`` with η = BATTERY_RT_EFFICIENCY; per-slot battery throughput
≤ MAX_INVERTER_KW × 0.5 h; SoC ∈ [reserve, capacity]; the heat-pump schedule is a
commitment and is NOT re-optimised under the realised world (that is exactly the
risk the floor insures against).

REPLAY FIDELITY: in the nominal world (pv×1.0, demand×1.0) the settlement should
reproduce the plan's own grid flows. It does so exactly on some days and within ~1 %
of the bill on others. The residual is NOT a bug in the settlement: the LP can plan a
*partial* discharge (e.g. cover 0.5 of a 1.2 kWh residual from the battery and import
the rest), which a SelfUse/Backup group physically cannot express — SelfUse covers the
whole residual, Backup covers none. The settlement is the honest hardware; the LP is
the optimistic one. Both plans are settled under the identical policy, so the A/B is
unbiased. The per-day replay residual is printed so you can see it.

Realised cost = Σ import_kwh·import_price − Σ export_kwh·export_price, over the full
48 h horizon, plus a TERMINAL SoC correction: the two plans can end with different
stored energy, so the plan holding more kWh at the end is credited at
``--terminal-value-p`` p/kWh (default: the 25th-percentile import price of the
horizon = the cost to refill it). Reported with sensitivity at 0p and at median price.

USAGE
    .venv/bin/python scripts/research/winter_floor_value.py            # full run
    .venv/bin/python scripts/research/winter_floor_value.py --days 3   # quick
No source file is modified; only the public Octopus + Open-Meteo APIs are called
(cached to data/winter_floor_cache.json).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import sys
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

# --- Pin the PROD electrical/LP config BEFORE importing src.config (env-read at
#     class definition time). The local sim .env carries some dev-only values. ---
_PROD_ENV = {
    "BATTERY_CAPACITY_KWH": "10.36",
    "MAX_INVERTER_KW": "5.0",
    "MIN_SOC_RESERVE_PERCENT": "10",       # prod floor since #339
    "BATTERY_RT_EFFICIENCY": "0.92",
    "FOX_EXPORT_MAX_PWR": "3680",
    "PV_CAPACITY_KWP": "4.5",
    "LP_PESS_CHARGE_FLOOR_ENABLED": "true",
    "LP_PESS_CHARGE_FLOOR_TOLERANCE_KWH": "0.2",
    "LP_PESS_CHARGE_FLOOR_HOURS": "24",
    "LP_PESS_CHARGE_FLOOR_SLACK_PENALTY_PENCE": "50.0",
    "LP_SCENARIO_PESSIMISTIC_PV_FACTOR": "0.85",
    "LP_SCENARIO_PESSIMISTIC_LOAD_FACTOR": "1.15",
    "LP_SCENARIO_PESSIMISTIC_TEMP_DELTA_C": "-1.5",
    "LP_SCENARIO_OPTIMISTIC_PV_FACTOR": "1.05",
    "LP_SCENARIO_OPTIMISTIC_LOAD_FACTOR": "0.90",
    "LP_SCENARIO_OPTIMISTIC_TEMP_DELTA_C": "1.0",
    "FORECAST_NIGHT_TEMP_BIAS_C": "0",
    "BULLETPROOF_TIMEZONE": "Europe/London",
    # The local sim .env is DAIKIN_CONTROL_MODE=passive (never touches hardware);
    # prod runs ACTIVE (Phase 1 flip). In passive mode the LP pins e_dhw/e_space to
    # the predicted autonomous draw, which collides with the dhw_policy pin → the
    # solve is Infeasible. Active is what the floor actually runs under in prod.
    "DAIKIN_CONTROL_MODE": "active",
}
for _k, _v in _PROD_ENV.items():
    os.environ[_k] = _v

from zoneinfo import ZoneInfo  # noqa: E402

from src import db  # noqa: E402
from src.config import config  # noqa: E402
from src.scheduler.agile import _fetch_rates  # noqa: E402
from src.scheduler.lp_optimizer import LpInitialState, LpPlan, solve_lp  # noqa: E402
from src.scheduler.optimizer import _apply_pessimistic_charge_floor  # noqa: E402
from src.scheduler.scenarios import solve_scenarios_with_nominal  # noqa: E402
from src.weather import (  # noqa: E402
    HourlyForecast,
    compute_heating_demand_factor,
    estimate_pv_kw,
    forecast_to_lp_inputs,
)

CACHE = REPO / "data" / "winter_floor_cache.json"
TZ = ZoneInfo("Europe/London")

# Winter days simulated (each = a 48 h horizon starting 00:00 UTC on that date).
# Chosen to span the winter regime: cold snaps, mild wet spells, and the
# Christmas/New-Year negative-price season.
WINTER_DAYS = [
    "2025-12-03", "2025-12-09", "2025-12-15", "2025-12-21",
    "2025-12-28", "2026-01-06", "2026-01-13", "2026-01-20",
    "2026-01-26", "2026-01-30",
]

# ---------------------------------------------------------------------------
# Outcome grid + prior
# ---------------------------------------------------------------------------
# PV realised/forecast daily ratio. The repo's own pv_error_log gives a SUMMER
# distribution (per CLAUDE.md: daily Σactual/Σforecast p25 = 0.883 over 27 d) —
# that is what the pessimistic ×0.85 was calibrated on. Winter PV is relatively
# MORE volatile (a low sun angle + thick cloud can wipe out 60 % of a day), so we
# use a deliberately wider prior and test its sensitivity.
PV_FACTORS = [1.15, 1.00, 0.85, 0.70, 0.55]
PV_WEIGHTS = [0.15, 0.30, 0.25, 0.20, 0.10]

# DEMAND factor. In winter the dominant forecast error is NOT PV (a December day
# yields 1–3 kWh total) — it is heat demand: a colder-than-forecast day makes the
# firmware run the compressor longer at the same LWT setpoints. So the demand factor
# multiplies BOTH the residual house load AND the plan's committed heat-pump draw.
# This is the realised-world analogue of what the pessimistic scenario proxies with
# LOAD_FACTOR 1.15 + TEMP_DELTA −1.5 °C. A 1.30 cold-snap tail is included with low
# weight.
LOAD_FACTORS = [0.90, 1.00, 1.15, 1.30]
LOAD_WEIGHTS = [0.20, 0.45, 0.25, 0.10]
# Alternative prior: the LP's demand forecast is well calibrated and the cold tail is
# rare. If the floor's value survives THIS prior too, the conclusion is robust.
LOAD_WEIGHTS_TIGHT = [0.25, 0.55, 0.15, 0.05]

PEAK_LOCAL_HOURS = (16, 20)  # 16:00–19:59 local: the expensive evening window


# ---------------------------------------------------------------------------
# Cached fetchers
# ---------------------------------------------------------------------------
def _load_cache() -> dict:
    if CACHE.exists():
        return json.loads(CACHE.read_text())
    return {}


def _save_cache(c: dict) -> None:
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(c))


def fetch_prices(cache: dict, gsp: str, start: datetime, end: datetime) -> tuple[dict, dict]:
    """Return (import, export) dicts: slot-start ISO → p/kWh inc VAT."""
    key = f"prices:{gsp}:{start.date()}:{end.date()}"
    if key not in cache:
        imp = _fetch_rates(f"E-1R-AGILE-24-10-01-{gsp}", start, end)
        exp = _fetch_rates(f"E-1R-AGILE-OUTGOING-19-05-13-{gsp}", start, end)
        cache[key] = {
            "import": {r["valid_from"]: r["value_inc_vat"] for r in imp},
            "export": {r["valid_from"]: r["value_inc_vat"] for r in exp},
        }
        _save_cache(cache)
    d = cache[key]
    return d["import"], d["export"]


def fetch_archive_weather(cache: dict, lat: float, lon: float,
                          start: datetime, end: datetime) -> list[HourlyForecast]:
    key = f"wx:{lat}:{lon}:{start.date()}:{end.date()}"
    if key not in cache:
        q = urllib.parse.urlencode({
            "latitude": lat, "longitude": lon,
            "start_date": start.date().isoformat(), "end_date": end.date().isoformat(),
            "hourly": "temperature_2m,cloud_cover,shortwave_radiation",
            "timezone": "UTC",
        })
        url = f"https://archive-api.open-meteo.com/v1/archive?{q}"
        with urllib.request.urlopen(url, timeout=30) as r:
            cache[key] = json.loads(r.read().decode())
        _save_cache(cache)
    h = cache[key]["hourly"]
    out: list[HourlyForecast] = []
    for i, t in enumerate(h["time"]):
        temp = float(h["temperature_2m"][i] or 0.0)
        rad = float(h["shortwave_radiation"][i] or 0.0)
        cloud = float(h["cloud_cover"][i] or 0.0)
        out.append(HourlyForecast(
            time_utc=datetime.fromisoformat(t).replace(tzinfo=UTC),
            temperature_c=temp,
            cloud_cover_pct=cloud,
            shortwave_radiation_wm2=rad,
            estimated_pv_kw=estimate_pv_kw(rad),
            heating_demand_factor=compute_heating_demand_factor(temp),
        ))
    return out


# ---------------------------------------------------------------------------
# Residual (non-heat-pump) load profile
# ---------------------------------------------------------------------------
_EVENING_UPLIFT_KW = {7: 0.30, 8: 0.30, 17: 0.60, 18: 0.60, 19: 0.60, 20: 0.60, 21: 0.60}


def build_residual_profile(scale: float) -> tuple[dict[int, float], str]:
    """Return (local-hour → kWh/slot residual, provenance string).

    Prefers the repo's own ``residual_load_profile_v2`` when the DB actually holds
    days. On a DB with no day data it returns a FLAT whole-house fallback (heat pump
    included) which would double-count heating — so we then synthesise the residual
    from real house telemetry: the per-hour p25 of ``pv_realtime_history.load_power_kw``
    (the heat-pump-free floor) plus a documented evening/morning activity uplift.
    """
    import sqlite3

    prof = db.residual_load_profile_v2()
    days = int((prof.get("day_counts") or {}).get("total", 0) or 0)
    if days > 0:
        out = {}
        for h in range(24):
            out[h] = db.lookup_residual_kwh(prof, 2, h, 0) * scale
        return out, f"residual_load_profile_v2 ({days} days in DB)"

    con = sqlite3.connect(str(REPO / "data" / "energy_state.db"))
    rows = con.execute(
        "SELECT captured_at, load_power_kw FROM pv_realtime_history "
        "WHERE load_power_kw IS NOT NULL"
    ).fetchall()
    by_hour: dict[int, list[float]] = {h: [] for h in range(24)}
    for ts, lw in rows:
        h = datetime.fromisoformat(ts).astimezone(TZ).hour
        by_hour[h].append(float(lw))
    out = {}
    for h in range(24):
        v = sorted(by_hour[h]) or [0.4]
        p25 = v[len(v) // 4]
        kw = p25 + _EVENING_UPLIFT_KW.get(h, 0.0)
        out[h] = kw * 0.5 * scale  # kW → kWh/slot
    daily = sum(out.values()) * 2
    return out, (
        f"SYNTHETIC: p25(load_power_kw) from pv_realtime_history ({len(rows)} samples) "
        f"+ evening uplift → {daily:.1f} kWh/day residual (×{scale})"
    )


# ---------------------------------------------------------------------------
# Plan → hardware commands
# ---------------------------------------------------------------------------
def plan_commands(plan: LpPlan, base_load: list[float]) -> list[dict]:
    """Extract the per-slot commands the hardware actually receives."""
    cmds = []
    n = len(plan.slot_starts_utc)
    for i in range(n):
        e_hp = float(plan.dhw_electric_kwh[i]) + float(plan.space_electric_kwh[i])
        pv_use = float(plan.pv_use_kwh[i])
        surplus = max(0.0, pv_use - base_load[i] - e_hp)
        dis = float(plan.battery_discharge_kwh[i])
        batt_export = max(0.0, float(plan.export_kwh[i]) - surplus)
        dis_to_load = max(0.0, dis - batt_export)
        planned_residual = base_load[i] + e_hp - pv_use
        # HOLD: LP left the battery alone despite a positive residual → Backup group.
        hold = planned_residual > 1e-6 and dis_to_load <= 1e-6
        cmds.append({
            "grid_charge": max(0.0, float(plan.battery_charge_kwh[i]) - surplus),
            "batt_export": batt_export,
            "e_hp": e_hp,
            "hold": hold,
        })
    return cmds


def settle(plan: LpPlan, base_load: list[float], pv_avail: list[float],
           imp_p: list[float], exp_p: list[float],
           soc0: float, pv_f: float, load_f: float) -> dict:
    """Execute the plan's commands against a realised world; return realised cost."""
    cap = float(config.BATTERY_CAPACITY_KWH)
    reserve = cap * float(config.MIN_SOC_RESERVE_PERCENT) / 100.0
    max_batt = float(config.MAX_INVERTER_KW) * 0.5
    sq = math.sqrt(max(0.01, min(1.0, float(config.BATTERY_RT_EFFICIENCY))))
    fuse = float(config.LP_GRID_IMPORT_MAX_KW) * 0.5
    exp_cap = float(config.FOX_EXPORT_MAX_PWR) / 2000.0

    cmds = plan_commands(plan, base_load)
    soc = soc0
    cost_p = 0.0
    tot_imp = tot_exp = 0.0
    empty_at_peak = 0
    unserved_import_at_peak_kwh = 0.0

    for i in range(len(cmds)):
        pv = pv_avail[i] * pv_f
        # demand factor hits residual load AND the committed heat-pump draw (a
        # colder-than-forecast day = longer compressor runs at the same setpoints)
        load = (base_load[i] + cmds[i]["e_hp"]) * load_f
        budget = max_batt  # battery AC throughput budget for this slot
        imp_kwh = exp_kwh = 0.0

        # (a) PV → load
        pv_to_load = min(pv, load)
        load -= pv_to_load
        pv -= pv_to_load

        # (b) ForceCharge from grid
        gc = min(cmds[i]["grid_charge"], budget, max(0.0, (cap - soc) / sq))
        if gc > 0:
            soc += gc * sq
            imp_kwh += gc
            budget -= gc

        # (c) ForceDischarge → grid
        fd = min(cmds[i]["batt_export"], budget, max(0.0, (soc - reserve) * sq))
        if fd > 0:
            soc -= fd / sq
            exp_kwh += fd
            budget -= fd

        # (d) PV surplus → battery, then export
        if pv > 0:
            c = min(pv, budget, max(0.0, (cap - soc) / sq))
            soc += c * sq
            budget -= c
            pv -= c
            exp_kwh += pv

        # (e) residual load → battery (unless HOLD: Backup group → import instead)
        if load > 0:
            if cmds[i]["hold"]:
                d = 0.0
            else:
                d = min(load, budget, max(0.0, (soc - reserve) * sq))
            soc -= d / sq
            budget -= d
            load -= d
            imp_kwh += load

        imp_kwh = min(imp_kwh, fuse)
        exp_kwh = min(exp_kwh, exp_cap)
        cost_p += imp_kwh * imp_p[i] - exp_kwh * exp_p[i]
        tot_imp += imp_kwh
        tot_exp += exp_kwh

        h = plan.slot_starts_utc[i].astimezone(TZ).hour
        if PEAK_LOCAL_HOURS[0] <= h < PEAK_LOCAL_HOURS[1]:
            if soc <= reserve + 1e-6 and imp_kwh > 1e-6:
                empty_at_peak += 1
                unserved_import_at_peak_kwh += imp_kwh

    return {
        "cost_p": cost_p,
        "soc_end": soc,
        "import_kwh": tot_imp,
        "export_kwh": tot_exp,
        "empty_at_peak_slots": empty_at_peak,
        "peak_import_at_floor_kwh": unserved_import_at_peak_kwh,
    }


# ---------------------------------------------------------------------------
# One simulated winter day
# ---------------------------------------------------------------------------
RESIDUAL: dict[int, float] = {}


def run_day(day: str, cache: dict, gsp: str, lat: float, lon: float,
            load_scale: float, soc0_pct: float, tank0: float) -> dict | None:
    start = datetime.fromisoformat(day).replace(tzinfo=UTC)
    end = start + timedelta(hours=48)
    starts = [start + timedelta(minutes=30 * i) for i in range(96)]

    imp_map, exp_map = fetch_prices(cache, gsp, start, end)
    def _p(m: dict, s: datetime) -> float | None:
        return m.get(s.isoformat().replace("+00:00", "Z")) or m.get(s.isoformat())
    imp_p = [_p(imp_map, s) for s in starts]
    exp_p = [_p(exp_map, s) for s in starts]
    if any(v is None for v in imp_p) or any(v is None for v in exp_p):
        miss_i = sum(1 for v in imp_p if v is None)
        miss_e = sum(1 for v in exp_p if v is None)
        print(f"  [skip {day}] price gaps: {miss_i} import / {miss_e} export slots missing")
        return None
    imp_p = [float(v) for v in imp_p]
    exp_p = [float(v) for v in exp_p]

    fc = fetch_archive_weather(cache, lat, lon, start, end + timedelta(hours=1))
    weather = forecast_to_lp_inputs(fc, starts, pv_scale=1.0)

    base_load = [RESIDUAL[s.astimezone(TZ).hour] for s in starts]
    # No learned p75 spread on this DB → pass None so the scenario LP uses the flat
    # LP_SCENARIO_*_LOAD_FACTOR knobs (0.90 / 1.15), which is the documented default.
    spread = None

    initial = LpInitialState(
        soc_kwh=float(config.BATTERY_CAPACITY_KWH) * soc0_pct / 100.0,
        tank_temp_c=tank0,
        indoor_temp_c=None,
    )
    kw = dict(
        slot_starts_utc=starts, price_pence=imp_p, base_load_kwh=base_load,
        weather=weather, initial=initial, tz=TZ, micro_climate_offset_c=0.0,
        micro_climate_offset_by_hour_c={}, export_price_pence=exp_p,
    )

    nominal = solve_lp(**kw)
    if not nominal.ok:
        print(f"  [skip {day}] nominal LP {nominal.status}")
        return None

    scen = dict(solve_scenarios_with_nominal(
        nominal=nominal, base_load_spread=spread, **kw,
    ))
    snap: dict = {}
    floored = _apply_pessimistic_charge_floor(
        nominal, scen, solve_kwargs=dict(kw), exogenous_snapshot=snap,
    )
    floor_meta = snap.get("pess_charge_floor", {})
    floor_bound = floored is not nominal

    pv_avail = list(weather.pv_kwh_per_slot)
    soc0 = initial.soc_kwh

    # terminal SoC valuation: cheap-quartile import price (cost to refill)
    tv_p25 = statistics.quantiles(imp_p, n=4)[0]
    tv_med = statistics.median(imp_p)

    worlds = {}
    for pv_f in PV_FACTORS:
        for lf in LOAD_FACTORS:
            a = settle(floored, base_load, pv_avail, imp_p, exp_p, soc0, pv_f, lf)
            b = settle(nominal, base_load, pv_avail, imp_p, exp_p, soc0, pv_f, lf)
            for tag, tv in (("p25", tv_p25), ("zero", 0.0), ("med", tv_med)):
                a[f"adj_{tag}"] = a["cost_p"] - a["soc_end"] * tv
                b[f"adj_{tag}"] = b["cost_p"] - b["soc_end"] * tv
            worlds[(pv_f, lf)] = {"floor": a, "noflo": b}

    # Replay fidelity: settled nominal-world cost vs the plan's own cash cost.
    plan_cash = sum(
        nominal.import_kwh[i] * imp_p[i] - nominal.export_kwh[i] * exp_p[i]
        for i in range(96)
    )
    replay_gap = worlds[(1.00, 1.00)]["noflo"]["cost_p"] - plan_cash
    # The floor's premium measured on the PLANS' OWN cash flows (no settlement in the
    # loop) — this isolates the true premium from settlement discretisation noise.
    floored_cash = sum(
        floored.import_kwh[i] * imp_p[i] - floored.export_kwh[i] * exp_p[i]
        for i in range(96)
    )
    cash_premium = floored_cash - plan_cash

    # "cannot pay off" class: battery ends the PV window full AND PV is still
    # being exported (i.e. the insured shortfall physically cannot happen because
    # free PV refills the battery anyway) — the summer failure mode.
    pv_day_kwh = sum(pv_avail[:48])
    surplus_export = sum(
        max(0.0, pv_avail[i] - base_load[i]) for i in range(48)
    )
    return {
        "day": day,
        "floor_bound": floor_bound,
        "binding_slots": floor_meta.get("binding_slots", 0),
        "insurance_cost_pence": floor_meta.get("insurance_cost_pence", 0.0),
        "nominal_obj_p": nominal.objective_pence,
        "floored_obj_p": floored.objective_pence,
        "plan_cash_p": plan_cash,
        "replay_gap_p": replay_gap,
        "cash_premium_p": cash_premium,
        "pv_day1_kwh": pv_day_kwh,
        "pv_surplus_day1_kwh": surplus_export,
        "mean_import_p": statistics.mean(imp_p),
        "mean_temp_c": statistics.mean(weather.temperature_outdoor_c),
        "worlds": worlds,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=len(WINTER_DAYS))
    ap.add_argument("--gsp", default="C", help="Octopus GSP letter (C = London)")
    ap.add_argument("--winter-load-scale", type=float, default=1.0)
    ap.add_argument("--soc0-pct", type=float, default=25.0)
    ap.add_argument("--tank0-c", type=float, default=40.0)
    args = ap.parse_args()

    lat = float(getattr(config, "WEATHER_LAT", None) or 51.49)
    lon = float(getattr(config, "WEATHER_LON", None) or -0.26)
    cache = _load_cache()

    global RESIDUAL
    RESIDUAL, prov = build_residual_profile(args.winter_load_scale)

    print("=" * 100)
    print("WINTER PESSIMISTIC-CHARGE-FLOOR NEWSVENDOR EVALUATION")
    print(f"site lat={lat} lon={lon} | GSP={args.gsp} | battery {config.BATTERY_CAPACITY_KWH} kWh "
          f"reserve {config.MIN_SOC_RESERVE_PERCENT}% | Daikin mode {config.DAIKIN_CONTROL_MODE}")
    print(f"residual load: {prov}")
    print("=" * 100)

    results = []
    for day in WINTER_DAYS[: args.days]:
        print(f"\n--- {day} ---")
        r = run_day(day, cache, args.gsp, lat, lon,
                    args.winter_load_scale, args.soc0_pct, args.tank0_c)
        if r:
            results.append(r)
            print(f"  floor bound={r['floor_bound']} slots={r['binding_slots']} "
                  f"insurance={r['insurance_cost_pence']}p | "
                  f"PV day1 {r['pv_day1_kwh']:.1f} kWh (surplus {r['pv_surplus_day1_kwh']:.1f}) | "
                  f"mean import {r['mean_import_p']:.1f}p | mean temp {r['mean_temp_c']:.1f}°C")
            print(f"  48h bill (nominal plan, nominal world) {r['plan_cash_p']/100:.2f} £ | "
                  f"replay gap {r['replay_gap_p']:+.1f}p "
                  f"({abs(r['replay_gap_p'])/max(1.0, r['plan_cash_p'])*100:.1f}% — LP partial-discharge "
                  f"intent the hardware can't express)")

    if not results:
        print("\nNo days solved — aborting.")
        return

    # ---- per-day × outcome-world table (p25 terminal valuation) ----
    print("\n" + "=" * 100)
    print("Δ = cost(FLOOR) − cost(NOFLOOR), pence per 48 h horizon. NEGATIVE = floor SAVED money.")
    print("(terminal SoC valued at the horizon's 25th-pct import price)")
    print("=" * 100)
    hdr = "day        load  " + "".join(f"  pv×{f:<5.2f}" for f in PV_FACTORS)
    print(hdr)
    for r in results:
        for lf in LOAD_FACTORS:
            row = f"{r['day']}  ×{lf:.2f} "
            for pv_f in PV_FACTORS:
                w = r["worlds"][(pv_f, lf)]
                d = w["floor"]["adj_p25"] - w["noflo"]["adj_p25"]
                row += f"  {d:+8.1f}"
            print(row)

    # ---- empty-at-peak counts ----
    print("\nEmpty-at-peak slots (SoC at reserve AND importing, 16:00–20:00 local) — floor / noflo")
    for r in results:
        cells = []
        for pv_f in PV_FACTORS:
            w = r["worlds"][(pv_f, 1.30)]
            cells.append(f"{w['floor']['empty_at_peak_slots']}/{w['noflo']['empty_at_peak_slots']}")
        print(f"  {r['day']} (demand ×1.30): " + "  ".join(f"pv×{f:.2f}: {c}" for f, c in zip(PV_FACTORS, cells)))

    # ---- probability-weighted expected value ----
    print("\n" + "=" * 100)
    print("PROBABILITY-WEIGHTED EXPECTED VALUE")
    print("=" * 100)
    for tag, label, lw_vec in (
        ("p25", "base prior, terminal SoC @ p25 import price", LOAD_WEIGHTS),
        ("zero", "base prior, terminal SoC @ 0p (worst case for floor)", LOAD_WEIGHTS),
        ("p25", "TIGHT demand prior (cold tail rare)", LOAD_WEIGHTS_TIGHT),
    ):
        ev_days = []
        for r in results:
            ev = 0.0
            for pv_f, pw in zip(PV_FACTORS, PV_WEIGHTS):
                for lf, lw in zip(LOAD_FACTORS, lw_vec):
                    w = r["worlds"][(pv_f, lf)]
                    ev += pw * lw * (w["floor"][f"adj_{tag}"] - w["noflo"][f"adj_{tag}"])
            ev_days.append(ev)
        # each horizon is 48 h but the plan is re-solved daily → per-DAY value is
        # the first-24 h share; we conservatively halve the 48 h delta.
        per_day = statistics.mean(ev_days) / 2.0
        print(f"  {label:<45}: E[Δ] = {per_day:+.2f} p/day  →  "
              f"{per_day * 120 / 100:+.2f} £/120-day winter  |  {per_day * 365 / 100:+.2f} £/yr if year-round")
        print(f"      per-day spread: " + " ".join(f"{d/2:+.1f}" for d in ev_days))

    # ---- nominal-world premium (what the floor costs when the forecast is right) ----
    prem = [r["worlds"][(1.0, 1.0)]["floor"]["adj_p25"]
            - r["worlds"][(1.0, 1.0)]["noflo"]["adj_p25"] for r in results]
    print(f"\n  PREMIUM (what the floor costs when the forecast is RIGHT), three measures:")
    print(f"    a) settled nominal world (pv×1.0, demand×1.0): "
          f"{statistics.mean(prem)/2:+.2f} p/day")
    print(f"    b) plans' own cash flows, floored − nominal : "
          f"{statistics.mean([r['cash_premium_p'] for r in results])/2:+.2f} p/day  "
          f"<-- the trustworthy one (no settlement discretisation in the loop)")
    print(f"    c) LP objective delta (insurance_cost_pence): "
          f"{statistics.mean([r['insurance_cost_pence'] for r in results])/2:+.2f} p/day "
          f"(includes non-cash penalties: inverter stress, TV smoothing, cycle)")
    print(f"    Mean |replay gap| = "
          f"{statistics.mean([abs(r['replay_gap_p']) for r in results])/2:.2f} p/day — measure (a) is "
          f"AT OR BELOW this noise floor, so do not read (a) as a real saving.")

    # ---- payoff only in the bad worlds ----
    bad = []
    for r in results:
        for pv_f in PV_FACTORS:
            for lf in (1.15, 1.30):
                w = r["worlds"][(pv_f, lf)]
                bad.append(w["floor"]["adj_p25"] - w["noflo"]["adj_p25"])
    print(f"  Payoff in BAD worlds (demand ≥ ×1.15, any PV): mean {statistics.mean(bad)/2:+.2f} p/day, "
          f"best {min(bad)/2:+.1f} p/day, worst {max(bad)/2:+.1f} p/day")
    lowpv = []
    for r in results:
        for pv_f in (0.70, 0.55):
            for lf in (0.90, 1.00):
                w = r["worlds"][(pv_f, lf)]
                lowpv.append(w["floor"]["adj_p25"] - w["noflo"]["adj_p25"])
    print(f"  Payoff in LOW-PV worlds only (pv≤0.70, demand ≤ ×1.0): "
          f"mean {statistics.mean(lowpv)/2:+.2f} p/day "
          f"— winter PV is 1–3 kWh/day, so PV error is NOT the lever the floor insures")

    # ---- 'cannot pay off' class ----
    n_pv_fill = sum(1 for r in results if r["pv_surplus_day1_kwh"] > 3.0)
    print(f"\n  'Cannot pay off' class (day-1 PV surplus > 3 kWh, i.e. battery refills free "
          f"and PV is still exported): {n_pv_fill}/{len(results)} winter days "
          f"(summer: 8/12).")


if __name__ == "__main__":
    main()
