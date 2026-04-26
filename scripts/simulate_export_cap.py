"""Worst-case feasibility check: tightening export_cap_kwh from 6 kW → 3.68 kW.

Builds a sunny low-load full-battery 24h scenario and runs the LP twice — once with
the old 6 kW cap, once with the new G98 3.68 kW cap. Compares status, objective,
total export, and total PV curtailment. Run with ``./.venv/bin/python -m scripts.simulate_export_cap``
from the repo root.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from src.config import config
from src.scheduler.lp_optimizer import LpInitialState, solve_lp
from src.weather import WeatherLpSeries

UTC = timezone.utc
TZ = ZoneInfo(config.OPTIMIZATION_TIMEZONE or "Europe/London")
SLOT_MIN = 30
N = 48  # 24h


def build_horizon(start_utc: datetime) -> list[datetime]:
    return [start_utc + timedelta(minutes=SLOT_MIN * i) for i in range(N)]


def sunny_pv(slot_starts_utc: list[datetime]) -> list[float]:
    """Bell-shaped PV peaking at ~13:00 local with 5 kW peak (worst case for export cap)."""
    out: list[float] = []
    for s in slot_starts_utc:
        local = s.astimezone(TZ)
        h = local.hour + local.minute / 60.0
        # Triangle: 0 at h=6 and h=20, peak 5 kW (= 2.5 kWh per 30-min slot) at h=13
        if h <= 6 or h >= 20:
            kw = 0.0
        elif h <= 13:
            kw = 5.0 * (h - 6) / 7
        else:
            kw = 5.0 * (20 - h) / 7
        out.append(round(kw * 0.5, 4))  # kWh per 30-min slot
    return out


def low_base_load() -> list[float]:
    return [0.15] * N  # 300 W average — well below PV peak


def import_prices(slot_starts_utc: list[datetime]) -> list[float]:
    """Octopus-Agile-ish: cheap overnight, peak 16:00–19:00, modest day."""
    out: list[float] = []
    for s in slot_starts_utc:
        local = s.astimezone(TZ)
        h = local.hour
        if h < 5:
            p = 8.0
        elif 16 <= h < 19:
            p = 36.0
        elif 12 <= h < 16:
            p = 14.0
        else:
            p = 22.0
        out.append(p)
    return out


def export_prices(slot_starts_utc: list[datetime]) -> list[float]:
    """Octopus Outgoing-ish, with a 36p peak that makes export economically attractive."""
    out: list[float] = []
    for s in slot_starts_utc:
        local = s.astimezone(TZ)
        h = local.hour
        if 16 <= h < 19:
            p = 36.0
        elif 11 <= h < 16:
            p = 12.0
        else:
            p = 5.0
        out.append(p)
    return out


def constant(v: float) -> list[float]:
    return [v] * N


def run(cap_kwh: float, label: str) -> dict:
    # We patch the module-level export_cap_kwh by overriding FOX_EXPORT_MAX_PWR
    # since solve_lp now derives it from config.
    config_val_w = int(round(cap_kwh * 2_000.0))
    config.FOX_EXPORT_MAX_PWR = config_val_w  # runtime override

    start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    slots = build_horizon(start)

    weather = WeatherLpSeries(
        slot_starts_utc=slots,
        temperature_outdoor_c=constant(15.0),  # mild — minimal HP draw
        shortwave_radiation_wm2=constant(500.0),
        cloud_cover_pct=constant(10.0),
        pv_kwh_per_slot=sunny_pv(slots),
        cop_space=constant(3.5),
        cop_dhw=constant(2.8),
    )

    initial = LpInitialState(
        soc_kwh=float(config.BATTERY_CAPACITY_KWH),  # full battery
        tank_temp_c=50.0,                            # tank already warm
        indoor_temp_c=21.0,                          # indoor in band
    )

    plan = solve_lp(
        slot_starts_utc=slots,
        price_pence=import_prices(slots),
        base_load_kwh=low_base_load(),
        weather=weather,
        initial=initial,
        tz=TZ,
        export_price_pence=export_prices(slots),
    )

    total_pv = sum(weather.pv_kwh_per_slot)
    total_export = sum(plan.export_kwh) if plan.ok else 0.0
    total_curt = sum(plan.pv_curtail_kwh) if plan.ok else 0.0
    total_import = sum(plan.import_kwh) if plan.ok else 0.0
    peak_export_slot = max(plan.export_kwh) if plan.ok else 0.0

    return {
        "label": label,
        "cap_kw": cap_kwh * 2,
        "status": plan.status,
        "ok": plan.ok,
        "objective_p": plan.objective_pence,
        "total_pv_kwh": total_pv,
        "total_export_kwh": total_export,
        "total_curt_kwh": total_curt,
        "total_import_kwh": total_import,
        "peak_export_slot_kwh": peak_export_slot,
        "peak_export_kw": peak_export_slot * 2,
    }


def main() -> int:
    rows = [
        run(3.0, "OLD cap (6 kW)"),
        run(1.84, "NEW cap (3.68 kW, G98)"),
    ]
    w = 26
    print(f"{'metric'.ljust(w)}{rows[0]['label'].rjust(22)}{rows[1]['label'].rjust(28)}")
    print("-" * (w + 22 + 28))
    for k, fmt in [
        ("cap_kw", "{:.2f} kW"),
        ("status", "{}"),
        ("ok", "{}"),
        ("objective_p", "{:+.2f} p"),
        ("total_pv_kwh", "{:.2f} kWh"),
        ("total_export_kwh", "{:.2f} kWh"),
        ("total_curt_kwh", "{:.2f} kWh"),
        ("total_import_kwh", "{:.2f} kWh"),
        ("peak_export_kw", "{:.2f} kW"),
    ]:
        a = fmt.format(rows[0][k])
        b = fmt.format(rows[1][k])
        print(f"{k.ljust(w)}{a.rjust(22)}{b.rjust(28)}")

    if not rows[0]["ok"] or not rows[1]["ok"]:
        print("\n!! INFEASIBILITY OBSERVED — investigate !!")
        return 1

    delta_obj = rows[1]["objective_p"] - rows[0]["objective_p"]
    delta_curt = rows[1]["total_curt_kwh"] - rows[0]["total_curt_kwh"]
    delta_export = rows[1]["total_export_kwh"] - rows[0]["total_export_kwh"]
    print(
        f"\nDelta (NEW − OLD): objective {delta_obj:+.2f} p  |  "
        f"export {delta_export:+.2f} kWh  |  curtailment {delta_curt:+.2f} kWh"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
