"""Plan per front (#821) — what the battery, the hot-water tank and space
heating will do on a local day, plus the consumption probability, a spend
score and a tariff comparison, composed into ONE viewer-safe read for the Home
page (``GET /api/v1/plan/fronts``).

Every section is built by a small helper and independently guarded: a failing
source leaves ``{"error": "<reason>"}`` in that section and the others intact.
Read-only; the whole response is cached 60 s in-process because the tariff
comparison is slow (seconds).
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

_CACHE_TTL_S = 60.0
_cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}

_KW_THR = 0.05  # kWh/slot threshold in the battery kind rule


# ---------------------------------------------------------------- small utils
def _tz() -> ZoneInfo:
    return ZoneInfo(str(getattr(config, "BULLETPROOF_TIMEZONE", "Europe/London") or "Europe/London"))


def _z(t: datetime) -> str:
    return t.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _hhmm(t: datetime, tz: ZoneInfo) -> str:
    return t.astimezone(tz).strftime("%H:%M")


def _parse(iso: Any) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return t.replace(tzinfo=UTC) if t.tzinfo is None else t.astimezone(UTC)


def _r(v: Any, n: int) -> float | None:
    try:
        return None if v is None else round(float(v), n)
    except (TypeError, ValueError):
        return None


def _guard(fn, *args, **kwargs) -> dict[str, Any]:
    """Run a section builder; never raise."""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 — one failing source must not 500 the page
        logger.warning("plan_fronts: section %s failed", getattr(fn, "__name__", "?"), exc_info=True)
        return {"error": f"{type(exc).__name__}: {exc}"}


def _local_day_bounds(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    a = datetime(day.year, day.month, day.day, tzinfo=tz)
    b = datetime(day.year, day.month, day.day, tzinfo=tz) + timedelta(days=1)
    return a.astimezone(UTC), b.astimezone(UTC)


def lp_slots_for_day(rows: list[dict[str, Any]], day: date, tz: ZoneInfo) -> list[dict[str, Any]]:
    """LP solution rows whose slot STARTS on local ``day``, with a parsed
    ``_start`` (UTC datetime), oldest first."""
    out = []
    for r in rows:
        st = _parse(r.get("slot_time_utc"))
        if st is None or st.astimezone(tz).date() != day:
            continue
        out.append({**r, "_start": st})
    out.sort(key=lambda r: r["_start"])
    return out


# --------------------------------------------------------------------- tariff
def tariff_section(windows: list[Any], structure: str, now_utc: datetime, tz: ZoneInfo) -> dict[str, Any]:
    from ..energy.tariff_structure import display_name

    out = []
    for w in windows:
        status = "done" if now_utc >= w.end_utc else ("ongoing" if now_utc >= w.start_utc else "upcoming")
        out.append({
            "key": w.key, "label": w.label,
            "start_utc": _z(w.start_utc), "end_utc": _z(w.end_utc),
            "start_local": _hhmm(w.start_utc, tz), "end_local": _hhmm(w.end_utc, tz),
            "price_p": _r(w.price_p, 2), "status": status,
        })
    return {"display_name": display_name(), "structure": structure, "windows": out}


# -------------------------------------------------------------------- battery
def normalise_fox_groups(groups: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Fox groups (local clock) → display dicts + minute bounds for coverage
    tests. Both end conventions (``:59`` inclusive, ``:30`` exclusive) are
    handled: a ``:59`` end covers up to the next minute."""
    out = []
    for g in groups or []:
        try:
            extra = g.get("extraParam") or {}
            sh, sm = int(g["startHour"]), int(g["startMinute"])
            eh, em = int(g["endHour"]), int(g["endMinute"])
        except (KeyError, TypeError, ValueError):
            continue
        s_min = sh * 60 + sm
        e_min = eh * 60 + em + (1 if em == 59 else 0)
        out.append({
            "mode": g.get("workMode"),
            "start_local": f"{sh:02d}:{sm:02d}", "end_local": f"{eh:02d}:{em:02d}",
            "min_soc": extra.get("minSocOnGrid", g.get("minSocOnGrid")),
            "fd_soc": extra.get("fdSoc", g.get("fdSoc")),
            "fd_pwr": extra.get("fdPwr", g.get("fdPwr")),
            "max_soc": extra.get("maxSoc", g.get("maxSoc")),
            "_s": s_min, "_e": e_min,
        })
    return out


def fox_mode_at(groups: list[dict[str, Any]], local_dt: datetime) -> str | None:
    m = local_dt.hour * 60 + local_dt.minute
    for g in groups:
        if g["_s"] <= m < g["_e"]:
            return g["mode"]
    return None


def battery_slot_kind(slot: dict[str, Any], fox_mode: str | None) -> str:
    imp = float(slot.get("import_kwh") or 0.0)
    chg = float(slot.get("charge_kwh") or 0.0)
    exp = float(slot.get("export_kwh") or 0.0)
    dis = float(slot.get("discharge_kwh") or 0.0)
    pv_use = float(slot.get("pv_use_kwh") or 0.0)
    if imp > _KW_THR and chg > _KW_THR:
        return "grid_charge"
    if chg > _KW_THR:
        return "pv_charge"
    if exp > pv_use + _KW_THR:
        return "export"
    if fox_mode == "Backup":
        return "hold"
    if dis > _KW_THR:
        return "self_use"
    return "idle"


def battery_windows(slots: list[dict[str, Any]], groups: list[dict[str, Any]],
                    cap_kwh: float, tz: ZoneInfo) -> list[dict[str, Any]]:
    """Contiguous runs of the same ``kind`` over the day's LP slots; ``fox_mode`` is the Fox mode at the window start."""
    def pct(v: Any) -> float | None:
        return None if v is None or cap_kwh <= 0 else round(float(v) / cap_kwh * 100.0, 1)

    runs: list[dict[str, Any]] = []
    for s in slots:
        st = s["_start"]
        mode = fox_mode_at(groups, st.astimezone(tz))
        kind = battery_slot_kind(s, mode)
        end = st + timedelta(minutes=30)
        if runs and runs[-1]["kind"] == kind and runs[-1]["_end"] == st:
            cur = runs[-1]
            cur["_end"] = end
            cur["grid_kwh"] += float(s.get("import_kwh") or 0.0)
            cur["charge_kwh"] += float(s.get("charge_kwh") or 0.0)
            cur["discharge_kwh"] += float(s.get("discharge_kwh") or 0.0)
            cur["soc_end_pct"] = pct(s.get("soc_kwh"))
        else:
            runs.append({
                "kind": kind, "fox_mode": mode, "_start": st, "_end": end,
                "grid_kwh": float(s.get("import_kwh") or 0.0),
                "charge_kwh": float(s.get("charge_kwh") or 0.0),
                "discharge_kwh": float(s.get("discharge_kwh") or 0.0),
                "soc_start_pct": pct(s.get("soc_kwh")), "soc_end_pct": pct(s.get("soc_kwh")),
            })
    out = []
    for c in runs:
        out.append({
            "kind": c["kind"],
            "start_utc": _z(c["_start"]), "end_utc": _z(c["_end"]),
            "start_local": _hhmm(c["_start"], tz), "end_local": _hhmm(c["_end"], tz),
            "grid_kwh": round(c["grid_kwh"], 3), "charge_kwh": round(c["charge_kwh"], 3),
            "discharge_kwh": round(c["discharge_kwh"], 3),
            "soc_start_pct": c["soc_start_pct"], "soc_end_pct": c["soc_end_pct"],
            "fox_mode": c["fox_mode"],
        })
    return out


def battery_by_band(windows: list[Any], planned_by_slot: dict[datetime, float],
                    realised_slots: dict[str, float], lp_slots: list[dict[str, Any]],
                    entry_slot_indices: set[int], cap_kwh: float,
                    now_utc: datetime, tz: ZoneInfo) -> list[dict[str, Any]]:
    """Per tariff window: planned import (committed stitch), realised import
    (until now; null if upcoming), SoC at entry and whether the pessimistic
    charge floor binds at the window's entry slot."""
    out = []
    realised_by_start = {}
    for iso, v in realised_slots.items():
        t = _parse(iso)
        if t is not None:
            realised_by_start[t] = float(v)
    for w in windows:
        planned = sum(v for t, v in planned_by_slot.items() if w.start_utc <= t < w.end_utc)
        has_planned = any(w.start_utc <= t < w.end_utc for t in planned_by_slot)
        realised = None
        if now_utc > w.start_utc:
            seen = [v for t, v in realised_by_start.items()
                    if w.start_utc <= t < w.end_utc and t + timedelta(minutes=30) <= now_utc]
            realised = sum(seen) if seen else None  # no telemetry != 0 kWh
        entry = next((s for s in lp_slots if s["_start"] == w.start_utc), None)
        soc_entry = None
        if entry is not None and entry.get("soc_kwh") is not None and cap_kwh > 0:
            soc_entry = round(float(entry["soc_kwh"]) / cap_kwh * 100.0, 1)
        floored = bool(entry is not None and entry.get("slot_index") in entry_slot_indices)
        out.append({
            "key": w.key, "label": w.label,
            "start_local": _hhmm(w.start_utc, tz), "end_local": _hhmm(w.end_utc, tz),
            "planned_import_kwh": round(planned, 3) if has_planned else None,
            "realised_import_kwh": None if realised is None else round(realised, 3),
            "soc_entry_pct": soc_entry, "floored": floored,
        })
    return out


def _committed_by_start(day: date, field: str, tz: ZoneInfo | None = None) -> dict[datetime, float]:
    """Committed-plan stitch for the LOCAL day: the stitch is keyed by UTC day,
    so read the previous UTC day too and keep slots starting on ``day`` local."""
    tz = tz or _tz()
    out: dict[datetime, float] = {}
    for d in (day - timedelta(days=1), day):
        for iso, v in db.committed_lp_field_by_slot(d, field).items():
            t = _parse(iso)
            if t is not None and t.astimezone(tz).date() == day:
                out[t] = float(v or 0.0)
    return out


def battery_section(day: date, windows: list[Any], now_utc: datetime, tz: ZoneInfo) -> dict[str, Any]:
    cap = float(getattr(config, "BATTERY_CAPACITY_KWH", 0.0) or 0.0)
    out: dict[str, Any] = {
        "capacity_kwh": _r(cap, 2),
        "reserve_pct": _r(getattr(config, "MIN_SOC_RESERVE_PERCENT", None), 1),
        "soc_now_pct": None, "soc_now_kwh": None,
        "plan_run_id": None, "plan_run_at": None,
        "windows": [], "by_band": [], "fox_groups": [], "floor_binding_slots": None,
        "peak_import_planned_kwh": None, "peak_import_realised_kwh": None,
    }
    try:
        soc = db.get_latest_soc_pct()
        if soc is not None:
            out["soc_now_pct"] = round(soc, 1)
            out["soc_now_kwh"] = round(soc / 100.0 * cap, 2)
    except Exception:  # noqa: BLE001
        logger.debug("plan_fronts: soc read failed", exc_info=True)

    groups: list[dict[str, Any]] = []
    try:
        st = db.get_latest_fox_schedule_state()
        groups = normalise_fox_groups((st or {}).get("groups"))
    except Exception:  # noqa: BLE001
        logger.debug("plan_fronts: fox groups read failed", exc_info=True)
    out["fox_groups"] = [{k: v for k, v in g.items() if not k.startswith("_")} for g in groups]

    run_id = db.find_latest_optimizer_run_id()
    lp_slots: list[dict[str, Any]] = []
    entry_idx: set[int] = set()
    if run_id is not None:
        inputs = db.get_lp_inputs(run_id) or {}
        out["plan_run_id"] = run_id
        out["plan_run_at"] = inputs.get("run_at_utc")
        try:
            exo = json.loads(inputs.get("exogenous_snapshot_json") or "{}")
            pcf = exo.get("pess_charge_floor") or {}
            entry_idx = {int(i) for i in (pcf.get("entry_slots") or [])}
            if pcf.get("binding_slots") is not None:
                out["floor_binding_slots"] = int(pcf["binding_slots"])
        except (ValueError, TypeError):
            entry_idx = set()
        lp_slots = lp_slots_for_day(db.get_lp_solution_slots(run_id), day, tz)
    out["windows"] = battery_windows(lp_slots, groups, cap, tz)

    planned = _committed_by_start(day, "import_kwh", tz)
    a, b = _local_day_bounds(day, tz)
    realised = db.half_hourly_kwh_for_utc_range(a, b, "grid_import_kw")
    out["by_band"] = battery_by_band(windows, planned, realised, lp_slots, entry_idx, cap, now_utc, tz)
    peaks = [w for w in windows if str(w.key) == "band_peak" or str(w.label).lower() == "peak"]
    if peaks and (lp_slots or planned):
        pk = [x for x in out["by_band"] if x["key"] in {p.key for p in peaks}]
        out["peak_import_planned_kwh"] = round(sum(x["planned_import_kwh"] or 0.0 for x in pk), 3)
        if any(x["realised_import_kwh"] is not None for x in pk):
            out["peak_import_realised_kwh"] = round(sum(x["realised_import_kwh"] or 0.0 for x in pk), 3)
    return out


# ----------------------------------------------------------------------- tank
_TANK_KIND = {
    "tank_warmup": "warmup", "tank_setback": "setback",
    "tank_negative_boost": "boost", "legionella_cycle": "legionella",
}


def tank_windows(day: date, tz: ZoneInfo) -> list[dict[str, Any]]:
    """Programmed tank rows overlapping local ``day`` (cycles anchored on the
    previous AND the same day), via the same generator as ``/daikin/dhw-schedule``."""
    from .. import dhw_policy

    a, b = _local_day_bounds(day, tz)
    seen: set[tuple[Any, Any]] = set()
    out = []
    for anchor in (day - timedelta(days=1), day):
        for r in dhw_policy.dhw_schedule_rows_for_day(anchor, tz=tz, allow_past=True):
            st, en = _parse(r.get("start_utc")), _parse(r.get("end_utc"))
            if st is None:
                continue
            en = en or st
            if en < a or st >= b:
                continue
            key = (r.get("action_type"), r.get("start_utc"))
            if key in seen:
                continue
            seen.add(key)
            at = str(r.get("action_type") or "")
            out.append({
                "kind": _TANK_KIND.get(at, at.removeprefix("tank_")),
                "start_utc": _z(st), "end_utc": _z(en),
                "start_local": _hhmm(st, tz), "end_local": _hhmm(en, tz),
                "tank_target_c": _r(r.get("tank_temp_c"), 1),
            })
    out.sort(key=lambda w: w["start_utc"])
    return out


def next_tank_action(windows: list[dict[str, Any]], now_utc: datetime,
                     later_windows: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    for w in list(windows) + list(later_windows or []):
        st = _parse(w["start_utc"])
        if st is not None and st > now_utc:
            return {"kind": w["kind"], "start_local": w["start_local"], "tank_target_c": w["tank_target_c"]}
    return None


def _fmt_hour(h: float) -> str:
    h = float(h)
    return f"{int(h) % 24:02d}:{int(round((h - int(h)) * 60)):02d}"


def predicted_tank_at(lp_slots: list[dict[str, Any]], hour: float, tz: ZoneInfo) -> float | None:
    for s in lp_slots:
        lt = s["_start"].astimezone(tz)
        if lt.hour + lt.minute / 60.0 == float(hour):
            return _r(s.get("tank_temp_c"), 1)
    return None


def coast_prediction(t0_c: float, hours: float, params: Any, setback_c: float) -> float:
    from ..dhw.model import coast_to

    return max(float(setback_c), float(coast_to(t0_c, max(0.0, hours), params)))


def predicted_shower_tank(sw: Any, day: date, preset: str, params: Any, tz: ZoneInfo,
                          lp_slots: list[dict[str, Any]]) -> dict[str, Any]:
    """Honest tank temperature at the START of a shower window: coast from the
    warmup target through the setback (the LP slot ``tank_temp_c`` is the phase
    TARGET under the DHW pin — 37 during setback — not a physical prediction).
    Falls back to that LP value only when the decision / params are missing."""
    from .. import dhw_policy

    fallback = {"predicted_tank_c": predicted_tank_at(lp_slots, sw.start_hour, tz),
                "predicted_basis": "lp_slot_target"}
    normal_c = float(config.DHW_TEMP_NORMAL_C)
    setback_c = float(getattr(config, "DHW_TEMP_SETBACK_C", 37.0))
    mode = str(preset or "normal").strip().lower()
    if mode == "guests":  # tank held at normal all day, no coast
        return {"predicted_tank_c": _r(normal_c, 1), "predicted_basis": "guests_held_at_normal"}
    try:
        warm_h = dhw_policy.read_warmup_hour(day)
        evening = float(sw.start_hour) >= warm_h
        d = day if evening else day - timedelta(days=1)
        if mode == "normal" and evening and dhw_policy._legionella_standoff_window_utc(d) is not None:
            return {"predicted_tank_c": 60.0, "predicted_basis": "legionella_cycle"}
        dec = dhw_policy.read_window_decision(d)
        t0 = float(dec.warmup_target_c) if dec.arm == "boost" else normal_c
        hours = float(sw.start_hour) - float(dec.setback_hour_local)
        if not evening and hours < 0:
            hours += 24.0
        if params is None:
            return fallback
        return {"predicted_tank_c": _r(coast_prediction(t0, hours, params, setback_c), 1),
                "predicted_basis": "coast_from_warmup_target"}
    except Exception:  # noqa: BLE001
        logger.debug("plan_fronts: coast prediction failed", exc_info=True)
        return fallback


def tank_section(day: date, now_utc: datetime, tz: ZoneInfo, lp_slots: list[dict[str, Any]]) -> dict[str, Any]:
    from .. import dhw_policy
    from ..dhw.comfort import shower_windows
    from ..dhw.params import resolve_tank_params

    out: dict[str, Any] = {
        "tank_now_c": None, "target_now_c": None, "power_on": None, "telemetry_at_utc": None,
        "model": {}, "decision": {}, "windows": [], "showers": [], "next_action": None,
    }
    tel = db.get_latest_daikin_telemetry(source="live")
    if tel:
        out["tank_now_c"] = _r(tel.get("tank_temp_c"), 1)
        out["target_now_c"] = _r(tel.get("tank_target_c"), 1)
        try:
            out["telemetry_at_utc"] = _z(datetime.fromtimestamp(float(tel["fetched_at"]), UTC))
        except (TypeError, ValueError, KeyError, OSError):
            pass
    try:
        logs = db.get_execution_logs(limit=1)
        if logs and logs[0].get("daikin_tank_power_on") is not None:
            out["power_on"] = bool(logs[0]["daikin_tank_power_on"])
    except Exception:  # noqa: BLE001
        logger.debug("plan_fronts: tank power read failed", exc_info=True)

    st = dhw_policy._tank_model_state()
    out["model"] = {
        "source": st.get("source"), "ua_w_per_k": st.get("ua_w_per_k"), "ambient_c": st.get("ambient_c"),
        "tau_hours": st.get("tau_hours"),
        "coast_measured_c_per_h": _r(st.get("coast_measured_c_per_h"), 3),
        "coast_model_c_per_h": _r(st.get("coast_model_c_per_h"), 3),
        "coast_ratio": _r(st.get("coast_ratio"), 2),
    }
    params = None
    try:
        from ..dhw.params import live_indoor_ambient_c

        params = resolve_tank_params(ambient_c=live_indoor_ambient_c())
    except Exception:  # noqa: BLE001
        logger.debug("plan_fronts: tank params failed", exc_info=True)

    try:
        d = dhw_policy.read_window_decision(day)
        out["decision"] = {
            "arm": d.arm, "warmup_hour": dhw_policy.read_warmup_hour(day),
            "setback_hour": d.setback_hour_local, "warmup_target_c": _r(d.warmup_target_c, 1),
            "peak_entry_hour": d.peak_entry_hour_local,
            "cost_hold_p": _r(d.cost_hold_p, 2), "cost_boost_p": _r(d.cost_boost_p, 2),
        }
    except Exception as exc:  # noqa: BLE001
        out["decision"] = {"error": f"{type(exc).__name__}: {exc}"}

    out["windows"] = tank_windows(day, tz)
    later: list[dict[str, Any]] = []
    if next_tank_action(out["windows"], now_utc) is None:
        try:
            later = tank_windows(day + timedelta(days=1), tz)
        except Exception:  # noqa: BLE001
            later = []
    out["next_action"] = next_tank_action(out["windows"], now_utc, later)
    try:
        preset = str(getattr(config, "OPTIMIZATION_PRESET", "normal") or "normal")
        for sw in shower_windows(preset=preset, p=params):
            out["showers"].append({
                "start_local": _fmt_hour(sw.start_hour), "end_local": _fmt_hour(sw.end_hour),
                "floor_c": _r(sw.floor_c, 1), "label": sw.label,
            } | predicted_shower_tank(sw, day, preset, params, tz, lp_slots))
    except Exception as exc:  # noqa: BLE001
        out["showers_error"] = f"{type(exc).__name__}: {exc}"
    return out


# -------------------------------------------------------------------- heating
def heating_windows(rows: list[dict[str, Any]], tz: ZoneInfo) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        at = r.get("action_type")
        if at not in ("lwt_preheat", "restore"):
            continue
        params = r.get("params") if isinstance(r.get("params"), dict) else {}
        st, en = _parse(r.get("start_time")), _parse(r.get("end_time"))
        if st is None:
            continue
        en = en or st
        off = params.get("lwt_offset")
        if at == "restore":
            kind, source = "restore", None
        else:
            if off is None or float(off) == 0:
                continue
            kind = "boost" if float(off) > 0 else "setback"
            source = "lp" if params.get("lp_optimizer") else "tier"
        out.append({
            "kind": kind, "offset_c": _r(off, 1) if off is not None else 0.0,
            "start_utc": _z(st), "end_utc": _z(en),
            "start_local": _hhmm(st, tz), "end_local": _hhmm(en, tz), "source": source,
        })
    out.sort(key=lambda w: w["start_utc"])
    return out


def _indoor_stats(lp_slots: list[dict[str, Any]], tz: ZoneInfo) -> dict[str, Any]:
    vals = [(s["_start"].astimezone(tz), float(s["indoor_temp_c"]))
            for s in lp_slots if s.get("indoor_temp_c") is not None]

    def at(h: int) -> float | None:
        for lt, v in vals:
            if lt.hour == h and lt.minute == 0:
                return round(v, 1)
        return None

    return {
        "min_c": round(min(v for _, v in vals), 1) if vals else None,
        "max_c": round(max(v for _, v in vals), 1) if vals else None,
        "at_07_c": at(7), "at_16_c": at(16), "at_19_c": at(19), "at_22_c": at(22),
    }


def heating_by_band(windows: list[Any], lp_slots: list[dict[str, Any]],
                    hwins: list[dict[str, Any]], tz: ZoneInfo) -> list[dict[str, Any]]:
    out = []
    for w in windows:
        vs = [float(s["indoor_temp_c"]) for s in lp_slots
              if s.get("indoor_temp_c") is not None and w.start_utc <= s["_start"] < w.end_utc]
        mode = None
        if hwins:
            mode = "neutral"
            for h in hwins:
                hs, he = _parse(h["start_utc"]), _parse(h["end_utc"])
                if h["kind"] in ("boost", "setback") and hs and he and hs < w.end_utc and he > w.start_utc:
                    mode = h["kind"]
                    break
        out.append({
            "key": w.key, "label": w.label,
            "start_local": _hhmm(w.start_utc, tz), "end_local": _hhmm(w.end_utc, tz),
            "indoor_min_c": round(min(vs), 1) if vs else None,
            "indoor_max_c": round(max(vs), 1) if vs else None,
            "offset_mode": mode,
        })
    return out


def _plan_ceiling_c(day: date, tz: ZoneInfo) -> float:
    """Ceiling the committed plan was SOLVED with (the latest ``lwt_learning_log``
    row of the day carries it); effective config value as fallback."""
    try:
        a, b = _local_day_bounds(day, tz)
        rows = db.get_lwt_learning_rows(_z(a), _z(b))
        for r in reversed(rows):
            if r.get("ceiling_c") is not None:
                return float(r["ceiling_c"])
    except Exception:  # noqa: BLE001
        logger.debug("plan_fronts: plan ceiling read failed", exc_info=True)
    from ..scheduler.lwt_coast import effective_w3_ceiling_c
    return effective_w3_ceiling_c()


def heating_section(day: date, windows: list[Any], tz: ZoneInfo, lp_slots: list[dict[str, Any]]) -> dict[str, Any]:
    from ..scheduler.lp_dispatch import space_heating_gate_state

    out: dict[str, Any] = {
        "indoor_now_c": None, "indoor_rooms_c": {}, "indoor_aggregate": None, "outdoor_now_c": None,
        "setpoint_c": _r(getattr(config, "INDOOR_SETPOINT_C", None), 1),
        "night_floor_c": _r(getattr(config, "LP_W3_NIGHT_FLOOR_C", 17.5), 1),
        "peak_coast_delta_c": _r(getattr(config, "LP_W3_PEAK_COAST_DELTA_C", 1.0), 1),
        "ceiling_c": _r(_plan_ceiling_c(day, tz), 1),
        "lwt_source": str(getattr(config, "DAIKIN_LWT_SOURCE", "tier") or "tier"),
        "coast_mode": str(getattr(config, "DAIKIN_LWT_COAST_MODE", "setback") or "setback"),
        "gate": None, "windows": [], "predicted_indoor": _indoor_stats(lp_slots, tz), "by_band": [],
    }
    try:
        ind = db.get_latest_indoor_reading(max_age_minutes=int(getattr(config, "INDOOR_SENSOR_STALE_MINUTES", 30)))
        if ind:
            out["indoor_now_c"] = _r(ind.get("temp_c"), 1)
            rooms = ind.get("rooms_c")
            out["indoor_rooms_c"] = dict(rooms) if isinstance(rooms, dict) else {}
            out["indoor_aggregate"] = ind.get("aggregate") or "mean"
    except Exception:  # noqa: BLE001
        logger.debug("plan_fronts: indoor read failed", exc_info=True)
    try:
        tel = db.get_latest_daikin_telemetry(source="live")
        if tel:
            out["outdoor_now_c"] = _r(tel.get("outdoor_temp_c"), 1)
    except Exception:  # noqa: BLE001
        pass
    try:
        g = space_heating_gate_state()
        diff = g.get("lwt_source_last_diff")
        lp_av = diff.get("lp_available") if isinstance(diff, dict) else None
        out["lwt_source"] = g.get("lwt_source") or out["lwt_source"]
        out["coast_mode"] = g.get("coast_mode") or out["coast_mode"]
        out["gate"] = {
            "preheat_enabled": g.get("preheat_enabled"), "demand_present": g.get("demand_present"),
            "measured_window_kwh": g.get("measured_window_kwh"), "threshold_kwh": g.get("threshold_kwh"),
            "current_outdoor_c": g.get("current_outdoor_c"), "outdoor_cutoff_c": g.get("outdoor_cutoff_c"),
            "positive_offset_suppressed_by_outdoor": g.get("positive_offset_suppressed_by_outdoor"),
            "preheat_suppressed": g.get("preheat_suppressed"),
            "lp_available": lp_av,
        }
    except Exception as exc:  # noqa: BLE001
        out["gate"] = {"error": f"{type(exc).__name__}: {exc}"}
    a, b = _local_day_bounds(day, tz)
    rows: dict[Any, dict[str, Any]] = {}
    for pd in (day - timedelta(days=1), day):  # rows are keyed by the run's plan_date
        for r in db.get_actions_for_plan_date(pd.isoformat(), device="daikin"):
            st, en = _parse(r.get("start_time")), _parse(r.get("end_time"))
            if st is not None and st < b and (en or st) >= a:
                rows[r.get("id") or (pd, r.get("start_time"), r.get("action_type"))] = r
    hw = heating_windows(list(rows.values()), tz)
    out["windows"] = hw
    out["by_band"] = heating_by_band(windows, lp_slots, hw, tz)
    return out


# ---------------------------------------------------------------------- spend
def spend_score(avg_p: float | None, peak_kwh: float | None, ideal_p: float | None,
                above_min_p: float | None, ratio: float, *, banded: bool = True) -> str | None:
    """``ideal`` / ``below`` / ``above`` (or None without data). The peak
    clause only applies on banded tariffs (a dynamic day's "peak" is a
    percentile, not a band the house can avoid entirely)."""
    if avg_p is None or ideal_p is None or above_min_p is None:
        return None
    ideal_max = ideal_p * ratio
    if avg_p <= ideal_max and (not banded or (peak_kwh or 0.0) <= 0.1):
        return "ideal"
    if avg_p <= above_min_p:
        return "below"
    return "above"


_period_cache: dict[tuple, tuple[float, dict[str, Any]]] = {}
_PERIOD_TTL_S = 600.0


def _period(start: date, end: date) -> dict[str, Any]:
    key = (str(config.DB_PATH), start.isoformat(), end.isoformat())
    hit = _period_cache.get(key)
    if hit is not None and time.monotonic() - hit[0] < _PERIOD_TTL_S:
        return hit[1]
    res = _period_uncached(start, end)
    if len(_period_cache) > 64:
        _period_cache.clear()
    _period_cache[key] = (time.monotonic(), res)
    return res


def _period_uncached(start: date, end: date) -> dict[str, Any]:
    from . import pnl

    p = pnl.compute_period_pnl(start, end)
    n = int(p.get("n_days") or 0)
    kwh = float(p.get("import_kwh") or 0.0)
    cost = float(p.get("import_cost_gbp") or 0.0)
    net = float(p.get("realised_net_cost_gbp") or 0.0)
    return {
        "n_days": n, "net_cost_gbp": round(net, 2),
        "per_day_gbp": round(net / n, 2) if n else None,
        "import_kwh": round(kwh, 2),
        "avg_import_p": round(cost * 100.0 / kwh, 2) if kwh > 0 else None,
    }


def day_prices(day: date, windows: list[Any], tz: ZoneInfo) -> dict[datetime, float]:
    """Per-slot import price for the local day: stored rates where present,
    the tariff-window price (band profile) for any slot without a stored rate."""
    code = str(getattr(config, "OCTOPUS_TARIFF_CODE", "") or "")
    rows = db.get_agile_rates_slots_for_local_day(code, day, tz_name=str(tz.key)) if code else []
    out: dict[datetime, float] = {}
    for r in rows:
        t = _parse(r.get("valid_from"))
        if t is not None:
            out[t] = float(r["value_inc_vat"])
    for w in windows:
        t = w.start_utc
        while t < w.end_utc:
            out.setdefault(t, float(w.price_p))
            t += timedelta(minutes=30)
    return out


def spend_section(day: date, windows: list[Any], now_utc: datetime, tz: ZoneInfo) -> dict[str, Any]:
    from ..energy.tariff_structure import detect, is_tou_family
    from . import pnl

    code = str(getattr(config, "OCTOPUS_TARIFF_CODE", "") or "")
    out: dict[str, Any] = {
        "realised_import_kwh": None, "realised_import_cost_gbp": None, "realised_avg_import_p": None,
        "forecast_import_kwh": None, "forecast_avg_import_p": None,
        "peak_import_kwh": None, "ideal_avg_import_p": None, "score": None,
        "score_thresholds": {"ideal_max_p": None, "above_min_p": None},
        "score_basis": None, "period": {},
    }
    # Day prices (per slot start, UTC) and the structure derived from them.
    price_by_start = day_prices(day, windows, tz)
    struct = detect(list(price_by_start.values()), short_ok=is_tou_family(code)) if price_by_start else None

    # Realised (meter-preferring daily PnL).
    try:
        pnl_d = pnl.compute_daily_pnl(day)
        kwh = float(pnl_d.get("import_kwh") or 0.0)
        cost = float(pnl_d.get("import_cost_gbp") or 0.0)
        out["realised_import_kwh"] = round(kwh, 3)
        out["realised_import_cost_gbp"] = round(cost, 2)
        out["realised_avg_import_p"] = round(cost * 100.0 / kwh, 2) if kwh > 0 else None
    except Exception as exc:  # noqa: BLE001
        out["realised_error"] = f"{type(exc).__name__}: {exc}"

    # Forecast (committed stitch × per-slot prices).
    fc = _committed_by_start(day, "import_kwh", tz)
    fk = sum(fc.values())
    mean_price = (sum(price_by_start.values()) / len(price_by_start)) if price_by_start else 0.0
    fcost = sum(v * price_by_start.get(t, mean_price) for t, v in fc.items())  # never price a slot at 0
    if fc:
        out["forecast_import_kwh"] = round(fk, 3)
        out["forecast_avg_import_p"] = round(fcost / fk, 2) if fk > 0 else None

    basis = "realised" if (out["realised_import_kwh"] or 0.0) >= 1.0 else "forecast"
    out["score_basis"] = basis if (out["realised_avg_import_p"] is not None or out["forecast_avg_import_p"] is not None) else None
    avg_p = out["realised_avg_import_p"] if basis == "realised" else out["forecast_avg_import_p"]

    # Peak import under the chosen basis. NB realised peak import is the Fox
    # telemetry roll-up while realised_import_kwh is meter-preferring, so the two
    # can differ slightly.
    banded = bool(struct and struct.is_banded)
    if struct is not None and price_by_start:
        peak_starts = {t for t, p in price_by_start.items() if p >= struct.peak_thr and struct.peak_thr > 0}
        if basis == "realised":
            a, b = _local_day_bounds(day, tz)
            rl = {_parse(k): float(v) for k, v in db.half_hourly_kwh_for_utc_range(a, b, "grid_import_kw").items()}
            out["peak_import_kwh"] = round(sum(v for t, v in rl.items() if t in peak_starts), 3)
        else:
            out["peak_import_kwh"] = round(sum(v for t, v in fc.items() if t in peak_starts), 3)
        if banded and struct.cheap_level is not None:
            ideal, above = float(struct.cheap_level), float(struct.cheap_thr)
        else:
            ideal = float(struct.cheap_thr)  # index q25 (same rule as the LP)
            vs = sorted(price_by_start.values())
            above = vs[len(vs) // 2]  # index median, not interpolated
        ratio = float(getattr(config, "SPEND_SCORE_IDEAL_RATIO", 1.15))
        out["ideal_avg_import_p"] = round(ideal, 2)
        out["score_thresholds"] = {"ideal_max_p": round(ideal * ratio, 2), "above_min_p": round(above, 2)}
        out["score"] = spend_score(avg_p, out["peak_import_kwh"], ideal, above, ratio, banded=banded)

    try:
        week_start = day - timedelta(days=day.weekday())
        out["period"] = {"week": _period(week_start, day), "month": _period(day.replace(day=1), day)}
    except Exception as exc:  # noqa: BLE001
        out["period"] = {"error": f"{type(exc).__name__}: {exc}"}
    return out


# -------------------------------------------------------------------- compare
def compare_rows(result: dict[str, Any]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    tariffs = list(result.get("tariffs") or [])
    cur = next((t for t in tariffs if t.get("is_current")), None)
    cur_net = None if cur is None else float(cur.get("net_pence") or 0.0) / 100.0
    rows = []
    for t in tariffs:
        net = float(t.get("net_pence") or 0.0) / 100.0
        rows.append({
            "product_code": t.get("product_code"), "display_name": t.get("display_name"),
            "net_gbp": round(net, 2), "approximate": bool(t.get("approximate")),
            "is_current": bool(t.get("is_current")),
            "delta_vs_current_gbp": None if cur_net is None else round(net - cur_net, 2),
        })
    current = None if cur is None else {
        "product_code": cur.get("product_code"), "display_name": cur.get("display_name"),
        "net_gbp": round(cur_net or 0.0, 2),
    }
    return current, rows


def compare_section(day: date) -> dict[str, Any]:
    from . import fair_compare
    from ..energy.tariff_structure import display_name

    start = day.replace(day=1)
    res = fair_compare.cached_fair_comparison(start, day, 4)
    current, rows = compare_rows(res)
    return {
        "period": "month",
        "period_start": str(res.get("period_start") or start.isoformat()),
        "period_end": str(res.get("period_end") or day.isoformat()),
        "n_days": int(res.get("n_days") or ((day - start).days + 1)),
        "current": current, "rows": rows,
        "framing": f"Staying on {display_name()} for the contract — comparison is informational",
    }


# ----------------------------------------------------------------------- main
def plan_fronts(day: date | None = None, *, now_utc: datetime | None = None,
                use_cache: bool = True) -> dict[str, Any]:
    from .load_expected import band_windows_for_day, expected_load_by_band

    tz = _tz()
    explicit_now = now_utc is not None
    now_utc = now_utc or datetime.now(UTC)
    day = day or now_utc.astimezone(tz).date()
    use_cache = use_cache and not explicit_now
    ckey = (str(config.DB_PATH), day.isoformat())
    mono = time.monotonic()
    if use_cache:
        hit = _cache.get(ckey)
        if hit is not None and mono - hit[0] < _CACHE_TTL_S:
            return hit[1]

    windows: list[Any] = []
    structure = "unknown"
    try:
        windows, structure = band_windows_for_day(day, tz)
    except Exception:  # noqa: BLE001
        logger.warning("plan_fronts: band windows failed", exc_info=True)

    lp_slots: list[dict[str, Any]] = []
    try:
        rid = db.find_latest_optimizer_run_id()
        if rid is not None:
            lp_slots = lp_slots_for_day(db.get_lp_solution_slots(rid), day, tz)
    except Exception:  # noqa: BLE001
        logger.warning("plan_fronts: LP slots failed", exc_info=True)

    result: dict[str, Any] = {
        "date": day.isoformat(), "now_utc": _z(now_utc),
        "tariff": _guard(tariff_section, windows, structure, now_utc, tz),
        "battery": _guard(battery_section, day, windows, now_utc, tz),
        "tank": _guard(tank_section, day, now_utc, tz, lp_slots),
        "heating": _guard(heating_section, day, windows, tz, lp_slots),
        "consumption": _guard(expected_load_by_band, day, now_utc=now_utc if explicit_now else None,
                               use_cache=use_cache),
        "spend": _guard(spend_section, day, windows, now_utc, tz),
        "compare": _guard(compare_section, day),
    }
    if use_cache:
        if len(_cache) > 32:
            _cache.clear()
        _cache[ckey] = (mono, result)
    return result


def clear_cache() -> None:
    _cache.clear()
