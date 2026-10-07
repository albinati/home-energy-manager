"""Nightly LWT learning (#838): fill the REALISED half of ``lwt_learning_log``
for yesterday's local day and estimate the building UA and the pump's k.

* UA from coast windows (device offset < 0 or ~no heating): over a run of
  consecutive coast slots the indoor temperature follows
  ``T(t) - To = (T0 - To) * exp(-UA/C * t)``  =>  ``UA = C * ln((T0-To)/(T1-To)) / dt``
  (C = learned thermal mass kWh/K, UA reported in W/K).
* k from heating slots: the LP pump model is ``kW = k * (LWT - 18)``, so
  ``k = heating_kw / (lwt_actual - 18)`` (median over heating slots).
* Prediction error: planned indoor trajectory minus realised indoor.
"""
from __future__ import annotations

import logging
import math
from datetime import UTC, date, datetime, timedelta
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

from .. import db
from ..config import config

logger = logging.getLogger(__name__)

SLOT_MIN = 30
COAST_HEATING_KWH_EPS = 0.02   # per 30-min slot
MIN_COAST_BLOCK_SLOTS = 3      # >= 1 h between first/last slot means


def _z(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _tz() -> ZoneInfo:
    return ZoneInfo(str(getattr(config, "BULLETPROOF_TIMEZONE", "Europe/London") or "Europe/London"))


def day_slots_utc(day: date, tz: ZoneInfo) -> list[datetime]:
    t = datetime(day.year, day.month, day.day, tzinfo=tz).astimezone(UTC)
    end = datetime(day.year, day.month, day.day, tzinfo=tz) + timedelta(days=1)
    end = end.astimezone(UTC)
    out = []
    while t < end:
        out.append(t)
        t += timedelta(minutes=SLOT_MIN)
    return out


def fill_realised(day: date, tz: ZoneInfo | None = None) -> int:
    """Fill realised fields for every 30-min slot of the local ``day``."""
    tz = tz or _tz()
    slots = day_slots_utc(day, tz)
    if not slots:
        return 0
    start, end = slots[0], slots[-1] + timedelta(minutes=SLOT_MIN)

    def idx(ts: datetime) -> int | None:
        i = int((ts - start).total_seconds() // (SLOT_MIN * 60))
        return i if 0 <= i < len(slots) else None

    # indoor per room
    rooms: dict[int, dict[str, list[float]]] = {}
    for r in db.get_indoor_readings_range(_z(start), _z(end)):
        try:
            ts = datetime.fromisoformat(str(r["captured_at"]).replace("Z", "+00:00"))
            i = idx(ts)
            if i is None:
                continue
            rooms.setdefault(i, {}).setdefault(str(r.get("room") or "home"), []).append(float(r["temp_c"]))
        except (ValueError, TypeError, KeyError):
            continue
    # live telemetry
    tel: dict[int, list[dict[str, Any]]] = {}
    for r in db.get_daikin_telemetry_range(start.timestamp(), end.timestamp()):
        i = idx(datetime.fromtimestamp(float(r["fetched_at"]), tz=UTC))
        if i is not None:
            tel.setdefault(i, []).append(r)
    # execution_log device offset
    offs: dict[int, list[float]] = {}
    for r in db.get_execution_logs(from_ts=_z(start), to_ts=_z(end), limit=5000):
        if r.get("daikin_lwt_offset") is None:
            continue
        try:
            ts = datetime.fromisoformat(str(r["timestamp"]).replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            i = idx(ts)
        except ValueError:
            continue
        if i is not None:
            offs.setdefault(i, []).append(float(r["daikin_lwt_offset"]))
    # heating kWh: local 2h bucket prorated over its slots
    cons = {(r["date"], int(r["bucket_idx"])): r.get("kwh_heating")
            for r in db.get_daikin_consumption_2hourly_range(day.isoformat(), day.isoformat())}
    per_bucket: dict[int, int] = {}
    for st in slots:
        b = st.astimezone(tz).hour // 2
        per_bucket[b] = per_bucket.get(b, 0) + 1

    n = 0
    for i, st in enumerate(slots):
        f: dict[str, Any] = {}
        rm = rooms.get(i)
        if rm:
            means = {k: round(sum(v) / len(v), 3) for k, v in rm.items()}
            _mode, agg = db.aggregate_indoor_c(means)
            f["indoor_real_c"] = round(agg, 3)
            f["indoor_min_c"] = round(min(means.values()), 3)
            import json as _json
            f["indoor_rooms_json"] = _json.dumps(means, sort_keys=True)
        tl = tel.get(i)
        if tl:
            ot = [float(x["outdoor_temp_c"]) for x in tl if x.get("outdoor_temp_c") is not None]
            lw = [float(x["lwt_actual_c"]) for x in tl if x.get("lwt_actual_c") is not None]
            if ot:
                f["outdoor_real_c"] = round(sum(ot) / len(ot), 3)
            if lw:
                f["lwt_actual_c"] = round(sum(lw) / len(lw), 3)
        if i in offs:
            f["device_offset"] = round(sum(offs[i]) / len(offs[i]), 3)
        b = st.astimezone(tz).hour // 2
        kb = cons.get((day.isoformat(), b))
        if kb is not None:
            f["heating_kwh"] = round(float(kb) / max(1, per_bucket.get(b, 4)), 4)
        db.update_lwt_learning_realised(_z(st), f)
        n += 1
    return n


def _is_coast(r: dict[str, Any]) -> bool:
    off = r.get("device_offset")
    hk = r.get("heating_kwh")
    if off is not None and float(off) < 0:
        return True
    return hk is not None and float(hk) <= COAST_HEATING_KWH_EPS


def estimate_ua_w_per_k(rows: list[dict[str, Any]], c_kwh_per_k: float) -> tuple[float | None, int]:
    """UA (W/K) from runs of consecutive coast slots. Returns (ua, n_coast_slots)."""
    sum_log = 0.0
    sum_dt = 0.0
    n_coast = 0
    block: list[dict[str, Any]] = []

    def flush() -> None:
        nonlocal sum_log, sum_dt
        if len(block) >= MIN_COAST_BLOCK_SLOTS:
            t0, t1 = float(block[0]["indoor_real_c"]), float(block[-1]["indoor_real_c"])
            outs = [float(b["outdoor_real_c"]) for b in block if b.get("outdoor_real_c") is not None]
            if outs:
                to = sum(outs) / len(outs)
                if t0 - to > 1.0 and t1 - to > 0.5 and t1 < t0:
                    sum_log += math.log((t0 - to) / (t1 - to))
                    sum_dt += (len(block) - 1) * SLOT_MIN / 60.0
        block.clear()

    prev: datetime | None = None
    for r in rows:
        ts = datetime.fromisoformat(r["slot_time_utc"].replace("Z", "+00:00"))
        ok = _is_coast(r) and r.get("indoor_real_c") is not None
        contiguous = prev is not None and (ts - prev) == timedelta(minutes=SLOT_MIN)
        if ok:
            n_coast += 1
            if block and not contiguous:
                flush()
            block.append(r)
        else:
            flush()
        prev = ts
    flush()
    if sum_dt <= 0 or sum_log <= 0:
        return None, n_coast
    return round(c_kwh_per_k * (sum_log / sum_dt) * 1000.0, 1), n_coast


def estimate_k_kw_per_c(rows: list[dict[str, Any]]) -> tuple[float | None, int]:
    ks = []
    for r in rows:
        hk, lw = r.get("heating_kwh"), r.get("lwt_actual_c")
        if hk is None or lw is None or float(hk) <= COAST_HEATING_KWH_EPS or float(lw) - 18.0 < 2.0:
            continue
        ks.append((float(hk) / (SLOT_MIN / 60.0)) / (float(lw) - 18.0))
    return (round(float(median(ks)), 4) if ks else None), len(ks)


def prediction_error(rows: list[dict[str, Any]]) -> tuple[float | None, float | None]:
    errs = [float(r["indoor_pred_c"]) - float(r["indoor_real_c"]) for r in rows
            if r.get("indoor_pred_c") is not None and r.get("indoor_real_c") is not None]
    if not errs:
        return None, None
    ab = sorted(abs(e) for e in errs)
    p90 = ab[min(len(ab) - 1, int(math.ceil(0.9 * len(ab))) - 1)]
    return round(sum(errs) / len(errs), 3), round(p90, 3)


def run_for_day(day: date, tz: ZoneInfo | None = None) -> dict[str, Any]:
    tz = tz or _tz()
    fill_realised(day, tz)
    slots = day_slots_utc(day, tz)
    rows = db.get_lwt_learning_rows(_z(slots[0]), _z(slots[-1] + timedelta(minutes=SLOT_MIN)))
    try:
        from .thermal_learning import get_building_thermal_mass_kwh_per_k
        c = float(get_building_thermal_mass_kwh_per_k())
    except Exception:
        c = 16.5
    ua, n_coast = estimate_ua_w_per_k(rows, c)
    k, n_heat = estimate_k_kw_per_c(rows)
    pm, p90 = prediction_error(rows)
    try:
        from .thermal_learning import get_building_ua_w_per_k
        ua_pin = float(get_building_ua_w_per_k())
    except Exception:
        ua_pin = float(getattr(config, "BUILDING_UA_W_PER_K", 200))
    try:
        from ..physics import get_kw_per_degc_lwt
        k_pin = float(get_kw_per_degc_lwt())
    except Exception:
        k_pin = None
    row = {
        "date": day.isoformat(), "n_coast_slots": n_coast, "n_heat_slots": n_heat,
        "ua_est_w_per_k": ua, "k_est_kw_per_c": k,
        "pred_err_mean_c": pm, "pred_err_p90_c": p90,
        "payload": {"c_kwh_per_k": c, "ua_pinned_w_per_k": ua_pin, "k_pinned_kw_per_c": k_pin,
                    "n_rows": len(rows)},
    }
    db.upsert_lwt_learning_daily(row)
    try:
        db.log_action(device="system", action="lwt_learning_summary",
                      params={k_: v for k_, v in row.items()}, result="ok", trigger="cron")
    except Exception:
        logger.debug("lwt_learning_summary log failed", exc_info=True)
    return row
