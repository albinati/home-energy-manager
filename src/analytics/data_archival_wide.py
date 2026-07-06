"""Wide ML-ready monthly archive (#540).

Builds `archive/ml_wide/YYYY-MM.csv.gz`: one row per 15-min UTC slot with the
features aligned across the sensor + dispatch + tariff tables — directly
trainable (predict indoor from heating/weather/price, learn a better thermal
model, etc.). Rebuilt for the current + previous month each run (idempotent
overwrite), so a re-run never duplicates and late-arriving rows get folded in.

Timestamp normalisation is the whole game here: room_temperature_history and
agile_rates store the `...Z` form, execution_log stores `+00:00`, and the
measured 2-hourly heat is keyed by LOCAL-TZ 2h buckets. Everything is coerced
to UTC and floored to the 15-min grid before joining.
"""
from __future__ import annotations

import csv
import gzip
import io
import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from .. import db
from ..config import config

logger = logging.getLogger(__name__)

_COLUMNS = [
    "ts", "indoor_c", "indoor_n", "outdoor_c", "lwt_c", "tank_c",
    "soc_pct", "heat_kwh_2h_spread", "price_p", "export_price_p", "tier",
]
# Forward-fill execution_log (~30-min cadence) onto the 15-min grid, but only
# across a bounded gap so a dead sensor doesn't smear a stale value for days.
_FFILL_MAX_MIN = 60


def _to_utc(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return d.astimezone(UTC) if d.tzinfo else d.replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return None


def _floor15(d: datetime) -> datetime:
    return d.replace(minute=(d.minute // 15) * 15, second=0, microsecond=0)


def _tier(p: float | None, cheap: float, peak: float) -> str | None:
    if p is None:
        return None
    if p < 0:
        return "negative"
    if p <= cheap:
        return "cheap"
    if p >= peak:
        return "peak"
    return "standard"


def _month_windows(now: datetime) -> list[tuple[datetime, datetime]]:
    """(start, end) UTC for the current and previous calendar month."""
    cur_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    nxt = (cur_start + timedelta(days=32)).replace(day=1)
    prev_start = (cur_start - timedelta(days=1)).replace(day=1)
    return [(prev_start, cur_start), (cur_start, nxt)]


def _bucket_latest(rows, ts_key, cols):
    """Latest value per 15-min bucket for each column (from ~30-min rows)."""
    out: dict[datetime, dict] = {}
    for r in rows:
        d = _to_utc(r.get(ts_key))
        if d is None:
            continue
        b = _floor15(d)
        prev = out.get(b)
        # keep the row with the newest raw ts inside the bucket
        if prev is None or d >= prev["_ts"]:
            out[b] = {"_ts": d, **{c: r.get(c) for c in cols}}
    return out


def build_ml_wide_archive(archive_root_fn) -> dict:
    """Build the wide monthly CSV.gz for the current + previous month."""
    now = datetime.now(UTC)
    tz = ZoneInfo(config.BULLETPROOF_TIMEZONE or "Europe/London")
    cheap = float(getattr(config, "OPTIMIZATION_CHEAP_THRESHOLD_PENCE", 12.0))
    peak = float(getattr(config, "OPTIMIZATION_PEAK_THRESHOLD_PENCE", 25.0))
    import_code = (getattr(config, "OCTOPUS_TARIFF_CODE", "") or "").strip() or None
    out_dir = archive_root_fn() / "ml_wide"

    written: dict[str, int] = {}
    for m_start, m_end in _month_windows(now):
        end = min(m_end, _floor15(now))
        if end <= m_start:
            continue

        # --- pull sources for the window (a little padding for ffill) ---
        pad_start = (m_start - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        win_start_z = m_start.strftime("%Y-%m-%dT%H:%M:%SZ")
        win_end_z = end.strftime("%Y-%m-%dT%H:%M:%SZ")

        indoor = db.get_indoor_readings_range(pad_start, win_end_z)
        indoor_by_bucket: dict[datetime, list[float]] = {}
        for r in indoor:
            d = _to_utc(r.get("captured_at"))
            if d is None or r.get("temp_c") is None:
                continue
            indoor_by_bucket.setdefault(_floor15(d), []).append(float(r["temp_c"]))

        try:
            ex_rows = db.get_execution_logs(pad_start, win_end_z, 100000)
        except Exception:
            ex_rows = []
        ex_by_bucket = _bucket_latest(
            ex_rows, "timestamp",
            ["daikin_outdoor_temp", "daikin_lwt", "daikin_tank_temp", "soc_percent"],
        )

        # prices: build (from,to,value) intervals
        def _intervals(rows):
            iv = []
            for r in rows or []:
                f, t = _to_utc(r.get("valid_from")), _to_utc(r.get("valid_to"))
                if f and t and r.get("value_inc_vat") is not None:
                    iv.append((f, t, float(r["value_inc_vat"])))
            iv.sort()
            return iv
        try:
            # get_rates_for_period takes datetime objects (not ISO strings).
            imp_iv = _intervals(db.get_rates_for_period(import_code, m_start, end)) if import_code else []
        except Exception:
            imp_iv = []
        try:
            exp_iv = _intervals(db.get_agile_export_rates_in_range(win_start_z, win_end_z))
        except Exception:
            exp_iv = []

        def _price_at(dt, iv):
            for f, t, v in iv:
                if f <= dt < t:
                    return v
            return None

        # measured 2-hourly heat (local-TZ buckets) → spread to 15-min (÷8)
        heat_2h: dict[tuple[str, int], float] = {}
        try:
            d0 = m_start.astimezone(tz).date()
            d1 = end.astimezone(tz).date()
            for row in db.get_daikin_consumption_2hourly_range(d0.isoformat(), d1.isoformat()):
                if row.get("kwh_heating") is not None:
                    heat_2h[(str(row.get("date")), int(row.get("bucket_idx")))] = float(row["kwh_heating"])
        except Exception:
            heat_2h = {}

        # --- walk the 15-min grid ---
        rows_out = []
        ff = {"daikin_outdoor_temp": None, "daikin_lwt": None, "daikin_tank_temp": None, "soc_percent": None}
        ff_age = {k: 999 for k in ff}
        cur = m_start
        while cur < end:
            b = ex_by_bucket.get(cur)
            for k in ff:
                if b is not None and b.get(k) is not None:
                    ff[k] = b[k]
                    ff_age[k] = 0
                else:
                    ff_age[k] += 15
                    if ff_age[k] > _FFILL_MAX_MIN:
                        ff[k] = None

            temps = indoor_by_bucket.get(cur)
            price = _price_at(cur, imp_iv)
            loc = cur.astimezone(tz)
            hk = heat_2h.get((loc.date().isoformat(), loc.hour // 2))
            rows_out.append({
                "ts": cur.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "indoor_c": round(sum(temps) / len(temps), 2) if temps else None,
                "indoor_n": len(temps) if temps else 0,
                "outdoor_c": ff["daikin_outdoor_temp"],
                "lwt_c": ff["daikin_lwt"],
                "tank_c": ff["daikin_tank_temp"],
                "soc_pct": ff["soc_percent"],
                "heat_kwh_2h_spread": round(hk / 8.0, 4) if hk is not None else None,
                "price_p": price,
                "export_price_p": _price_at(cur, exp_iv),
                "tier": _tier(price, cheap, peak),
            })
            cur += timedelta(minutes=15)

        # --- write (overwrite the month, idempotent) ---
        month = m_start.strftime("%Y-%m")
        out_dir.mkdir(parents=True, exist_ok=True)
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=_COLUMNS)
        w.writeheader()
        w.writerows(rows_out)
        with gzip.open(out_dir / f"{month}.csv.gz", "wt", encoding="utf-8", newline="") as fh:
            fh.write(buf.getvalue())
        written[month] = len(rows_out)

    logger.info("archival: ml-wide rebuilt %s", written)
    return written
