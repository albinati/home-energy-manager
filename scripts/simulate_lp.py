#!/usr/bin/env python3
"""Simulate the V8 PuLP planner using **real** data: SQLite Agile rates, Open-Meteo, Fox SoC, optional Daikin.

Does **not** upload Fox Scheduler V3, does **not** write Daikin actions, does **not** call save_daily_target.
Prints **Fox Scheduler V3 group preview** and **Daikin action_schedule preview** (same as operational dispatch would use).

Usage (from project root):

    PYTHONPATH=. .venv/bin/python scripts/simulate_lp.py
    PYTHONPATH=. .venv/bin/python scripts/simulate_lp.py --json
    PYTHONPATH=. .venv/bin/python scripts/simulate_lp.py --no-daikin
    PYTHONPATH=. .venv/bin/python scripts/simulate_lp.py --no-dispatch

Prerequisites:
    - `.env` with ``OCTOPUS_TARIFF_CODE``, ``DB_PATH`` (default ``energy_state.db``), ``WEATHER_LAT`` / ``WEATHER_LON``
    - Tomorrow’s half-hour rates already in SQLite (daily Octopus fetch, or run your ingest)
    - For live SoC: Fox ESS configured (``get_cached_realtime``)
    - For live tank/room: Daikin OAuth tokens (``DAIKIN_TOKEN_FILE``) unless ``--no-daikin``
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def _try_daikin():
    try:
        from src.daikin.client import DaikinClient

        c = DaikinClient()
        c.get_devices()
        return c
    except Exception as e:
        print(f"(Daikin skipped: {e})", file=sys.stderr)
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Simulate PuLP plan with live DB + weather + telemetry")
    parser.add_argument("--json", action="store_true", help="Print JSON summary + per-slot arrays")
    parser.add_argument("--no-daikin", action="store_true", help="Do not call Onecta (tank/room from defaults/logs only)")
    parser.add_argument(
        "--max-rows",
        type=int,
        default=0,
        help="Limit printed table rows (0 = all half-hours)",
    )
    parser.add_argument(
        "--no-dispatch",
        action="store_true",
        help="Skip Fox/Daikin schedule preview (LP table / JSON slots only)",
    )
    args = parser.parse_args()

    from src.scheduler.lp_simulation import run_lp_simulation

    daikin = None if args.no_daikin else _try_daikin()
    r = run_lp_simulation(daikin=daikin)

    if r.plan is None:
        print(f"Error: {r.error}", file=sys.stderr)
        return 1

    p = r.plan
    init = r.initial
    n_slots = len(p.slot_starts_utc)
    has_solution = bool(p.ok and p.import_kwh and len(p.import_kwh) == n_slots)

    if args.json:
        out: dict = {
            "ok": r.ok,
            "error": r.error,
            "plan_date": r.plan_date,
            "solver_status": p.status,
            "objective_pence": p.objective_pence,
            "slot_count": r.slot_count,
            "mu_load_kwh_per_slot": r.mu_load_kwh,
            "mean_agile_pence": r.actual_mean_agile_pence,
            "forecast_solar_kwh_horizon": r.forecast_solar_kwh_horizon,
            "initial_soc_kwh": init.soc_kwh if init else None,
            "initial_tank_c": init.tank_temp_c if init else None,
            "initial_indoor_c": init.indoor_temp_c if init else None,
            "cheap_threshold_pence": p.cheap_threshold_pence,
            "peak_threshold_pence": p.peak_threshold_pence,
            "slots": [],
        }
        if not has_solution:
            print(json.dumps(out, indent=2))
            return 2 if not r.ok else 0
        if not args.no_dispatch and r.forecast is not None:
            from src.scheduler.lp_dispatch import build_fox_groups_from_lp, daikin_dispatch_preview

            out["fox_scheduler_groups"] = [g.to_api_dict() for g in build_fox_groups_from_lp(p)]
            pairs = daikin_dispatch_preview(p, r.forecast)
            out["daikin_dispatch"] = [
                {"restore": rest, "action": act} for rest, act in pairs
            ]
        n = n_slots
        for i in range(n):
            out["slots"].append(
                {
                    "start_utc": p.slot_starts_utc[i].isoformat(),
                    "price_pence": p.price_pence[i],
                    "import_kwh": p.import_kwh[i],
                    "export_kwh": p.export_kwh[i],
                    "bat_charge_kwh": p.battery_charge_kwh[i],
                    "bat_discharge_kwh": p.battery_discharge_kwh[i],
                    "pv_use_kwh": p.pv_use_kwh[i],
                    "pv_curtail_kwh": p.pv_curtail_kwh[i],
                    "dhw_kwh": p.dhw_electric_kwh[i],
                    "space_kwh": p.space_electric_kwh[i],
                    "soc_kwh": p.soc_kwh[i + 1],
                    "tank_c": p.tank_temp_c[i + 1],
                    "indoor_c": p.indoor_temp_c[i + 1],
                    "temp_out_c": p.temp_outdoor_c[i] if i < len(p.temp_outdoor_c) else None,
                }
            )
        print(json.dumps(out, indent=2))
        return 0 if r.ok else 2

    print("=== LP simulation (no hardware writes) ===")
    print(f"Plan date (tomorrow local): {r.plan_date}")
    print(
        f"Solver: {p.status}  |  Objective ≈ {p.objective_pence:.1f} pence  |  slots={r.slot_count}"
    )
    if not has_solution:
        print(f"No per-slot solution to display ({r.error or p.status}).", file=sys.stderr)
        if init:
            print(
                f"Initial state: SoC={init.soc_kwh:.2f} kWh, tank={init.tank_temp_c:.1f}°C, "
                f"indoor={init.indoor_temp_c:.1f}°C"
            )
        return 2 if not r.ok else 0
    print(f"Mean Agile (horizon): {r.actual_mean_agile_pence:.2f} p/kWh  |  PV≈{r.forecast_solar_kwh_horizon:.2f} kWh (PV scale: {getattr(r, 'pv_scale_factor', 1.0):.2f})")
    print(f"Rolling load μ: {r.mu_load_kwh:.3f} kWh/half-hour ({r.mu_load_kwh*48:.1f} kWh/day)  |  cheap/peak thr: {p.cheap_threshold_pence:.1f} / {p.peak_threshold_pence:.1f} p")
    if init:
        print(
            f"Initial state: SoC={init.soc_kwh:.2f} kWh, tank={init.tank_temp_c:.1f}°C, indoor={init.indoor_temp_c:.1f}°C"
        )
    if r.error:
        print(f"Warning: {r.error}", file=sys.stderr)

    print()
    hdr = (
        f"{'UTC':<17} {'p/kWh':>6} {'Imp':>6} {'Exp':>6} {'Chg':>6} {'Dis':>6} "
        f"{'PVu':>6} {'DHW':>6} {'Space':>6} {'SoC':>6} {'Tank':>6} {'In':>5} {'Out':>5}"
    )
    print(hdr)
    print("-" * len(hdr))
    n = n_slots
    lim = n if args.max_rows <= 0 else min(n, args.max_rows)
    for i in range(lim):
        st = p.slot_starts_utc[i]
        print(
            f"{st.strftime('%m-%d %H:%M'):<17} "
            f"{p.price_pence[i]:6.1f} "
            f"{p.import_kwh[i]:6.2f} {p.export_kwh[i]:6.2f} "
            f"{p.battery_charge_kwh[i]:6.2f} {p.battery_discharge_kwh[i]:6.2f} "
            f"{p.pv_use_kwh[i]:6.2f} {p.dhw_electric_kwh[i]:6.2f} {p.space_electric_kwh[i]:6.2f} "
            f"{p.soc_kwh[i+1]:6.2f} {p.tank_temp_c[i+1]:6.1f} {p.indoor_temp_c[i+1]:5.1f} "
            f"{p.temp_outdoor_c[i]:5.1f}"
        )
    if lim < n:
        print(f"... ({n - lim} more rows; omit --max-rows to print all)")
    print()

    if has_solution and not args.no_dispatch and r.forecast is not None:
        from src.config import config as app_config
        from src.scheduler.lp_dispatch import build_fox_groups_from_lp, daikin_dispatch_preview
        from src.scheduler.optimizer import TZ

        loc_tz = TZ()
        print("--- Fox ESS Scheduler V3 (preview — not uploaded) ---")
        print(
            f"OPERATION_MODE={app_config.OPERATION_MODE} — upload only when operational + not read-only."
        )
        groups = build_fox_groups_from_lp(p)
        if not groups:
            print("  (no groups — check LP slot kinds / merge)")
        for idx, g in enumerate(groups, 1):
            extra = g.to_api_dict().get("extraParam") or {}
            print(
                f"  {idx}. {g.start_hour:02d}:{g.start_minute:02d} → {g.end_hour:02d}:{g.end_minute:02d}  "
                f"{g.work_mode}  minSoc={g.min_soc_on_grid}  extra={extra}"
            )

        print()
        print("--- Daikin action_schedule (preview — not written to SQLite) ---")
        pairs = daikin_dispatch_preview(p, r.forecast)
        if not pairs:
            print("  (no Daikin windows — standard slots only, or away-like preset skipped preheat)")
        for rest, act in pairs:
            st = datetime.fromisoformat(act["start_time"].replace("Z", "+00:00")).astimezone(loc_tz)
            en = datetime.fromisoformat(act["end_time"].replace("Z", "+00:00")).astimezone(loc_tz)
            kind = act.get("lp_slot_kind", "")
            print(
                f"  {act['action_type']:<10} [{kind}]  {st.strftime('%Y-%m-%d %H:%M')} → {en.strftime('%H:%M')}  "
                f"params={act.get('params', {})}"
            )
        print()

    return 0 if r.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
