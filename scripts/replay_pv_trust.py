#!/usr/bin/env python
"""14-day PV-trust guard rail replay (incident 2026-05-15).

Re-solves the LP for each of the last N local days (one solve per day,
anchored on the first run_id of that day) under four variants:

  - baseline   — neither knob (matches pre-fix behaviour)
  - A          — PV-sufficiency guard ON, trust bias OFF
  - B          — PV-sufficiency guard OFF, trust bias ON (P75 of skill log)
  - C          — both ON (the proposed default)

For every variant the script reports £ cost-at-actual (import × Agile_p −
export × Outgoing_p) plus the £ delta vs baseline. Negative delta means the
variant would have saved money on that day.

Read-only: never touches Fox / Daikin / network, never writes to the DB.

Usage:
    DB_PATH=/srv/hem/data/energy_state.db \
        python scripts/replay_pv_trust.py --days 14

Optional flags:
    --days N             Number of trailing days to include (default 14)
    --percentile P       Trust-bias percentile (default 0.75; pass 0.5 to disable bias change)
    --margin M           PV-sufficiency margin (default 1.0)
    --as-of YYYY-MM-DD   Anchor date for the trailing window (default: today UTC)
    --json               Emit JSON to stdout instead of the markdown table

Designed to be runnable inside the prod container:
    docker exec hem python /app/scripts/replay_pv_trust.py --days 14
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date as _date, datetime, timedelta
from typing import Any, Iterator
from zoneinfo import ZoneInfo


def _install_readonly_db() -> None:
    """Monkey-patch ``src.db.get_connection`` to open the DB in SQLite URI
    read-only mode. This lets the replay run safely against a prod DB mounted
    read-only inside a container; SQLite otherwise tries to create journal
    files and fails on a read-only filesystem.
    """
    from src import db as _db
    from src.config import config as _cfg
    db_path = os.path.abspath(_cfg.DB_PATH)
    uri = f"file:{db_path}?mode=ro&immutable=1"

    def _ro_connection(*_args: Any, **_kwargs: Any) -> sqlite3.Connection:
        conn = sqlite3.connect(uri, uri=True, timeout=15.0)
        conn.row_factory = sqlite3.Row
        return conn

    _db.get_connection = _ro_connection  # type: ignore[assignment]


@dataclass
class VariantResult:
    """Per-variant solve outcome for one day."""

    label: str
    ok: bool
    error: str = ""
    cost_p: float = 0.0
    grid_import_kwh: float = 0.0
    grid_export_kwh: float = 0.0
    soc_terminal_kwh: float = 0.0
    bias_factor: float = 1.0
    guard_applied: bool = False
    guard_reason: str = ""
    forecast_pv_today_kwh: float = 0.0
    expected_load_today_kwh: float = 0.0
    battery_headroom_kwh: float = 0.0
    demand_kwh: float = 0.0
    n_pre_peak_slots: int = 0
    initial_soc_kwh: float = 0.0


@dataclass
class DayResult:
    """Per-day comparison across the four variants."""

    date_local: str = ""
    run_id: int = 0
    error: str = ""
    variants: dict[str, VariantResult] = field(default_factory=dict)

    def baseline_cost_p(self) -> float:
        if "baseline" not in self.variants or not self.variants["baseline"].ok:
            return 0.0
        return self.variants["baseline"].cost_p

    def delta_p(self, variant_label: str) -> float:
        if variant_label not in self.variants or not self.variants[variant_label].ok:
            return 0.0
        return self.variants[variant_label].cost_p - self.baseline_cost_p()


@contextmanager
def _patched(**kwargs: Any) -> Iterator[None]:
    """Temporarily set attributes on ``src.config.config``. Restores on exit."""
    from src.config import config
    prior: dict[str, Any] = {}
    try:
        for k, v in kwargs.items():
            prior[k] = getattr(config, k, None)
            setattr(config, k, v)
        yield
    finally:
        for k, v in prior.items():
            setattr(config, k, v)


def _solve_variant(
    *,
    run_id: int,
    variant_label: str,
    guard_enabled: bool,
    bias_enabled: bool,
    percentile: float,
    margin: float,
    as_of_date_utc: _date,
) -> VariantResult:
    """Replay one LP solve under the given variant. Pure function, no writes."""
    # Late imports — config reads DB_PATH at import time.
    from src import db
    from src.scheduler.lp_optimizer import LpInitialState, solve_lp
    from src.scheduler.lp_replay import (
        _build_export_prices,
        _parse_iso,
        _reconstruct_weather,
    )
    from src.scheduler.pv_trust import compute_pv_trust_bias
    from src.weather import WeatherLpSeries
    from src.config import config as _cfg

    inputs = db.get_lp_inputs(run_id)
    if not inputs:
        return VariantResult(label=variant_label, ok=False, error="no lp_inputs_snapshot")
    slots = db.get_lp_solution_slots(run_id)
    if not slots:
        return VariantResult(label=variant_label, ok=False, error="no lp_solution_snapshot")

    slot_starts_utc: list[datetime] = []
    price_pence: list[float] = []
    for s in slots:
        try:
            slot_starts_utc.append(_parse_iso(str(s["slot_time_utc"])))
        except (KeyError, ValueError, TypeError) as e:
            return VariantResult(label=variant_label, ok=False, error=f"bad slot_time_utc: {e}")
        price_pence.append(float(s.get("price_p") or 0.0))

    try:
        base_load_kwh = [float(x) for x in json.loads(inputs.get("base_load_json") or "[]")]
    except (TypeError, ValueError, json.JSONDecodeError) as e:
        return VariantResult(label=variant_label, ok=False, error=f"bad base_load_json: {e}")
    if len(base_load_kwh) != len(slot_starts_utc):
        return VariantResult(
            label=variant_label, ok=False,
            error=f"base_load length {len(base_load_kwh)} != slot count {len(slot_starts_utc)}",
        )

    initial = LpInitialState(
        soc_kwh=float(inputs.get("soc_initial_kwh") or 0.0),
        tank_temp_c=float(inputs.get("tank_initial_c") or 45.0),
    )

    weather, fidelity = _reconstruct_weather(
        str(inputs.get("run_at_utc") or ""),
        slot_starts_utc,
        forecast_fetch_at_utc=str(inputs.get("forecast_fetch_at_utc") or ""),
    )
    if fidelity == "missing":
        return VariantResult(label=variant_label, ok=False, error="weather snapshot missing")

    # Option B: apply P-th percentile bias to the PV vector. We compute the
    # bias against ``as_of_date_utc - 1`` so each day's replay only sees the
    # skill log that existed when its LP solve happened (causality preserved).
    bias_factor = 1.0
    if bias_enabled:
        bias = compute_pv_trust_bias(
            as_of_date_utc=as_of_date_utc,
            percentile=percentile,
        )
        bias_factor = bias.factor
        if bias_factor != 1.0:
            weather = WeatherLpSeries(
                slot_starts_utc=weather.slot_starts_utc,
                temperature_outdoor_c=weather.temperature_outdoor_c,
                shortwave_radiation_wm2=weather.shortwave_radiation_wm2,
                cloud_cover_pct=weather.cloud_cover_pct,
                pv_kwh_per_slot=[v * bias_factor for v in weather.pv_kwh_per_slot],
                cop_space=weather.cop_space,
                cop_dhw=weather.cop_dhw,
            )

    export_price_pence = _build_export_prices(slot_starts_utc)
    tz = ZoneInfo(_cfg.BULLETPROOF_TIMEZONE)
    mco_h: dict[int, float] = {}

    # Option A: flip the guard via config + force strict_savings so the rail
    # fires regardless of the prod runtime setting. Variant 'baseline' keeps
    # guard off so the comparison is honest.
    with _patched(
        LP_PV_SUFFICIENCY_GUARD=guard_enabled,
        LP_PV_SUFFICIENCY_MARGIN=margin,
        ENERGY_STRATEGY_MODE="strict_savings",
    ):
        plan = solve_lp(
            slot_starts_utc=slot_starts_utc,
            price_pence=price_pence,
            base_load_kwh=base_load_kwh,
            weather=weather,
            initial=initial,
            tz=tz,
            micro_climate_offset_c=0.0,
            micro_climate_offset_by_hour_c=mco_h,
            export_price_pence=export_price_pence,
        )

    if not plan.ok:
        return VariantResult(
            label=variant_label, ok=False,
            error=f"solver status: {plan.status}",
            bias_factor=bias_factor,
        )

    # Cost-at-actual = sum(imp × Agile_import) - sum(exp × Agile_outgoing)
    # using the same per-slot prices that drove the original solve (Agile is
    # day-ahead published; "actual" == "snapshot" by construction).
    cost = 0.0
    for i in range(len(plan.import_kwh)):
        imp_kwh = float(plan.import_kwh[i])
        exp_kwh = float(plan.export_kwh[i])
        ip = float(price_pence[i])
        ep = float(export_price_pence[i]) if export_price_pence else float(_cfg.EXPORT_RATE_PENCE)
        cost += imp_kwh * ip - exp_kwh * ep
    g = plan.pv_sufficiency_guard
    return VariantResult(
        label=variant_label,
        ok=True,
        cost_p=cost,
        grid_import_kwh=sum(plan.import_kwh),
        grid_export_kwh=sum(plan.export_kwh),
        soc_terminal_kwh=plan.soc_kwh[-1] if plan.soc_kwh else 0.0,
        bias_factor=bias_factor,
        guard_applied=(g.applied if g else False),
        guard_reason=(g.reason if g else ""),
        forecast_pv_today_kwh=(g.forecast_pv_today_kwh if g else 0.0),
        expected_load_today_kwh=(g.expected_load_today_kwh if g else 0.0),
        battery_headroom_kwh=(g.battery_headroom_kwh if g else 0.0),
        demand_kwh=(g.demand_kwh if g else 0.0),
        n_pre_peak_slots=(len(g.pre_peak_slot_indices) if g else 0),
        initial_soc_kwh=float(initial.soc_kwh),
    )


def replay_day(
    date_local: str,
    *,
    percentile: float = 0.75,
    margin: float = 1.0,
) -> DayResult:
    """Run all four variants for the morning-LP solve of ``date_local``
    (YYYY-MM-DD, UTC). Picks the EARLIEST run in the UTC day so the
    LP horizon covers a full day of PV — late-evening runs (BST → previous UTC
    day) would land slot[0] at 22:00 UTC with no PV in scope, masking the
    rail's behaviour."""
    from src.scheduler.lp_replay import list_run_ids_for_date, _parse_iso
    from datetime import datetime as _dt

    res = DayResult(date_local=date_local)
    # Pull every run for the local day, then pick the first one whose slot[0]
    # actually starts inside the UTC day of interest. That excludes the
    # late-evening BST runs (slot[0] = 22:00 UTC of prior date).
    try:
        target_utc = _date.fromisoformat(date_local)
    except ValueError:
        res.error = f"invalid date: {date_local}"
        return res
    # Search a wide local window: local day ± 1 catches all candidates.
    candidates: list[tuple[int, str]] = []
    for d in (
        (target_utc - timedelta(days=1)).isoformat(),
        target_utc.isoformat(),
        (target_utc + timedelta(days=1)).isoformat(),
    ):
        candidates.extend(list_run_ids_for_date(d))
    # Filter to runs whose run_at is between [target 00:00 UTC, target 12:00 UTC]
    # — the morning solves we care about for this audit.
    target_start = _dt.combine(target_utc, _dt.min.time()).replace(tzinfo=UTC)
    target_noon = target_start + timedelta(hours=12)
    morning_runs = [
        (rid, ts) for (rid, ts) in candidates
        if target_start <= _parse_iso(ts) < target_noon
    ]
    morning_runs.sort(key=lambda x: x[1])
    if not morning_runs:
        res.error = "no morning LP run with snapshot for this UTC date"
        return res
    run_id = morning_runs[0][0]
    res.run_id = run_id

    # The skill log used for Option B should reflect what was knowable when
    # this run solved — i.e. the previous days' data. We pass ``date_local``
    # as ``as_of_date_utc``; ``compute_pv_trust_bias`` excludes the as-of day
    # itself, so we read [date_local - lookback .. date_local).
    try:
        as_of = _date.fromisoformat(date_local)
    except ValueError:
        res.error = f"invalid date_local: {date_local}"
        return res

    variants = [
        ("baseline", False, False),
        ("A_only",   True,  False),
        ("B_only",   False, True),
        ("C_both",   True,  True),
    ]
    for label, guard, bias in variants:
        res.variants[label] = _solve_variant(
            run_id=run_id,
            variant_label=label,
            guard_enabled=guard,
            bias_enabled=bias,
            percentile=percentile,
            margin=margin,
            as_of_date_utc=as_of,
        )
    return res


def _fmt_p(p: float) -> str:
    return f"{p/100:+8.3f} £"


def _fmt_p_abs(p: float) -> str:
    return f"{p/100:8.3f} £"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", maxsplit=1)[0])
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--percentile", type=float, default=0.75)
    ap.add_argument("--margin", type=float, default=1.0)
    ap.add_argument("--as-of", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    # Open the DB read-only so we can safely run inside a container that
    # bind-mounted /srv/hem/data:/app/data as :ro. Done BEFORE the first
    # lp_replay import to make sure every code path sees the patched
    # connection helper.
    _install_readonly_db()

    if args.as_of:
        try:
            end = _date.fromisoformat(args.as_of)
        except ValueError:
            print(f"bad --as-of: {args.as_of}", file=sys.stderr)
            return 2
    else:
        end = datetime.now(UTC).date()
    start = end - timedelta(days=args.days)
    dates = [(end - timedelta(days=k)).isoformat() for k in range(args.days, 0, -1)]

    rows: list[DayResult] = []
    for d in dates:
        rows.append(replay_day(d, percentile=args.percentile, margin=args.margin))

    if args.json:
        out = {
            "window": {"start": start.isoformat(), "end": end.isoformat(), "days": args.days},
            "config": {"percentile": args.percentile, "margin": args.margin},
            "rows": [
                {
                    "date": r.date_local,
                    "run_id": r.run_id,
                    "error": r.error,
                    "variants": {
                        v.label: {
                            "ok": v.ok, "error": v.error,
                            "cost_p": v.cost_p,
                            "delta_baseline_p": r.delta_p(v.label),
                            "import_kwh": v.grid_import_kwh,
                            "export_kwh": v.grid_export_kwh,
                            "bias_factor": v.bias_factor,
                            "guard_applied": v.guard_applied,
                            "guard_reason": v.guard_reason,
                            "forecast_pv_today_kwh": v.forecast_pv_today_kwh,
                            "expected_load_today_kwh": v.expected_load_today_kwh,
                            "battery_headroom_kwh": v.battery_headroom_kwh,
                            "demand_kwh": v.demand_kwh,
                            "n_pre_peak_slots": v.n_pre_peak_slots,
                            "initial_soc_kwh": v.initial_soc_kwh,
                        }
                        for v in r.variants.values()
                    },
                }
                for r in rows
            ],
        }
        print(json.dumps(out, indent=2))
        return 0

    print()
    print(f"PV-trust replay over {args.days} days ({start} .. {end})")
    print(f"  Variant config: percentile=P{int(args.percentile*100)}, margin={args.margin}")
    print()
    header = (
        f"  {'Date':<12} {'run_id':>6} "
        f"{'baseline':>10} {'A_only':>10} {'B_only':>10} {'C_both':>10}   "
        f"{'ΔA':>9} {'ΔB':>9} {'ΔC':>9}  "
        f"{'A.app':>5} {'bias':>5} {'note':<14}"
    )
    print(header)
    print("  " + "-" * (len(header) - 2))

    sum_baseline = 0.0
    sum_a = 0.0
    sum_b = 0.0
    sum_c = 0.0
    for r in rows:
        if r.error:
            print(f"  {r.date_local:<12} {'-':>6} {'-':>10} {'-':>10} {'-':>10} {'-':>10}   "
                  f"{'-':>9} {'-':>9} {'-':>9}  {'-':>5} {'-':>5} {r.error:<14}")
            continue
        b = r.variants.get("baseline")
        a = r.variants.get("A_only")
        bb = r.variants.get("B_only")
        c = r.variants.get("C_both")
        if not (b and a and bb and c and b.ok and a.ok and bb.ok and c.ok):
            note = next((v.error for v in r.variants.values() if v and not v.ok), "fail")
            print(f"  {r.date_local:<12} {r.run_id:>6} {'-':>10} {'-':>10} {'-':>10} {'-':>10}   "
                  f"{'-':>9} {'-':>9} {'-':>9}  {'-':>5} {'-':>5} {note[:14]:<14}")
            continue
        sum_baseline += b.cost_p
        sum_a += a.cost_p
        sum_b += bb.cost_p
        sum_c += c.cost_p
        print(
            f"  {r.date_local:<12} {r.run_id:>6} "
            f"{_fmt_p_abs(b.cost_p):>10} {_fmt_p_abs(a.cost_p):>10} "
            f"{_fmt_p_abs(bb.cost_p):>10} {_fmt_p_abs(c.cost_p):>10}   "
            f"{_fmt_p(r.delta_p('A_only')):>9} {_fmt_p(r.delta_p('B_only')):>9} "
            f"{_fmt_p(r.delta_p('C_both')):>9}  "
            f"{'Y' if a.guard_applied else 'n':>5} {c.bias_factor:>5.2f} "
            f"{a.guard_reason[:14]:<14}"
        )

    print("  " + "-" * (len(header) - 2))
    print(
        f"  {'TOTAL':<12} {'':>6} "
        f"{_fmt_p_abs(sum_baseline):>10} {_fmt_p_abs(sum_a):>10} "
        f"{_fmt_p_abs(sum_b):>10} {_fmt_p_abs(sum_c):>10}   "
        f"{_fmt_p(sum_a - sum_baseline):>9} {_fmt_p(sum_b - sum_baseline):>9} "
        f"{_fmt_p(sum_c - sum_baseline):>9}"
    )
    print()
    print(
        f"  Annualised (× 365 / {args.days}): "
        f"A {_fmt_p((sum_a - sum_baseline) * 365.0 / args.days)}, "
        f"B {_fmt_p((sum_b - sum_baseline) * 365.0 / args.days)}, "
        f"C {_fmt_p((sum_c - sum_baseline) * 365.0 / args.days)}"
    )
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
