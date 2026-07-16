#!/usr/bin/env python
"""#728 — trajectory vs peak_entry pessimistic charge floor, on frozen prod inputs.

METHOD (built on the #684 retraction lessons — READ scripts/research/
winter_floor_value.py's header before extending this):

* NO settlement layer. The one metric #684's adversarial review endorsed as
  robust is the plans' OWN cash flows (import cost − export revenue at the
  plan's prices) — settlement replays distort in treatment-correlated ways.
* Both arms are floored variants solved from the SAME frozen snapshot inputs
  (same nominal, same pessimistic trajectory, same initial SoC) — uniform
  exposure by construction.
* One run per local day (the first solve of the day ≈ the nightly plan push,
  the canonical commitment).

Per day, per arm (trajectory | peak_entry):
  insurance_p   objective delta floored − nominal (48 h LP objective — NOT
                cash, includes cycle/TV/terminal penalties; labelled honestly)
  cash24_p      Σ(import×price) − Σ(export×export_price) over the first 24 h
  cash_premium  cash24(arm) − cash24(nominal)  ← the decision metric
  entry_ok      floored SoC ≥ pess SoC − tol at every expensive/severe-peak
                entry boundary (protection parity — must be True for BOTH)
  chg_exp_kwh   same-local-day overlap min(grid-charge kWh, positive-price
                export kWh) in the plan — the crossed-flow signature

Usage:
  DB_PATH=/path/to/prod_copy.db .venv/bin/python scripts/research/floor_scope_backtest.py --days 21
"""
from __future__ import annotations

import argparse
import statistics
import sys
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.config import config  # noqa: E402


@contextmanager
def _capture_solve_kwargs():
    """Intercept lp_replay's solve_lp call to capture the reconstructed inputs
    while still returning the real solve (the nominal plan)."""
    from src.scheduler import lp_replay
    from src.scheduler.lp_optimizer import solve_lp as real_solve

    captured: dict = {}

    def spy(**kw):
        captured.update(kw)
        return real_solve(**kw)

    orig = lp_replay.solve_lp
    lp_replay.solve_lp = spy
    try:
        yield captured
    finally:
        lp_replay.solve_lp = orig


def _cash24_p(plan, export_prices) -> float:
    n24 = min(48, len(plan.slot_starts_utc))
    imp = getattr(plan, "import_kwh", None) or []
    exp = getattr(plan, "export_kwh", None) or []
    prices = getattr(plan, "price_pence", None) or []
    cash = 0.0
    for i in range(n24):
        p_imp = float(prices[i]) if i < len(prices) else 0.0
        p_exp = float(export_prices[i]) if export_prices and i < len(export_prices) else 0.0
        cash += (float(imp[i]) if i < len(imp) else 0.0) * p_imp
        cash -= (float(exp[i]) if i < len(exp) else 0.0) * p_exp
    return cash


def _crossed_flow_kwh(plan) -> float:
    """Same-local-day overlap of grid charge and positive-price export (kWh)."""
    tz = ZoneInfo(config.BULLETPROOF_TIMEZONE)
    imp = getattr(plan, "import_kwh", None) or []
    exp = getattr(plan, "export_kwh", None) or []
    chg = getattr(plan, "battery_charge_kwh", None) or []
    prices = getattr(plan, "price_pence", None) or []
    by_day: dict[date, list[float]] = {}
    for i, st in enumerate(plan.slot_starts_utc):
        d = st.astimezone(tz).date()
        grid_chg = min(
            float(imp[i]) if i < len(imp) else 0.0,
            float(chg[i]) if i < len(chg) else 0.0,
        )
        pos_exp = (
            float(exp[i])
            if i < len(exp) and i < len(prices) and float(prices[i]) >= 0
            else 0.0
        )
        acc = by_day.setdefault(d, [0.0, 0.0])
        acc[0] += grid_chg
        acc[1] += pos_exp
    return sum(min(a, b) for a, b in by_day.values())


def _entry_protection_ok(plan, pess_plan, entry_slots, tol) -> bool:
    # The floor only covers the first LP_PESS_CHARGE_FLOOR_HOURS (the far half
    # is replanned before it executes) — entries beyond it are out of contract
    # for BOTH scopes and must not count as breaches.
    max_slots = int(float(config.LP_PESS_CHARGE_FLOOR_HOURS) * 2)
    for j in entry_slots:
        if j < 1 or j > max_slots or j >= len(plan.soc_kwh):
            continue
        if float(plan.soc_kwh[j]) + 1e-6 < float(pess_plan.soc_kwh[j]) - tol:
            return False
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=21)
    ap.add_argument("--end", type=str, default=None, help="last local date (default: yesterday)")
    args = ap.parse_args()

    from src.scheduler import lp_replay
    from src.scheduler.optimizer import (
        _apply_pessimistic_charge_floor,
        _peak_entry_floor_indices,
    )
    from src.scheduler.scenarios import solve_scenarios_with_nominal

    tz = ZoneInfo(config.BULLETPROOF_TIMEZONE)
    end = date.fromisoformat(args.end) if args.end else (
        datetime.now(tz).date() - timedelta(days=0)
    )
    tol = float(config.LP_PESS_CHARGE_FLOOR_TOLERANCE_KWH)

    def _pick_runs(d: date) -> list[tuple[str, int]]:
        """(label, run_id): the nightly push AND the run nearest 10:30 UTC —
        the 2026-07-16 incident regime is a mid-morning replan from low SoC,
        invisible to a push-only backtest."""
        runs = lp_replay.list_run_ids_for_date(d.isoformat())
        if not runs:
            return []
        out = [("push", runs[0][0])]
        target = datetime(d.year, d.month, d.day, 10, 30, tzinfo=UTC)
        best, best_gap = None, None
        for rid, at in runs:
            try:
                t = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
            except ValueError:
                continue
            gap = abs((t - target).total_seconds())
            if best_gap is None or gap < best_gap:
                best, best_gap = rid, gap
        if best is not None and best != runs[0][0] and best_gap < 4 * 3600:
            out.append(("mid", best))
        return out

    rows = []
    day_runs: list[tuple[date, str, int]] = []
    for k in range(args.days):
        d = end - timedelta(days=args.days - 1 - k)
        picked = _pick_runs(d)
        if not picked:
            print(f"{d}  —  no run")
            continue
        for label, rid in picked:
            day_runs.append((d, label, rid))

    for d, label, run_id in day_runs:
        with _capture_solve_kwargs() as kw:
            res = lp_replay.replay_run(run_id)
        if not res.ok or res._replayed_plan is None:
            print(f"{d}  —  replay failed: {res.error}")
            continue
        nominal = res._replayed_plan
        # scenario solves on the same frozen inputs
        scen_kw = dict(
            nominal=nominal,
            slot_starts_utc=kw["slot_starts_utc"],
            price_pence=kw["price_pence"],
            base_load_kwh=kw["base_load_kwh"],
            weather=kw["weather"],
            initial=kw["initial"],
            tz=kw["tz"],
            micro_climate_offset_c=kw.get("micro_climate_offset_c", 0.0),
            export_price_pence=kw.get("export_price_pence"),
            micro_climate_offset_by_hour_c=kw.get("micro_climate_offset_by_hour_c"),
        )
        scenarios = dict(solve_scenarios_with_nominal(**scen_kw))
        pessr = scenarios.get("pessimistic")
        pess_plan = getattr(pessr, "plan", None)
        if pess_plan is None or not pess_plan.ok:
            print(f"{d}  —  pessimistic solve failed")
            continue
        solve_kwargs = {
            k2: v for k2, v in kw.items()
            if k2 in (
                "slot_starts_utc", "price_pence", "base_load_kwh", "weather",
                "initial", "tz", "micro_climate_offset_c",
                "micro_climate_offset_by_hour_c", "export_price_pence",
                "force_dhw_lp_owned", "pinned_dhw_override",
            )
        }
        entries = _peak_entry_floor_indices(
            nominal.slot_starts_utc, list(nominal.price_pence or [])
        )
        cash_nom = _cash24_p(nominal, kw.get("export_price_pence"))
        day_row = {
            "date": d.isoformat(), "label": label,
            "entries": len(entries), "cash_nom": cash_nom,
        }
        for scope in ("trajectory", "peak_entry"):
            old = config.LP_PESS_CHARGE_FLOOR_SCOPE
            config.LP_PESS_CHARGE_FLOOR_SCOPE = scope  # plain attr
            try:
                snap: dict = {}
                floored = _apply_pessimistic_charge_floor(
                    nominal, scenarios,
                    solve_kwargs=solve_kwargs, exogenous_snapshot=snap,
                )
            finally:
                config.LP_PESS_CHARGE_FLOOR_SCOPE = old
            fc = snap.get("pess_charge_floor") or {}
            cash = _cash24_p(floored, kw.get("export_price_pence"))
            day_row[scope] = {
                "insurance_p": fc.get("insurance_cost_pence", 0.0),
                "binding": fc.get("binding_slots", 0),
                "cash_premium_p": cash - cash_nom,
                "entry_ok": _entry_protection_ok(floored, pess_plan, entries, tol),
                "crossed_kwh": _crossed_flow_kwh(floored),
                "resolved": floored is not nominal,
            }
        day_row["crossed_nom"] = _crossed_flow_kwh(nominal)
        rows.append(day_row)
        t = day_row["trajectory"]
        p = day_row["peak_entry"]
        print(
            f"{d} {label:4s}  entries={day_row['entries']}  "
            f"traj: ins {t['insurance_p']:7.1f}p cashΔ {t['cash_premium_p']:7.1f}p "
            f"xflow {t['crossed_kwh']:.2f}  |  "
            f"peak: ins {p['insurance_p']:7.1f}p cashΔ {p['cash_premium_p']:7.1f}p "
            f"xflow {p['crossed_kwh']:.2f}  "
            f"prot {'OK' if t['entry_ok'] and p['entry_ok'] else 'BREACH'}"
        )

    if not rows:
        print("no days evaluated")
        return 1

    def agg(scope, key):
        vals = [r[scope][key] for r in rows if scope in r]
        return vals

    print("\n=== SUMMARY (n=%d days) — plan cash flows, NO settlement ===" % len(rows))
    for scope in ("trajectory", "peak_entry"):
        ins = agg(scope, "insurance_p")
        cash = agg(scope, "cash_premium_p")
        xf = agg(scope, "crossed_kwh")
        prot = agg(scope, "entry_ok")
        print(
            f"{scope:11s}: cash premium median {statistics.median(cash):6.1f}p "
            f"mean {statistics.mean(cash):6.1f}p max {max(cash):6.1f}p | "
            f"objective-ins median {statistics.median(ins):6.1f}p | "
            f"crossed-flow mean {statistics.mean(xf):.2f} kWh | "
            f"protection {sum(prot)}/{len(prot)} days OK"
        )
    xnom = [r["crossed_nom"] for r in rows]
    print(f"nominal    : crossed-flow mean {statistics.mean(xnom):.2f} kWh (no floor)")
    print(
        "\ncash premium = floored plan's 24h import−export cash minus nominal's, "
        "at the frozen snapshot prices.\nPositive = the floor cost money that day "
        "in the plan's own cash terms. Objective-ins includes non-cash penalties."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
