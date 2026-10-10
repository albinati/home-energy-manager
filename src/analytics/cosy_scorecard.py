"""Daily Cosy scorecard (#831, epic #830) — scores YESTERDAY (local day).

Read-only: it composes existing readers (tariff windows, grid/load/PV
roll-ups, load_error_log, committed LP stitch, indoor/tank telemetry,
action_log, PnL) into ONE row per day, persisted in ``cosy_scorecard_daily``.
It never changes a setting. Every section is independently guarded so a
missing input degrades that section to ``None`` rather than losing the day.

Alerts (one ``notify_risk`` each, deduped per date with
``db.acknowledge_warning``; a final ``daikin_write_verify`` failure is NOT
re-alerted here — the verifier notifies once itself): 3 consecutive days of peak-band load
under-forecast; tank below a shower-window floor at entry; Daikin quota above
``COSY_SCORECARD_QUOTA_ALERT``.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .. import db
from ..config import config

logger = logging.getLogger(__name__)

_UNDER_FORECAST_MIN_KWH = 0.15
_UNDER_FORECAST_REL = 0.10
_TANK_FLOOR_TOL_C = 0.5


def _tz() -> ZoneInfo:
    return ZoneInfo(str(getattr(config, "BULLETPROOF_TIMEZONE", "Europe/London") or "Europe/London"))


def _r(v: Any, n: int = 2) -> float | None:
    try:
        return None if v is None else round(float(v), n)
    except (TypeError, ValueError):
        return None


def _z(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(iso: Any) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def _section(label: str, fn, *args, **kw) -> Any:
    try:
        return fn(*args, **kw)
    except Exception as exc:  # noqa: BLE001 — one section must never lose the day
        logger.warning("cosy_scorecard: %s failed: %s", label, exc, exc_info=True)
        return {"error": f"{type(exc).__name__}: {exc}"}


def _is_peak(w: Any) -> bool:
    return str(w.key) == "band_peak" or str(w.label).lower() == "peak"


# --------------------------------------------------------------------- bands
def _bands(day: date, windows: list[Any], tz: ZoneInfo, a: datetime, b: datetime) -> dict[str, Any]:
    from .load_expected import _window_sum

    cols = {
        "import": "grid_import_kw", "load": "load_power_kw", "pv": "solar_power_kw",
        "discharge": "battery_discharge_kw", "charge": "battery_charge_kw",
    }
    roll = {k: db.half_hourly_kwh_for_utc_range(a, b, c) for k, c in cols.items()}
    errs = db.get_load_error_log_range(_z(a), _z(b))
    fc: dict[str, float] = {}
    ac: dict[str, float] = {}
    for r in errs:
        if r.get("forecast_kwh") is not None and r.get("actual_kwh") is not None:
            fc[r["slot_time_utc"]] = float(r["forecast_kwh"])
            ac[r["slot_time_utc"]] = float(r["actual_kwh"])

    out_bands: list[dict[str, Any]] = []
    for w in windows:
        imp, n_imp = _window_sum(roll["import"], w, tz, day=day)
        f_kwh, n_f = _window_sum(fc, w, tz, day=day)
        a_kwh, _ = _window_sum(ac, w, tz, day=day)
        err = (a_kwh - f_kwh) if n_f else None
        under = None
        if err is not None:
            under = bool(err > max(_UNDER_FORECAST_MIN_KWH, _UNDER_FORECAST_REL * f_kwh))
        out_bands.append({
            "key": str(w.key), "label": str(w.label), "is_peak": _is_peak(w),
            "start_local": w.start_utc.astimezone(tz).strftime("%H:%M"),
            "end_local": w.end_utc.astimezone(tz).strftime("%H:%M"),
            "price_p": _r(w.price_p), "hours": _r(w.hours),
            "import_kwh": _r(imp, 3) if n_imp else None,
            "import_cost_gbp": _r(imp * float(w.price_p) / 100.0, 3) if n_imp else None,
            "load_kwh": _r(_window_sum(roll["load"], w, tz, day=day)[0], 3),
            "pv_kwh": _r(_window_sum(roll["pv"], w, tz, day=day)[0], 3),
            "battery_discharge_kwh": _r(_window_sum(roll["discharge"], w, tz, day=day)[0], 3),
            "forecast_load_kwh": _r(f_kwh, 3) if n_f else None,
            "actual_load_kwh": _r(a_kwh, 3) if n_f else None,
            "load_error_kwh": _r(err, 3),
            "under_forecast": under,
        })
    charge_kwh = sum(
        float(v) for k, v in roll["charge"].items()
        if (t := _parse(k)) is not None and t.astimezone(tz).date() == day
    )
    return {"bands": out_bands, "battery_charge_kwh": charge_kwh}


# ------------------------------------------------------------------- battery
def _soc(day: date, tz: ZoneInfo) -> dict[str, Any]:
    from . import plan_fronts as pf

    cap = float(getattr(config, "BATTERY_CAPACITY_KWH", 0.0) or 0.0)
    committed = pf._committed_by_start(day, "soc_kwh", tz)
    committed.update(pf._committed_by_start(day + timedelta(days=1), "soc_kwh", tz))
    edges = []
    for label, d, h in (("07:00", day, 7), ("16:00", day, 16), ("00:00", day + timedelta(days=1), 0)):
        t = datetime(d.year, d.month, d.day, h, tzinfo=tz).astimezone(UTC)
        # soc_kwh of an LP slot is the END-of-slot SoC, so the at-instant plan
        # value for an edge is the slot that ENDS there (starts 30 min earlier).
        planned = committed.get(t - timedelta(minutes=30))
        pct = db.get_soc_pct_at(t)
        real_kwh = (pct / 100.0 * cap) if (pct is not None and cap) else None
        edges.append({
            "edge": label, "planned_soc_kwh": _r(planned),
            "realised_soc_kwh": _r(real_kwh), "realised_soc_pct": _r(pct, 1),
            "delta_kwh": _r(real_kwh - planned) if (real_kwh is not None and planned is not None) else None,
        })
    floor: dict[str, Any] = {"floor_binding_slots": None, "run_id": None}
    inputs = db.get_latest_lp_inputs_for_plan_date(day.isoformat())
    if inputs:
        floor["run_id"] = inputs.get("run_id")
        import json
        try:
            pcf = (json.loads(inputs.get("exogenous_snapshot_json") or "{}")).get("pess_charge_floor") or {}
            if pcf.get("binding_slots") is not None:
                floor["floor_binding_slots"] = int(pcf["binding_slots"])
                floor["insurance_cost_pence"] = pcf.get("insurance_cost_pence")
        except (ValueError, TypeError):
            pass
    return {"edges": edges, "pess_floor": floor, "capacity_kwh": _r(cap)}


# ------------------------------------------------------------------- comfort
def _comfort(day: date, windows: list[Any], tz: ZoneInfo, a: datetime, b: datetime) -> dict[str, Any]:
    rows = db.get_indoor_readings_range(_z(a), _z(b))
    rooms: dict[str, list[float]] = {}
    buckets: dict[datetime, dict[str, float]] = {}
    for r in rows:
        t = _parse(r.get("captured_at"))
        if t is None or r.get("temp_c") is None:
            continue
        room = str(r.get("room") or "home")
        v = float(r["temp_c"])
        rooms.setdefault(room, []).append(v)
        bt = t.replace(minute=(t.minute // 15) * 15, second=0, microsecond=0)
        buckets.setdefault(bt, {})[room] = v  # latest reading per room within the 15-min bucket
    per_room = {
        k: {"min_c": _r(min(v), 1), "max_c": _r(max(v), 1), "mean_c": _r(sum(v) / len(v), 1), "n": len(v)}
        for k, v in sorted(rooms.items())
    }
    night_floor = float(config.LP_W3_NIGHT_FLOOR_C)
    peak_floor = float(config.INDOOR_SETPOINT_C) - float(config.LP_W3_PEAK_COAST_DELTA_C)
    n0, n1 = int(config.LP_W3_NIGHT_START_HOUR_LOCAL), int(config.LP_W3_NIGHT_END_HOUR_LOCAL)
    peak_slots: set[tuple[int, int]] = set()
    for w in windows:
        if _is_peak(w):
            peak_slots |= set(w.local_slots)
    mode = "mean"
    below_night = below_peak = 0.0
    for bt in sorted(buckets):
        try:
            mode, house = db.aggregate_indoor_c(buckets[bt])
        except ValueError:
            continue
        lt = bt.astimezone(tz)
        if lt.date() != day:
            continue
        night = (lt.hour >= n0 or lt.hour < n1) if n0 > n1 else (n0 <= lt.hour < n1)
        if night and house < night_floor:
            below_night += 0.25
        if (lt.hour, 30 if lt.minute >= 30 else 0) in peak_slots and house < peak_floor:
            below_peak += 0.25
    return {
        "aggregate": str(getattr(config, "INDOOR_COMFORT_AGGREGATE", "mean")), "aggregate_applied": mode,
        "rooms": per_room, "n_readings": len(rows),
        "night_floor_c": _r(night_floor, 1), "peak_floor_c": _r(peak_floor, 1),
        "hours_below_night_floor": _r(below_night) if buckets else None,
        "hours_below_peak_floor": _r(below_peak) if buckets else None,
    }


# ---------------------------------------------------------------------- tank
def _tank(day: date, tz: ZoneInfo) -> dict[str, Any]:
    from .. import dhw_policy
    from ..dhw.comfort import shower_windows

    out: dict[str, Any] = {"showers": [], "any_below_floor": None, "decision": {}, "coast_ratio_median_recent": None}
    params = None
    try:
        from ..dhw.params import live_indoor_ambient_c, resolve_tank_params

        params = resolve_tank_params(ambient_c=live_indoor_ambient_c())
    except Exception:  # noqa: BLE001
        logger.debug("cosy_scorecard: tank params failed", exc_info=True)
    preset = str(getattr(config, "OPTIMIZATION_PRESET", "normal") or "normal")
    any_below = False
    seen = False
    for sw in shower_windows(preset=preset, p=params):
        hh = int(sw.start_hour)
        mm = int(round((float(sw.start_hour) - hh) * 60))
        t = datetime(day.year, day.month, day.day, hh, mm, tzinfo=tz).astimezone(UTC)
        pts = db.get_tank_temps_range(t.timestamp() - 1200, t.timestamp() + 1200)
        tank_c = None
        if pts:
            tank_c = min(pts, key=lambda p: abs(p[0] - t.timestamp()))[1]
        below = None
        if tank_c is not None:
            seen = True
            below = bool(tank_c < float(sw.floor_c) - _TANK_FLOOR_TOL_C)
            any_below = any_below or below
        out["showers"].append({
            "label": sw.label, "entry_local": f"{hh:02d}:{mm:02d}", "floor_c": _r(sw.floor_c, 1),
            "tank_c": _r(tank_c, 1), "below_floor": below,
        })
    out["any_below_floor"] = any_below if seen else None
    try:
        d = dhw_policy.read_window_decision(day)
        out["decision"] = {
            "arm": d.arm, "setback_hour": d.setback_hour_local, "peak_entry_hour": d.peak_entry_hour_local,
            "warmup_hour": dhw_policy.read_warmup_hour(day),
        }
    except Exception as exc:  # noqa: BLE001
        out["decision"] = {"error": f"{type(exc).__name__}: {exc}"}
    try:
        chk = db.get_dhw_calibration("coast_check")
        if chk:
            pl = chk.get("payload") or {}
            out["coast_ratio_median_recent"] = _r(pl.get("ratio_median_recent"), 3)
            out["coast_check_status"] = chk.get("status")
    except Exception:  # noqa: BLE001
        logger.debug("cosy_scorecard: coast_check read failed", exc_info=True)
    return out


# ----------------------------------------------------------------------- lwt
def _band_name(w: Any) -> str:
    if _is_peak(w):
        return "peak"
    k = f"{w.key} {w.label}".lower()
    return "cheap" if "cheap" in k else "standard"


def _heating_kwh_by_band(day: date, windows: list[Any], tz: ZoneInfo, a: datetime, b: datetime) -> dict[str, Any]:
    """#841 — Daikin space-heating kWh per tariff band. The 2-hourly
    ``kwh_heating`` bucket is prorated over its 30-min slots (as
    ``lwt_learning.fill_realised`` does) and each slot is assigned to the band
    window covering its LOCAL (hour, minute). ``None`` when no consumption row."""
    cons = {int(r["bucket_idx"]): r.get("kwh_heating")
            for r in db.get_daikin_consumption_2hourly_range(day.isoformat(), day.isoformat())}
    if not cons:
        return {"cheap": None, "standard": None, "peak": None}
    slot_band: dict[tuple[int, int], str] = {}
    for w in windows:
        for hm in w.local_slots:
            slot_band[hm] = _band_name(w)
    out = {"cheap": 0.0, "standard": 0.0, "peak": 0.0}
    st = a
    slots = []
    while st < b:
        slots.append(st.astimezone(tz))
        st += timedelta(minutes=30)
    per_bucket: dict[int, int] = {}
    for lt in slots:
        per_bucket[lt.hour // 2] = per_bucket.get(lt.hour // 2, 0) + 1
    for lt in slots:
        kb = cons.get(lt.hour // 2)
        if kb is None:
            continue
        band = slot_band.get((lt.hour, 30 if lt.minute >= 30 else 0), "standard")
        out[band] += float(kb) / max(1, per_bucket.get(lt.hour // 2, 4))
    return {k: _r(v, 3) for k, v in out.items()}


def _indoor_min_max(day: date, tz: ZoneInfo, a: datetime, b: datetime) -> tuple[float | None, float | None]:
    """Min / max of the AGGREGATE house temperature (INDOOR_COMFORT_AGGREGATE)
    over the local day, 15-min buckets (same bucketing as ``_comfort``)."""
    buckets: dict[datetime, dict[str, float]] = {}
    for r in db.get_indoor_readings_range(_z(a), _z(b)):
        t = _parse(r.get("captured_at"))
        if t is None or r.get("temp_c") is None:
            continue
        bt = t.replace(minute=(t.minute // 15) * 15, second=0, microsecond=0)
        # later reading in the same bucket wins (the range is time-ordered)
        buckets.setdefault(bt, {})[str(r.get("room") or "home")] = float(r["temp_c"])
    all_rooms: set[str] = set()
    for rooms_b in buckets.values():
        all_rooms.update(rooms_b)
    # A room on a slower cadence is absent from some buckets; aggregating only
    # the rooms that happened to report biases min/max. Carry each room's last
    # reading forward, and aggregate only once EVERY room seen has reported.
    last: dict[str, float] = {}
    vals: list[float] = []
    for bt in sorted(buckets):
        last.update(buckets[bt])
        if bt.astimezone(tz).date() != day:
            continue
        if len(last) < len(all_rooms):
            continue
        try:
            _m, house = db.aggregate_indoor_c(dict(last))
        except ValueError:
            continue
        vals.append(float(house))
    return (_r(min(vals), 1), _r(max(vals), 1)) if vals else (None, None)


def _lwt(day: date, tz: ZoneInfo, a: datetime, b: datetime, windows: list[Any] | None = None) -> dict[str, Any]:
    n_pre = n_restore = 0
    for pd in ((day - timedelta(days=1)).isoformat(), day.isoformat()):
        for r in db.get_actions_for_plan_date(pd, "daikin"):
            at = str(r.get("action_type") or "")
            if at not in ("lwt_preheat", "restore"):
                continue
            st = _parse(r.get("start_time"))
            if st is None or not (a <= st < b):
                continue
            if at == "lwt_preheat":
                n_pre += 1
            else:
                n_restore += 1
    # "mismatch" counts only FINAL failures (attempt >= 2 — the verifier already
    # notified once for those); a first-attempt failure is a retry, informational.
    verify = {"success": 0, "unverified": 0, "mismatch": 0, "retried": 0}
    for r in db.get_action_logs(device="daikin", action="daikin_write_verify", since=a.isoformat(), limit=1000):
        t = _parse(r.get("timestamp"))
        if t is None or t >= b:
            continue
        res = str(r.get("result") or "")
        if res == "failure":
            final = int((r.get("params") or {}).get("attempt") or 1) >= 2
            verify["mismatch" if final else "retried"] += 1
        else:
            verify["success" if res == "success" else "unverified"] += 1
    diff: dict[str, Any] = {}
    for r in db.get_action_logs(device="daikin", action="lwt_source_diff", since=a.isoformat(), limit=200):
        t = _parse(r.get("timestamp"))
        if t is None or t >= b:
            continue
        p = r.get("params") or {}
        diff = {k: p.get(k) for k in ("source_used", "lp_available", "n_differ", "mean_abs_diff")}
        break  # newest first
    n_backstops = 0
    try:
        for r in db.get_action_logs(device="daikin", action="lwt_comfort_backstop", since=a.isoformat(), limit=200):
            t = _parse(r.get("timestamp"))
            if t is not None and t < b and str(r.get("result") or "") == "ok":
                n_backstops += 1
    except Exception:  # noqa: BLE001
        logger.debug("cosy_scorecard: backstop count failed", exc_info=True)
    n_gate_skips = 0
    n_gate_windows = 0
    try:
        for r in db.get_action_logs(device="daikin", action="lwt_demand_gate", since=a.isoformat(), limit=500):
            t = _parse(r.get("timestamp"))
            if t is not None and t < b:
                n_gate_skips += 1
                try:
                    _p = r.get("params")
                    _p = json.loads(_p) if isinstance(_p, str) else (_p or {})
                    n_gate_windows += int(_p.get("windows_suppressed") or 0)
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001
        logger.debug("cosy_scorecard: demand-gate count failed", exc_info=True)
    by_band: dict[str, Any] | None = None
    try:
        by_band = _heating_kwh_by_band(day, windows or [], tz, a, b)
    except Exception:  # noqa: BLE001
        logger.debug("cosy_scorecard: heating_kwh_by_band failed", exc_info=True)
    imin = imax = None
    try:
        imin, imax = _indoor_min_max(day, tz, a, b)
    except Exception:  # noqa: BLE001
        logger.debug("cosy_scorecard: indoor min/max failed", exc_info=True)
    return {"preheat_rows": n_pre, "restore_rows": n_restore, "write_verify": verify,
            "source_diff_last": diff, "lwt_backstops": n_backstops,
            "demand_gate_closed_dispatches": n_gate_skips,
            "demand_gate_windows_suppressed": n_gate_windows,
            "heating_kwh_by_band": by_band, "indoor_min_c": imin, "indoor_max_c": imax}


def _ops(a: datetime, b: datetime) -> dict[str, Any]:
    from .. import api_quota

    # api_call_log keeps 48 h: an older day would read as a false 0.
    retained = a.timestamp() >= time.time() - 48 * 3600
    out: dict[str, Any] = {
        "daikin_calls": api_quota.count_calls_between("daikin", a.timestamp(), b.timestamp()) if retained else None
    }
    out["daikin_budget"] = int(getattr(config, "DAIKIN_DAILY_BUDGET", 180))
    fails = 0
    for r in db.get_action_logs(device="foxess", since=a.isoformat(), limit=2000):
        t = _parse(r.get("timestamp"))
        if t is None or t >= b:
            continue
        if str(r.get("result") or "").lower() in ("failure", "failed", "error"):
            fails += 1
    out["fox_failures"] = fails
    return out


def _spend(day: date, windows: list[Any], tz: ZoneInfo, a: datetime, b: datetime) -> dict[str, Any]:
    """REALISED import kWh / £ / avg p / peak kWh / score from the Fox grid-import
    roll-up × per-slot prices. DB-only (no PnL, no HTTP) and always realised for
    a completed day, whatever the import volume."""
    from ..energy.tariff_structure import detect, is_tou_family
    from . import plan_fronts as pf

    code = str(getattr(config, "OCTOPUS_TARIFF_CODE", "") or "")
    prices = pf.day_prices(day, windows, tz)
    out: dict[str, Any] = {
        "has_telemetry": False, "import_kwh": None, "import_cost_gbp": None, "avg_import_p": None,
        "peak_import_kwh": None, "ideal_avg_import_p": None, "score": None,
        "score_thresholds": {"ideal_max_p": None, "above_min_p": None},
    }
    roll = db.half_hourly_kwh_for_utc_range(a, b, "grid_import_kw")
    out["has_telemetry"] = bool(roll)
    if not prices:
        return out
    struct = detect(list(prices.values()), short_ok=is_tou_family(code))
    mean_price = sum(prices.values()) / len(prices)
    kwh = cost_p = peak = 0.0
    for iso, v in roll.items():
        t = _parse(iso)
        if t is None:
            continue
        price = prices.get(t, mean_price)
        kwh += float(v)
        cost_p += float(v) * price
        if struct.peak_thr > 0 and price >= struct.peak_thr:
            peak += float(v)
    if not roll:
        return out
    banded = bool(struct.is_banded)
    if banded and struct.cheap_level is not None:
        ideal, above = float(struct.cheap_level), float(struct.cheap_thr)
    else:
        ideal = float(struct.cheap_thr)
        vs = sorted(prices.values())
        above = vs[len(vs) // 2]
    ratio = float(getattr(config, "SPEND_SCORE_IDEAL_RATIO", 1.15))
    avg = (cost_p / kwh) if kwh > 0 else None
    out.update({
        "import_kwh": _r(kwh, 3), "import_cost_gbp": _r(cost_p / 100.0, 3),
        "avg_import_p": _r(avg), "peak_import_kwh": _r(peak, 3), "ideal_avg_import_p": _r(ideal),
        "score_thresholds": {"ideal_max_p": _r(ideal * ratio), "above_min_p": _r(above)},
        "score": pf.spend_score(avg, peak, ideal, above, ratio, banded=banded),
    })
    return out


# --------------------------------------------------------------------- build
def build_scorecard(day: date, *, tz: ZoneInfo | None = None) -> dict[str, Any]:
    """Compose the scorecard row for local ``day`` (not persisted)."""
    from . import pnl
    from . import plan_fronts as pf
    from .load_expected import band_windows_for_day

    tz = tz or _tz()
    a, b = pf._local_day_bounds(day, tz)
    windows, structure = band_windows_for_day(day, tz)
    payload: dict[str, Any] = {"structure": structure, "tz": str(tz.key)}

    spend = _section("spend", _spend, day, windows, tz, a, b)
    spend = spend if isinstance(spend, dict) else {}
    payload["spend"] = spend
    payload["has_telemetry"] = bool(spend.get("has_telemetry"))

    bands = _section("bands", _bands, day, windows, tz, a, b)
    payload["bands"] = bands.get("bands") if isinstance(bands, dict) else bands
    battery_charge = bands.get("battery_charge_kwh") if isinstance(bands, dict) else None
    peak_bands = [x for x in (payload["bands"] or []) if isinstance(x, dict) and x.get("is_peak")]
    payload["peak_under_forecast"] = (
        any(x.get("under_forecast") for x in peak_bands) if any(x.get("under_forecast") is not None for x in peak_bands) else None
    )

    soc = _section("soc", _soc, day, tz)
    payload["battery"] = soc if isinstance(soc, dict) else {}
    cap = float(getattr(config, "BATTERY_CAPACITY_KWH", 0.0) or 0.0)
    if isinstance(payload["battery"], dict):
        payload["battery"]["charge_kwh"] = _r(battery_charge, 3)
        payload["battery"]["cycles"] = _r(battery_charge / cap, 3) if (battery_charge is not None and cap) else None

    payload["comfort"] = _section("comfort", _comfort, day, windows, tz, a, b)
    payload["tank"] = _section("tank", _tank, day, tz)
    payload["lwt"] = _section("lwt", _lwt, day, tz, a, b, windows)
    payload["ops"] = _section("ops", _ops, a, b)

    money: dict[str, Any] = {}
    try:
        p = pnl.compute_daily_pnl(day, standing_source="manual")  # ONCE; DB-only
        money = {
            "realised_net_cost_gbp": _r(p.get("realised_net_cost_gbp"), 4),
            "delta_vs_fixed_tariff_real_gbp": _r(p.get("delta_vs_fixed_tariff_real_gbp"), 4),
        }
    except Exception as exc:  # noqa: BLE001
        money = {"error": f"{type(exc).__name__}: {exc}"}
    prev = [r.get("net_cost_gbp") for r in db.get_cosy_scorecards(6, up_to=(day - timedelta(days=1)).isoformat())
            if r.get("net_cost_gbp") is not None and (day - date.fromisoformat(r["date"])).days <= 6]
    cur = money.get("realised_net_cost_gbp")
    series = ([float(cur)] if cur is not None else []) + [float(x) for x in prev]
    money["rolling7_mean_net_cost_gbp"] = _r(sum(series) / len(series), 4) if series else None
    payload["money"] = money

    peak_kwh = spend.get("peak_import_kwh")
    return {
        "date": day.isoformat(),
        "score": spend.get("score"),
        "peak_import_kwh": _r(peak_kwh, 3),
        "import_kwh": spend.get("import_kwh"),
        "import_cost_gbp": spend.get("import_cost_gbp"),
        "avg_import_p": spend.get("avg_import_p"),
        "ideal_avg_import_p": spend.get("ideal_avg_import_p"),
        "net_cost_gbp": money.get("realised_net_cost_gbp"),
        "payload": payload,
        "built_at_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def persist_scorecard(row: dict[str, Any]) -> None:
    db.upsert_cosy_scorecard(row)


def get_scorecards(days: int = 14) -> list[dict[str, Any]]:
    return db.get_cosy_scorecards(max(1, min(90, int(days))))


# -------------------------------------------------------------------- alerts
def evaluate_alerts(row: dict[str, Any]) -> list[tuple[str, str]]:
    """``[(warning_key, message)]`` for the conditions the row trips."""
    day = str(row["date"])
    p = row.get("payload") or {}
    out: list[tuple[str, str]] = []

    if p.get("peak_under_forecast"):
        d = date.fromisoformat(day)
        prior = {r["date"]: r for r in db.get_cosy_scorecards(4, up_to=(d - timedelta(days=1)).isoformat())}
        streak = all(
            ((prior.get((d - timedelta(days=i)).isoformat()) or {}).get("payload") or {}).get("peak_under_forecast")
            for i in (1, 2)
        )
        if streak:
            out.append((f"cosy_peak_underforecast_{(d - timedelta(days=2)).isoformat()}",
                        f"Peak-band load under-forecast 3 days running (to {day}) - the battery may be sized too small for the peak."))
    low = [s for s in ((p.get("tank") or {}).get("showers") or []) if s.get("below_floor")]
    if low:
        txt = ", ".join(f"{s['label']} {s['tank_c']}C < {s['floor_c']}C at {s['entry_local']}" for s in low)
        out.append((f"cosy_tank_floor_{day}", f"Tank below the shower floor on {day}: {txt}."))
    calls = (p.get("ops") or {}).get("daikin_calls")
    lim = int(getattr(config, "COSY_SCORECARD_QUOTA_ALERT", 150))
    if calls is not None and int(calls) > lim:
        out.append((f"cosy_quota_{day}", f"Daikin used {calls} API calls on {day} (alert above {lim}/{(p.get('ops') or {}).get('daikin_budget')})."))
    return out


def fire_alerts(row: dict[str, Any]) -> int:
    """Notify each tripped alert once per date. Returns the number sent."""
    from ..notifier import notify_risk

    sent = 0
    for key, msg in evaluate_alerts(row):
        if db.is_warning_acknowledged(key):
            continue
        notify_risk(msg, extra={"warning_key": key})
        db.acknowledge_warning(key)
        sent += 1
    return sent


# ----------------------------------------------------------------------- job
def run_for_day(day: date) -> dict[str, Any]:
    row = build_scorecard(day)
    persist_scorecard(row)
    try:
        fire_alerts(row)
    except Exception:  # noqa: BLE001
        logger.warning("cosy_scorecard: alerting failed (non-fatal)", exc_info=True)
    return row


def backfill_missing(*, days: int = 7, today: date | None = None) -> list[str]:
    """Score every missing day of the last ``days`` (yesterday back) and re-score
    rows built within 48 h of their day (late Octopus/Fox data corrects them).
    Yesterday goes through ``run_for_day`` (with alerts, deduped); older days are
    build+persist only. Days with no grid telemetry are skipped."""
    tz = _tz()
    today = today or datetime.now(tz).date()
    have = {r["date"]: r for r in db.get_cosy_scorecards(days + 2)}
    done: list[str] = []
    for i in range(1, days + 1):
        d = today - timedelta(days=i)
        if d < _start_clamp():
            continue
        existing = have.get(d.isoformat())
        if existing is not None:
            built = _parse(existing.get("built_at_utc"))
            day_end = datetime(d.year, d.month, d.day, tzinfo=tz).astimezone(UTC) + timedelta(days=1)
            if built is None or built - day_end >= timedelta(hours=48):
                continue  # settled
        try:
            if d == today - timedelta(days=1):
                row = build_scorecard(d)
                if not row["payload"].get("has_telemetry"):
                    continue
                persist_scorecard(row)
                try:
                    fire_alerts(row)
                except Exception:  # noqa: BLE001
                    logger.warning("cosy_scorecard: alerting failed (non-fatal)", exc_info=True)
            else:
                row = build_scorecard(d)
                if not row["payload"].get("has_telemetry"):
                    continue
                persist_scorecard(row)
            done.append(d.isoformat())
        except Exception:  # noqa: BLE001
            logger.warning("cosy_scorecard backfill %s failed", d, exc_info=True)
    return done


def _start_clamp() -> date:
    raw = str(getattr(config, "SMART_TARIFF_START_DATE", "") or "")
    try:
        return date.fromisoformat(raw) if raw else date.min
    except ValueError:
        return date.min


def brief_line(today: date | None = None) -> str | None:
    """ONE line for the morning brief from yesterday's stored row, or None."""
    tz = _tz()
    today = today or datetime.now(tz).date()
    rows = db.get_cosy_scorecards(1, up_to=(today - timedelta(days=1)).isoformat())
    if not rows or rows[0]["date"] != (today - timedelta(days=1)).isoformat():
        return None
    r = rows[0]
    if r.get("import_kwh") is None:
        return None
    parts = [f"{r['import_kwh']:.1f} kWh import", f"{(r.get('peak_import_kwh') or 0.0):.1f} at peak"]
    if r.get("avg_import_p") is not None:
        s = f"{r['avg_import_p']:.1f}p avg"
        if r.get("ideal_avg_import_p") is not None:
            s += f" (ideal {r['ideal_avg_import_p']:.1f})"
        parts.append(s)
    if r.get("score"):
        parts.append({"ideal": "ideal day", "below": "below usual", "above": "above usual"}.get(r["score"], r["score"]))
    if r.get("net_cost_gbp") is not None:
        parts.append(f"£{r['net_cost_gbp']:.2f}")
    return "Cosy yesterday: " + " · ".join(parts)


__all__ = ["build_scorecard", "persist_scorecard", "get_scorecards", "evaluate_alerts", "fire_alerts",
           "run_for_day", "backfill_missing", "brief_line"]
