"""One-shot importer for Fox webapp CSV exports → pv_realtime_history + fox_energy_daily.

Usage
-----
    python -m scripts.import_fox_csv_extracts [--dir data/fox-ess-daily-extracts]

The CSV format (UTF-8 BOM) is what the Fox webapp downloads:
    time, Grid Export(kW), Battery Charge(kW), Total Load(kW), Solar(kW),
    Battery Discharge(kW), Grid Import(kW), SoC(%)

Each row is a 5-min instantaneous sample. Negative ``Total Load`` means the
house was net-importing at that instant (the sign convention in the export is
``load_kw = pv + battery_discharge - grid_import - battery_charge - export``).
We always store the absolute value.

The importer is **idempotent**: re-running it never duplicates rows
(``INSERT OR IGNORE`` on the ``captured_at`` PRIMARY KEY).

For ``fox_energy_daily``, we sum 5-min kW samples → kWh (× 5/60 = ÷12) per
column and ``INSERT OR REPLACE`` per date.
"""
from __future__ import annotations

import argparse
import csv
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src import db


_FOX_TIME_FORMAT = "%d/%m/%Y %H:%M:%S"


def _parse_iso(timestr: str) -> str | None:
    """Parse ``dd/MM/yyyy HH:MM:SS {GMT|BST}`` → ISO UTC string.

    Fox webapp exports use the wall-clock zone of the inverter — GMT in winter,
    BST (UTC+1) in summer. We normalise everything to UTC.
    """
    s = (timestr or "").strip()
    if not s:
        return None
    # Strip trailing zone label, default GMT (UTC+0).
    offset_minutes = 0
    if s.endswith(" BST"):
        offset_minutes = 60
        s = s[:-4]
    elif s.endswith(" GMT"):
        s = s[:-4]
    try:
        local = datetime.strptime(s, _FOX_TIME_FORMAT)
    except (ValueError, AttributeError):
        return None
    return (local - timedelta(minutes=offset_minutes)).replace(tzinfo=UTC).isoformat()


def _abs_or_none(value: str) -> float | None:
    try:
        v = float(value.strip())
        return abs(v)
    except (ValueError, AttributeError):
        return None


def _import_one_csv(path: Path) -> tuple[int, int, dict[str, dict[str, float]]]:
    """Import one CSV. Returns (rows_inserted, rows_skipped, daily_aggregates)."""
    inserted = 0
    skipped = 0
    daily: dict[str, dict[str, float]] = {}

    with path.open(encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts = _parse_iso(row.get("time", ""))
            if not ts:
                skipped += 1
                continue
            solar = _abs_or_none(row.get("Solar(kW)", ""))
            soc_raw = row.get("SoC(%)", "").strip()
            try:
                soc = float(soc_raw) if soc_raw else None
            except ValueError:
                soc = None
            load = _abs_or_none(row.get("Total Load(kW)", ""))
            grid_imp = _abs_or_none(row.get("Grid Import(kW)", ""))
            grid_exp = _abs_or_none(row.get("Grid Export(kW)", ""))
            bat_chg = _abs_or_none(row.get("Battery Charge(kW)", ""))
            bat_dis = _abs_or_none(row.get("Battery Discharge(kW)", ""))

            ok = db.save_pv_realtime_sample(
                ts,
                solar_power_kw=solar,
                soc_pct=soc,
                load_power_kw=load,
                grid_import_kw=grid_imp,
                grid_export_kw=grid_exp,
                battery_charge_kw=bat_chg,
                battery_discharge_kw=bat_dis,
                source="csv_backfill",
            )
            if ok:
                inserted += 1
            else:
                skipped += 1

            day = ts[:10]
            agg = daily.setdefault(day, {
                "solar": 0.0, "load": 0.0, "import": 0.0,
                "export": 0.0, "charge": 0.0, "discharge": 0.0,
            })
            # 5-min sample at instantaneous kW → kWh contribution = kW × (5/60) = kW / 12
            f12 = 1.0 / 12.0
            if solar is not None: agg["solar"] += solar * f12
            if load is not None: agg["load"] += load * f12
            if grid_imp is not None: agg["import"] += grid_imp * f12
            if grid_exp is not None: agg["export"] += grid_exp * f12
            if bat_chg is not None: agg["charge"] += bat_chg * f12
            if bat_dis is not None: agg["discharge"] += bat_dis * f12

    return inserted, skipped, daily


def _upsert_daily_agg(daily: dict[str, dict[str, float]]) -> tuple[int, int]:
    """Insert CSV-derived daily totals into fox_energy_daily WHEN MISSING.

    Returns ``(inserted, skipped_existing)``. Uses ``INSERT OR IGNORE`` because
    the Fox API ``report`` endpoint is the authoritative source for daily
    totals (uses the inverter's internal cumulative meter, not 5-min sample
    integration). The CSV-derived sum is a useful backfill for historical days
    the API never covered, but should never overwrite an existing day.
    """
    from src.db import _lock, get_connection
    now = datetime.now(UTC).isoformat()
    inserted = 0
    skipped = 0
    with _lock:
        conn = get_connection()
        try:
            for date_str, agg in daily.items():
                cur = conn.execute(
                    """INSERT OR IGNORE INTO fox_energy_daily
                       (date, solar_kwh, load_kwh, import_kwh, export_kwh,
                        charge_kwh, discharge_kwh, fetched_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        date_str,
                        round(agg["solar"], 3),
                        round(agg["load"], 3),
                        round(agg["import"], 3),
                        round(agg["export"], 3),
                        round(agg["charge"], 3),
                        round(agg["discharge"], 3),
                        now,
                    ),
                )
                if cur.rowcount > 0:
                    inserted += 1
                else:
                    skipped += 1
            conn.commit()
        finally:
            conn.close()
    return inserted, skipped


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--dir",
        type=Path,
        default=Path("data/fox-ess-daily-extracts"),
        help="Directory containing PLANT*.csv exports",
    )
    args = p.parse_args(argv)

    if not args.dir.exists():
        print(f"error: directory not found: {args.dir}", file=sys.stderr)
        return 2

    db.init_db()  # ensure schema exists

    files = sorted(args.dir.glob("*.csv"))
    if not files:
        print(f"warn: no CSV files in {args.dir}", file=sys.stderr)
        return 1

    total_inserted = 0
    total_skipped = 0
    all_daily: dict[str, dict[str, float]] = {}

    for path in files:
        ins, skp, daily = _import_one_csv(path)
        total_inserted += ins
        total_skipped += skp
        # merge daily aggregates (a CSV file may span 1 day or more)
        for d, agg in daily.items():
            existing = all_daily.setdefault(d, {k: 0.0 for k in agg})
            for k, v in agg.items():
                existing[k] += v
        print(f"  {path.name}: inserted={ins:4d}, skipped={skp:4d}, days={list(daily)}")

    days_inserted, days_kept = _upsert_daily_agg(all_daily)

    print()
    print(f"=== Backfill complete ===")
    print(f"  files processed:           {len(files)}")
    print(f"  rows inserted:             {total_inserted}")
    print(f"  rows skipped (dup/bad):    {total_skipped}")
    print(f"  fox_energy_daily inserted: {days_inserted} (filled missing days)")
    print(f"  fox_energy_daily kept:     {days_kept} (Fox API value preserved)")
    print(f"  date range:                {min(all_daily)} → {max(all_daily)}" if all_daily else "  date range:             (empty)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
