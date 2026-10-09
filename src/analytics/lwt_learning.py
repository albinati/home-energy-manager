"""Nightly LWT learning (#838): fill the REALISED half of ``lwt_learning_log``
for yesterday's local day and estimate the building UA and the pump's k.

* UA from coast windows (device offset < 0 or ~no heating): over a run of
  consecutive coast slots the indoor temperature follows
  ``T(t) - To = (T0 - To) * exp(-UA/C * t)``  =>  ``UA = C * ln((T0-To)/(T1-To)) / dt``
  (C = learned thermal mass kWh/K, UA reported in W/K).
* k per 2 h bucket: the LP pump model is ``kW = k * (LWT - 18)``, so
  ``k = sum(kWh) / sum((lwt - 18) * dt)`` over the bucket's telemetry (median over
  buckets). UA is reported for all coasts AND for local-night coasts only.
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
    cons_rows = db.get_daikin_consumption_2hourly_range(day.isoformat(), day.isoformat())
    # #843: drop the #749-family phantom 1.0-kWh Onecta buckets (the weather curve
    # says the compressor was off) before the learner sees them; same helper as the tau fit.
    try:
        from .thermal_learning import sanitize_phantom_heating
        out_series = sorted(
            (datetime.fromtimestamp(float(r["fetched_at"]), tz=UTC), float(r["outdoor_temp_c"]))
            for lst in tel.values() for r in lst if r.get("outdoor_temp_c") is not None
        )
        cons_rows, _nz = sanitize_phantom_heating(cons_rows, out_series, tz)
    except Exception:
        logger.debug("phantom sanitize skipped", exc_info=True)
    cons = {(r["date"], int(r["bucket_idx"])): (r.get("kwh_heating"), r.get("source"))
            for r in cons_rows}
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
        kb, ksrc = cons.get((day.isoformat(), b), (None, None))
        if kb is not None:
            f["heating_kwh"] = round(float(kb) / max(1, per_bucket.get(b, 4)), 4)
            f["heating_kwh_source"] = str(ksrc) if ksrc is not None else None
        db.update_lwt_learning_realised(_z(st), f)
        n += 1
    return n


def _is_coast(r: dict[str, Any]) -> bool:
    """A coast slot = the pump really stayed (nearly) off: measured heating
    ``None`` or <= COAST_HEATING_KWH_EPS, AND (negative device offset OR measured
    ~0 heating). A negative offset alone is not enough — a slot that drew heat
    while the offset was negative says nothing about passive cooling (M3)."""
    off = r.get("device_offset")
    hk = r.get("heating_kwh")
    if hk is not None and float(hk) > COAST_HEATING_KWH_EPS:
        return False
    if off is not None and float(off) < 0:
        return True
    return hk is not None and float(hk) <= COAST_HEATING_KWH_EPS


def _is_night_slot(r: dict[str, Any], tz: ZoneInfo) -> bool:
    ts = datetime.fromisoformat(r["slot_time_utc"].replace("Z", "+00:00"))
    h = ts.astimezone(tz).hour
    ns = int(getattr(config, "LP_W3_NIGHT_START_HOUR_LOCAL", 22))
    ne = int(getattr(config, "LP_W3_NIGHT_END_HOUR_LOCAL", 7))
    return (h >= ns or h < ne) if ns > ne else (ns <= h < ne)


def estimate_ua_w_per_k(
    rows: list[dict[str, Any]], c_kwh_per_k: float, *, night_only: bool = False,
    tz: ZoneInfo | None = None,
) -> tuple[float | None, int]:
    """UA (W/K) from runs of consecutive coast slots. Returns (ua, n_coast_slots).

    ``night_only``: only slots inside the local night window (22-07, PV ~ 0) —
    daytime solar gain biases the passive-cooling fit low."""
    tz = tz or _tz()
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
        if ok and night_only and not _is_night_slot(r, tz):
            ok = False
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


# ---------------------------------------------------------------------------
# #843 — joint UA / C fit (episode estimator) and cheap-band rise check
# ---------------------------------------------------------------------------
JOINT_WINDOW_DAYS = 14
JOINT_MIN_HEAT_SLOTS = 4        # slot-level diagnostic fit
JOINT_MIN_COAST_SLOTS = 8
JOINT_MIN_HEAT_EPISODES = 5     # episode estimator (the headline)
JOINT_MIN_COAST_BLOCKS = 8
JOINT_HEAT_KWH_MIN = 0.05       # a slot counts as heating above this (kWh electric)
JOINT_TAIL_SLOTS = 4            # coast tail appended to a heating episode (lag recovery)
COAST_LWT_DELTA_MAX_C = 6.0     # a metered-zero bucket whose water ran this far above indoor was NOT a coast
TAU_CONSISTENCY_TOL = 0.25      # |tau_free - coast_tau| / coast_tau above this -> flag
COP_SENSITIVITY_FACTORS = (0.8, 1.2)


def _is_measured(r: dict[str, Any]) -> bool:
    """Only Onecta-metered buckets are a MEASURED heating input. ``telemetry_integral``
    buckets are the weather-curve model (``get_daikin_heating_kw(outdoor) * dt``), not a
    measurement — using them as Q_th would regress the model against itself."""
    return str(r.get("heating_kwh_source") or "").startswith("onecta")


def _default_cop_fn() -> Any:
    """The LP's COP(outdoor): ``config.DAIKIN_COP_CURVE`` interpolated, derated for
    lift like the LP does (``physics.apply_cop_lift_multiplier``, active only when
    ``LP_COP_LIFT_PENALTY_PER_KELVIN > 0``). The supply temperature is the slot's
    curve LWT + written offset (``curve_lwt_c`` + ``offset_written``/``device_offset``),
    else the measured ``lwt_actual_c``. Remaining uncertainty: the lift penalty is the
    LP's assumption, not a measured Daikin map, hence the x0.8/x1.2 sensitivity."""
    from ..config import cop_at_temperature
    from ..physics import apply_cop_lift_multiplier
    curve = config.DAIKIN_COP_CURVE

    def fn(t_out: float, row: dict[str, Any] | None = None) -> float:
        base = max(1.0, cop_at_temperature(curve, float(t_out)))
        pen = float(getattr(config, "LP_COP_LIFT_PENALTY_PER_KELVIN", 0.0))
        if pen <= 0.0 or row is None:
            return base
        lwt = None
        if row.get("curve_lwt_c") is not None:
            off = row.get("offset_written")
            if off is None:
                off = row.get("device_offset")
            lwt = float(row["curve_lwt_c"]) + float(off or 0.0)
        elif row.get("lwt_actual_c") is not None:
            lwt = float(row["lwt_actual_c"])
        if lwt is None:
            return base
        return apply_cop_lift_multiplier(
            base, float(t_out), lwt, penalty_per_k=pen,
            reference_delta_k=float(getattr(config, "LP_COP_LIFT_REFERENCE_DELTA_K", 25.0)),
            min_mult=float(getattr(config, "LP_COP_LIFT_MIN_MULTIPLIER", 0.5)))

    fn.row_aware = True  # type: ignore[attr-defined]
    return fn


def _cop(cop_fn: Any, r: dict[str, Any]) -> float:
    to = float(r["outdoor_real_c"])
    return float(cop_fn(to, r)) if getattr(cop_fn, "row_aware", False) else float(cop_fn(to))


def _scaled_cop(cop_fn: Any, f: float) -> Any:
    def fn(t: float, row: dict[str, Any] | None = None) -> float:
        return f * (float(cop_fn(t, row)) if getattr(cop_fn, "row_aware", False) else float(cop_fn(t)))
    fn.row_aware = getattr(cop_fn, "row_aware", False)  # type: ignore[attr-defined]
    return fn


def _ts(r: dict[str, Any]) -> datetime | None:
    try:
        return datetime.fromisoformat(str(r["slot_time_utc"]).replace("Z", "+00:00"))
    except (ValueError, KeyError):
        return None


def _joint_samples(rows: list[dict[str, Any]], cop_fn: Any) -> list[tuple[float, float, float, bool]]:
    """SLOT-level diagnostic samples (i -> i+1): ``(dT, q_th_kwh, x, is_heat)`` with
    ``x = (T_i - To_i) * dt_h`` so that ``C*dT = Q_th - (UA/1000) * x``. Only slots whose
    heating figure is an Onecta-METERED bucket take part: heating = Onecta > 0.05 kWh/slot,
    coast = Onecta == 0 and device offset <= 0 / absent. Everything else is skipped."""
    dt_h = SLOT_MIN / 60.0
    out: list[tuple[float, float, float, bool]] = []
    for a, b in zip(rows, rows[1:], strict=False):
        ta, tb = _ts(a), _ts(b)
        if ta is None or tb is None or tb - ta != timedelta(minutes=SLOT_MIN):
            continue
        if a.get("indoor_real_c") is None or b.get("indoor_real_c") is None or a.get("outdoor_real_c") is None:
            continue
        if not _is_measured(a) or a.get("heating_kwh") is None:
            continue
        hk = float(a["heating_kwh"])
        if hk > JOINT_HEAT_KWH_MIN:
            is_heat, q = True, hk * _cop(cop_fn, a)
        elif hk <= COAST_HEATING_KWH_EPS and (a.get("device_offset") is None or float(a["device_offset"]) <= 0):
            is_heat, q = False, 0.0
        else:
            continue
        d_t = float(b["indoor_real_c"]) - float(a["indoor_real_c"])
        x = (float(a["indoor_real_c"]) - float(a["outdoor_real_c"])) * dt_h
        out.append((d_t, q, x, is_heat))
    return out


def _solve_joint(samples: list[tuple[float, float, float, bool]], tau_prior_h: float | None) -> dict[str, Any] | None:
    """Slot-level least squares ``dT = a*Q - b*x`` (a = 1/C, b = 1/tau): hand-rolled
    2x2 normal equations (free) or the 1-parameter solve for ``b = 1/tau_prior``."""
    n = len(samples)
    if n < 2:
        return None
    sqq = sum(q * q for _d, q, _x, _h in samples)
    sxx = sum(x * x for _d, _q, x, _h in samples)
    sqx = sum(q * x for _d, q, x, _h in samples)
    sdq = sum(d * q for d, q, _x, _h in samples)
    sdx = sum(d * x for d, _q, x, _h in samples)
    if tau_prior_h is None:
        det = sqq * sxx - sqx * sqx
        if det <= 1e-12 * max(1.0, sqq * sxx):
            return None
        a = (sdq * sxx - sdx * sqx) / det
        b = -((sqq * sdx - sqx * sdq) / det)
    else:
        if sqq <= 1e-12:
            return None
        b = 1.0 / float(tau_prior_h)
        a = (sdq + b * sqx) / sqq
    if a <= 0 or b <= 0:
        return None
    pred = [a * q - b * x for _d, q, x, _h in samples]
    mean_d = sum(d for d, _q, _x, _h in samples) / n
    ss_tot = sum((d - mean_d) ** 2 for d, _q, _x, _h in samples)
    ss_res = sum((d - p) ** 2 for (d, _q, _x, _h), p in zip(samples, pred, strict=True))
    r2 = (1.0 - ss_res / ss_tot) if ss_tot > 1e-12 else None
    c_k = 1.0 / a
    return {"ua_w_per_k": round(b * c_k * 1000.0, 1), "c_kwh_per_k": round(c_k, 2),
            "tau_h": round(1.0 / b, 1), "r2": None if r2 is None else round(r2, 3)}


def _coast_tau(samples: list[tuple[float, float, float, bool]]) -> float | None:
    """tau from coast pairs alone: ``dT = -(1/tau) * x``."""
    co = [(d, x) for d, _q, x, h in samples if not h]
    sxx = sum(x * x for _d, x in co)
    if len(co) < 2 or sxx <= 1e-12:
        return None
    inv = -sum(d * x for d, x in co) / sxx
    return round(1.0 / inv, 1) if inv > 0 else None


# -- episode estimator ------------------------------------------------------

# one sample: (dT_in, sum_q_th, sum_x, sum_dt_h, is_heat_episode, var_of_sum_q_th)
_EpSample = tuple[float, float, float, float, bool, float]


def _inverse(m: list[list[float]]) -> list[list[float]] | None:
    """Gauss-Jordan inverse of a small matrix; None when (near) singular."""
    n = len(m)
    a = [row[:] + [1.0 if i == j else 0.0 for j in range(n)] for i, row in enumerate(m)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(a[r][c]))
        if abs(a[p][c]) < 1e-10:
            return None
        a[c], a[p] = a[p], a[c]
        piv = a[c][c]
        a[c] = [v / piv for v in a[c]]
        for r in range(n):
            if r != c:
                f = a[r][c]
                if f:
                    a[r] = [v - f * w for v, w in zip(a[r], a[c], strict=True)]
    return [row[n:] for row in a]


def _ols(feats: list[list[float]], y: list[float], q_noise_var: float = 0.0,
         ) -> tuple[list[float], list[list[float]], float] | None:
    """OLS ``y = F beta``: returns ``(beta, cov = sigma^2 (F'F)^-1, ssr)``; columns are
    rms-normalised before inversion so the conditioning check is scale-free.

    ``q_noise_var`` = total KNOWN measurement-error variance of column 0 (the heat input,
    summed over samples). The whole-kWh Onecta counter quantises every 2 h bucket
    (uniform error, variance 1/12 kWh^2 x COP^2), which attenuates the heat coefficient
    (errors-in-variables) and inflates C; the method-of-moments correction subtracts it
    from ``F'F`` (consistent for independent noise; the SEs ignore the extra variance of
    the correction and are therefore slightly optimistic)."""
    n, k = len(y), len(feats[0])
    if n <= k:
        return None
    sc = [math.sqrt(sum(f[j] ** 2 for f in feats) / n) for j in range(k)]
    if any(s <= 1e-12 for s in sc):
        return None
    z = [[f[j] / sc[j] for j in range(k)] for f in feats]
    ztz = [[sum(r[i] * r[j] for r in z) for j in range(k)] for i in range(k)]
    if q_noise_var > 0:
        ztz[0][0] -= q_noise_var / (sc[0] ** 2)
        if ztz[0][0] <= 1e-6 * n:
            return None
    inv = _inverse(ztz)
    if inv is None:
        return None
    zty = [sum(r[i] * yy for r, yy in zip(z, y, strict=True)) for i in range(k)]
    g = [sum(inv[i][j] * zty[j] for j in range(k)) for i in range(k)]
    ssr = sum((yy - sum(r[j] * g[j] for j in range(k))) ** 2 for r, yy in zip(z, y, strict=True))
    s2 = ssr / (n - k)
    beta = [g[j] / sc[j] for j in range(k)]
    cov = [[s2 * inv[i][j] / (sc[i] * sc[j]) for j in range(k)] for i in range(k)]
    return beta, cov, ssr


def _episode_samples(rows: list[dict[str, Any]], cop_fn: Any, tz: ZoneInfo) -> tuple[list[_EpSample], int]:
    """One sample per contiguous Onecta-heating EPISODE (+ a coast tail) and one per
    Onecta-zero 2 h coast block. Returns ``(samples, n_measured_slots)``."""
    dt_h = SLOT_MIN / 60.0
    step = timedelta(minutes=SLOT_MIN)
    by: dict[datetime, dict[str, Any]] = {}
    for r in rows:
        t = _ts(r)
        if t is not None:
            by[t] = r
    n_measured = sum(1 for r in by.values() if _is_measured(r) and r.get("heating_kwh") is not None)
    # local 2 h buckets, each a run of 4 contiguous measured slots
    buckets: dict[tuple[Any, int], list[datetime]] = {}
    for t in sorted(by):
        loc = t.astimezone(tz)
        buckets.setdefault((loc.date(), loc.hour // 2), []).append(t)
    kinds: list[tuple[datetime, list[datetime], str]] = []   # (start, slots, heat|coast|other)
    for slots in buckets.values():
        ok = (len(slots) == 4 and all(slots[i + 1] - slots[i] == step for i in range(3))
              and all(_is_measured(by[t]) and by[t].get("heating_kwh") is not None for t in slots))
        kind = "other"
        if ok:
            hk = [float(by[t]["heating_kwh"]) for t in slots]
            if sum(hk) / 4.0 > JOINT_HEAT_KWH_MIN:
                kind = "heat"
            elif all(h <= COAST_HEATING_KWH_EPS for h in hk) and all(
                    by[t].get("device_offset") is None or float(by[t]["device_offset"]) <= 0 for t in slots):
                kind = "coast"
                # whole-kWh counter: a sub-0.5 kWh run reads 0. If the live water temperature
                # says the compressor ran, this is hidden heat, not a coast -> unusable.
                for t in slots:
                    r_ = by[t]
                    if (r_.get("lwt_actual_c") is not None and r_.get("indoor_real_c") is not None
                            and float(r_["lwt_actual_c"]) - float(r_["indoor_real_c"]) > COAST_LWT_DELTA_MAX_C):
                        kind = "other"
                        break
        kinds.append((slots[0], slots, kind))
    kinds.sort(key=lambda k: k[0])

    def window(slots: list[datetime], heat_slots: int, is_heat: bool) -> _EpSample | None:
        end = by.get(slots[-1] + step)
        if end is None or end.get("indoor_real_c") is None:
            return None
        rs = [by.get(t) for t in slots]
        if any(r is None or r.get("indoor_real_c") is None or r.get("outdoor_real_c") is None for r in rs):
            return None
        d_t = float(end["indoor_real_c"]) - float(rs[0]["indoor_real_c"])  # type: ignore[index]
        q = sum(float(r["heating_kwh"]) * _cop(cop_fn, r) for r in rs[:heat_slots])  # type: ignore[index]
        x = sum((float(r["indoor_real_c"]) - float(r["outdoor_real_c"])) * dt_h for r in rs)  # type: ignore[index]
        # quantisation of the whole-kWh counter: uniform error, var 1/12 kWh_el^2 per metered
        # bucket, scaled to thermal by the mean COP of the window's heating slots.
        if heat_slots:
            cops = [_cop(cop_fn, r) for r in rs[:heat_slots]]  # type: ignore[arg-type]
            qvar = (heat_slots / 4.0) / 12.0 * (sum(cops) / len(cops)) ** 2
        else:
            qvar = 0.0
        return d_t, q, x, len(rs) * dt_h, is_heat, qvar

    out: list[_EpSample] = []
    used_tail: set[datetime] = set()
    i = 0
    while i < len(kinds):
        if kinds[i][2] != "heat":
            i += 1
            continue
        j = i
        slots = list(kinds[i][1])
        while (j + 1 < len(kinds) and kinds[j + 1][2] == "heat"
               and kinds[j + 1][1][0] - slots[-1] == step):
            j += 1
            slots += kinds[j][1]
        n_heat_slots = len(slots)
        # an adjacent unusable bucket may hold unmetered heat (lag spills into / out of it)
        bad_before = (i > 0 and kinds[i - 1][2] == "other" and slots[0] - kinds[i - 1][1][-1] == step)
        bad_after = (j + 1 < len(kinds) and kinds[j + 1][2] == "other"
                     and kinds[j + 1][1][0] - slots[-1] == step)
        if bad_before or bad_after:
            i = j + 1
            continue
        if (j + 1 < len(kinds) and kinds[j + 1][2] == "coast" and kinds[j + 1][1][0] - slots[-1] == step):
            tail = kinds[j + 1][1][:JOINT_TAIL_SLOTS]
            slots += tail
            used_tail.add(kinds[j + 1][0])
        s = window(slots, n_heat_slots, True)
        if s is not None:
            out.append(s)
        i = j + 1
    for start, slots, kind in kinds:
        if kind == "coast" and start not in used_tail and _bucket_in_night(start, tz):
            s = window(slots, 0, False)
            if s is not None:
                out.append(s)
    return out, n_measured


def _bucket_in_night(start: datetime, tz: ZoneInfo) -> bool:
    """A 2 h bucket lying wholly inside the local night window (22-07): no solar gain,
    so the only unmodelled input is the (roughly constant) internal gain ``g``. Daytime
    coast blocks carry a diurnal solar term a constant intercept cannot absorb; on
    simulated data they biased UA by ~+20 %."""
    ns = int(getattr(config, "LP_W3_NIGHT_START_HOUR_LOCAL", 22))
    ne = int(getattr(config, "LP_W3_NIGHT_END_HOUR_LOCAL", 7))
    h = start.astimezone(tz).hour
    return h >= ns or h + 2 <= ne


def _fit_episodes(samples: list[_EpSample], tau_prior_h: float | None = None) -> dict[str, Any] | None:
    """``dT = a*sumQ - b*sumX + g*sumdt`` (a=1/C, b=UA/(1000 C)=1/tau, g=gain_kW/C). Free (3
    params, SEs by the delta method) or, with ``tau_prior_h``, b FIXED = 1/tau (2 params)."""
    y = [s[0] for s in samples]
    if tau_prior_h is None:
        res = _ols([[s[1], -s[2], s[3]] for s in samples], y, sum(s[5] for s in samples))
        if res is None:
            return None
        (a, b, g), cov, ssr = res
        if a <= 0 or b <= 0:
            return {"nonphysical": True}
        va, vb, cab = cov[0][0], cov[1][1], cov[0][1]
        ua = 1000.0 * b / a
        # d(ua)/da = -1000 b/a^2 ; d(ua)/db = 1000/a
        var_ua = (1000 * b / a ** 2) ** 2 * va + (1000 / a) ** 2 * vb - 2 * (1000 * b / a ** 2) * (1000 / a) * cab
        var_gain = (g / a ** 2) ** 2 * va + (1 / a) ** 2 * cov[2][2] - 2 * (g / a ** 2) * (1 / a) * cov[0][2]
        return {"ua_w_per_k": ua, "ua_se": math.sqrt(max(0.0, var_ua)), "c_kwh_per_k": 1.0 / a,
                "c_se": math.sqrt(max(0.0, va)) / a ** 2, "tau_h": 1.0 / b, "gain_kw": g / a,
                "gain_se": math.sqrt(max(0.0, var_gain)), "resid_rms_c": math.sqrt(ssr / (len(y) - 3))}
    b0 = 1.0 / float(tau_prior_h)
    res = _ols([[s[1], s[3]] for s in samples], [yy + b0 * s[2] for yy, s in zip(y, samples, strict=True)],
               sum(s[5] for s in samples))
    if res is None:
        return None
    (a, g), cov, ssr = res
    if a <= 0:
        return {"nonphysical": True}
    return {"ua_w_per_k": 1000.0 * b0 / a, "c_kwh_per_k": 1.0 / a, "tau_h": float(tau_prior_h),
            "c_se": math.sqrt(max(0.0, cov[0][0])) / a ** 2, "gain_kw": g / a,
            "resid_rms_c": math.sqrt(ssr / (len(y) - 2))}


def _rnd(d: dict[str, Any] | None, nd: dict[str, int]) -> dict[str, Any] | None:
    if d is None or d.get("nonphysical"):
        return None
    return {k: (round(v, nd.get(k, 2)) if isinstance(v, float) else v) for k, v in d.items()}


def night_rise_per_band(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """For each contiguous run of ``cheap``-band slots with a POSITIVE written offset:
    measured indoor rise vs the plan's predicted rise — the direct check on whether the
    thermal model is pessimistic. Plan-consistent: the predicted rise comes only from the
    slots that share the FIRST both-readings slot's ``plan_updated_at_utc`` token (a run
    re-planned mid-band mixes trajectories that start from different measured states);
    ``n_plans`` / ``mixed_plans`` flag such runs. Only slots carrying BOTH a real and a
    predicted reading count for first/last; ``n_slots`` is that measured span and
    ``end_utc`` = last such slot start + 30 min."""
    out: list[dict[str, Any]] = []
    run: list[dict[str, Any]] = []

    def flush() -> None:
        both = [r for r in run if r.get("indoor_real_c") is not None and r.get("indoor_pred_c") is not None]
        if len(both) >= 2:
            tok = both[0].get("plan_updated_at_utc")
            same = [r for r in both if r.get("plan_updated_at_utc") == tok]
            n_plans = len({r.get("plan_updated_at_utc") for r in run})
            m = float(both[-1]["indoor_real_c"]) - float(both[0]["indoor_real_c"])
            p = (float(same[-1]["indoor_pred_c"]) - float(same[0]["indoor_pred_c"])) if len(same) >= 2 else None
            offs = [float(r["offset_written"]) for r in run]
            t0, t1 = _ts(both[0]), _ts(both[-1])
            span = int((t1 - t0).total_seconds() // (SLOT_MIN * 60)) + 1 if t0 and t1 else len(both)
            last_start = both[-1]["slot_time_utc"]
            end_utc = _z(t1 + timedelta(minutes=SLOT_MIN)) if t1 else last_start
            out.append({
                "start_utc": both[0]["slot_time_utc"], "end_utc": end_utc,
                "n_slots": span, "mean_offset_c": round(sum(offs) / len(offs), 2),
                "measured_rise_c": round(m, 2),
                "predicted_rise_c": None if p is None else round(p, 2),
                "model_error_c": None if p is None else round(p - m, 2),
                "plan_token": tok, "n_plans": n_plans, "mixed_plans": n_plans > 1,
            })
        run.clear()

    prev: datetime | None = None
    for r in rows:
        ts = _ts(r)
        ok = (ts is not None and str(r.get("price_band") or "") == "cheap"
              and r.get("offset_written") is not None and float(r["offset_written"]) > 0)
        if ok and run and (prev is None or ts - prev != timedelta(minutes=SLOT_MIN)):
            flush()
        if ok:
            run.append(r)
        else:
            flush()
        prev = ts
    flush()
    return out


def fit_ua_c_joint(
    rows: list[dict[str, Any]], *, cop_fn: Any = None, tau_prior_h: float | None = None,
    tz: ZoneInfo | None = None,
) -> dict[str, Any]:
    """UA and C from Onecta-METERED heating + coast, episode estimator (#843).

    Samples (never the slots themselves): one per contiguous Onecta-heating episode plus a
    4-slot coast tail (the emitters/slab keep delivering heat after the compressor stops),
    one per 2 h Onecta-zero coast block lying wholly in the local night (no solar gain). Model ``dT = a*sumQ - b*sum((T-To)dt) + g*sum dt``
    with Q = COP(To) x metered kWh; a = 1/C, b = UA/(1000C) = 1/tau, g = internal+solar
    gain / C. Standard errors from sigma^2 (X'X)^-1 (delta method for UA, C). Identifiable
    only with >= 5 heating episodes AND >= 8 coast blocks AND a, b > 0, else
    ``identifiable: False`` + ``reason``. ``telemetry_integral`` buckets are the weather-curve
    model, not a measurement, and are excluded. ``tau_fixed`` is the HARD constraint b = 1/tau_prior;
    ``slot_fit`` is the per-slot regression (diagnostic only; biased by quantised, lagged
    heat). ``consistency_flag`` fires when the free tau disagrees with the coast-only tau by
    > 25 % (lag or gain contamination). Analytics only: nothing here is applied."""
    cop_fn = cop_fn or _default_cop_fn()
    tz = tz or _tz()
    ep, n_measured = _episode_samples(rows, cop_fn, tz)
    n_he = sum(1 for s in ep if s[4])
    n_cb = len(ep) - n_he
    slot = _joint_samples(rows, cop_fn)
    n_sh = sum(1 for s in slot if s[3])
    n_sc = len(slot) - n_sh
    coast_pairs = [(s[0], 0.0, s[2], False) for s in ep if not s[4]]
    slot_fit = None
    if n_sh >= JOINT_MIN_HEAT_SLOTS and n_sc >= JOINT_MIN_COAST_SLOTS:
        slot_fit = _solve_joint(slot, None)
        if slot_fit is not None:
            slot_fit.update(n_heat=n_sh, n_coast=n_sc)
    res: dict[str, Any] = {
        "identifiable": False, "reason": None, "n_heat_episodes": n_he, "n_coast_blocks": n_cb,
        "tau_prior_h": tau_prior_h, "ua_w_per_k": None, "ua_se": None, "c_kwh_per_k": None,
        "c_se": None, "tau_h": None, "gain_kw": None, "resid_rms_c": None,
        "coast_tau_h": _coast_tau(coast_pairs) if coast_pairs else _coast_tau(slot),
        "tau_fixed": None, "consistency_flag": None, "cop_sensitivity": None, "slot_fit": slot_fit,
    }
    if n_measured == 0:
        res["reason"] = "no_measured_input"
        return res
    if n_he < JOINT_MIN_HEAT_EPISODES:
        res["reason"] = "too_few_heat_episodes"
        return res
    if n_cb < JOINT_MIN_COAST_BLOCKS:
        res["reason"] = "too_few_coast_blocks"
        return res
    free = _fit_episodes(ep, None)
    if free is None:
        res["reason"] = "singular"
        return res
    if free.get("nonphysical"):
        res["reason"] = "nonphysical_fit"
        return res
    r = _rnd(free, {"ua_w_per_k": 1, "ua_se": 1, "c_kwh_per_k": 2, "c_se": 2, "tau_h": 1,
                    "gain_kw": 3, "gain_se": 3, "resid_rms_c": 3})
    assert r is not None
    res.update(r)
    res["identifiable"] = True
    ct = res["coast_tau_h"]
    if ct and abs(res["tau_h"] - ct) / ct > TAU_CONSISTENCY_TOL:
        res["consistency_flag"] = "lag_or_gain_contamination_suspected"
    if tau_prior_h:
        res["tau_fixed"] = _rnd(_fit_episodes(ep, tau_prior_h),
                                {"ua_w_per_k": 1, "c_kwh_per_k": 2, "c_se": 2, "tau_h": 1,
                                 "gain_kw": 3, "resid_rms_c": 3})
    sens: dict[str, Any] = {}
    for f in COP_SENSITIVITY_FACTORS:
        ep_f, _n = _episode_samples(rows, _scaled_cop(cop_fn, f), tz)
        alt = _fit_episodes(ep_f, None)
        sens[f"x{f}"] = (None if alt is None or alt.get("nonphysical") else
                         {"ua_w_per_k": round(alt["ua_w_per_k"], 1),
                          "c_kwh_per_k": round(alt["c_kwh_per_k"], 2), "tau_h": round(alt["tau_h"], 1)})
    res["cop_sensitivity"] = sens
    return res


K_MIN_SAMPLES = 3        # telemetry samples needed in a 2 h bucket
K_MIN_LWT_C = 20.0       # ignore samples with the water barely above idle
K_MIN_BUCKET_KWH = 0.05  # bucket must really have heated
K_MAX_DT_H = 0.5         # a telemetry gap longer than this is not integrated over


def estimate_k_kw_per_c(buckets: list[dict[str, Any]]) -> tuple[float | None, int]:
    """Pump ``k`` (kW per degC of ``LWT - 18``) at 2-HOUR BUCKET granularity.

    The heating kWh counter is quantised and only resolved per 2 h bucket, so a
    per-slot ratio is noise (M4). Per bucket: ``k = sum(kWh) / sum_i((lwt_i - 18) * dt_i)``
    over the bucket's own telemetry samples (``lwt_actual > K_MIN_LWT_C``, >= K_MIN_SAMPLES
    samples, dt = time to the next sample capped at K_MAX_DT_H). Returns
    (median k, n buckets used). ``buckets`` = ``[{"kwh": float, "samples": [(epoch_s, lwt_c), ...]}]``."""
    ks: list[float] = []
    for b in buckets:
        kwh = b.get("kwh")
        if kwh is None or float(kwh) < K_MIN_BUCKET_KWH:
            continue
        samples = sorted((float(t), float(l)) for t, l in b.get("samples", []) if l is not None)
        if len(samples) < K_MIN_SAMPLES:
            continue
        gaps = [(samples[i + 1][0] - samples[i][0]) / 3600.0 for i in range(len(samples) - 1)]
        last_dt = gaps[-1] if gaps else K_MAX_DT_H
        denom = 0.0
        n_ok = 0
        for i, (_t, lwt) in enumerate(samples):
            dt = min(K_MAX_DT_H, gaps[i] if i < len(gaps) else last_dt)
            if lwt > K_MIN_LWT_C and dt > 0:
                denom += (lwt - 18.0) * dt
                n_ok += 1
        if n_ok < K_MIN_SAMPLES or denom <= 0:
            continue
        ks.append(float(kwh) / denom)
    return (round(float(median(ks)), 4) if ks else None), len(ks)


def k_buckets_for_day(day: date, tz: ZoneInfo) -> list[dict[str, Any]]:
    """Per local 2 h bucket: measured heating kWh + the raw telemetry samples."""
    slots = day_slots_utc(day, tz)
    if not slots:
        return []
    start, end = slots[0], slots[-1] + timedelta(minutes=SLOT_MIN)
    cons = {int(r["bucket_idx"]): r.get("kwh_heating")
            for r in db.get_daikin_consumption_2hourly_range(day.isoformat(), day.isoformat())}
    samples: dict[int, list[tuple[float, float]]] = {}
    for r in db.get_daikin_telemetry_range(start.timestamp(), end.timestamp()):
        if r.get("lwt_actual_c") is None:
            continue
        ts = float(r["fetched_at"])
        b = datetime.fromtimestamp(ts, tz=UTC).astimezone(tz).hour // 2
        samples.setdefault(b, []).append((ts, float(r["lwt_actual_c"])))
    return [{"bucket": b, "kwh": cons.get(b), "samples": samples.get(b, [])} for b in sorted(cons)]


def pump_off_delta(rows: list[dict[str, Any]]) -> dict[str, float | None]:
    """Realised ``lwt_actual - indoor`` where heating stayed ~0 (pump off) vs
    where it ran — the data to fit the real ``DAIKIN_LWT_COAST_DELTA_C``."""
    off, on = [], []
    for r in rows:
        if r.get("lwt_actual_c") is None or r.get("indoor_real_c") is None or r.get("heating_kwh") is None:
            continue
        d = float(r["lwt_actual_c"]) - float(r["indoor_real_c"])
        (off if float(r["heating_kwh"]) <= COAST_HEATING_KWH_EPS else on).append(d)

    def q(v: list[float], p: float) -> float | None:
        if not v:
            return None
        v = sorted(v)
        return round(v[min(len(v) - 1, max(0, int(math.ceil(p * len(v))) - 1))], 2)

    return {"pump_off_delta_median_c": q(off, 0.5), "pump_off_delta_max_c": q(off, 1.0),
            "pump_off_n": len(off), "pump_on_delta_p10_c": q(on, 0.1), "pump_on_n": len(on)}


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
    ua, n_coast = estimate_ua_w_per_k(rows, c, tz=tz)
    ua_night, n_coast_night = estimate_ua_w_per_k(rows, c, night_only=True, tz=tz)
    n_heat = sum(1 for r in rows if r.get("heating_kwh") is not None
                 and float(r["heating_kwh"]) > COAST_HEATING_KWH_EPS)
    k, n_k_buckets = estimate_k_kw_per_c(k_buckets_for_day(day, tz))
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
    joint = _joint_for_day(day, tz)
    row = {
        "date": day.isoformat(), "n_coast_slots": n_coast, "n_heat_slots": n_heat,
        "ua_est_w_per_k": ua, "k_est_kw_per_c": k,
        "pred_err_mean_c": pm, "pred_err_p90_c": p90,
        "ua_est_night_w_per_k": ua_night,
        "payload": {"c_kwh_per_k": c, "ua_est_night_w_per_k": ua_night,
                    # #843: the coast-only UA is C x decay rate with C = tau x UA_pin -> circular
                    "ua_from_tau_scaled_w_per_k": ua, "ua_from_tau_scaled_night_w_per_k": ua_night,
                    "ua_est_circular": True, **joint,
                    "n_coast_night_slots": n_coast_night, "n_k_buckets": n_k_buckets, "ua_pinned_w_per_k": ua_pin, "k_pinned_kw_per_c": k_pin,
                    "n_rows": len(rows),
                    "coast_delta_configured_c": float(getattr(config, "DAIKIN_LWT_COAST_DELTA_C", 2.0)),
                    **pump_off_delta(rows)},
    }
    db.upsert_lwt_learning_daily(row)
    try:
        db.log_action(device="system", action="lwt_learning_summary",
                      params={k_: v for k_, v in row.items()}, result="ok", trigger="cron")
    except Exception:
        logger.debug("lwt_learning_summary log failed", exc_info=True)
    return row


def _joint_for_day(day: date, tz: ZoneInfo) -> dict[str, Any]:
    """#843 payload keys: rolling-window joint fit + cheap-band rise table. Never raises."""
    try:
        start = day_slots_utc(day - timedelta(days=JOINT_WINDOW_DAYS - 1), tz)[0]
        end = day_slots_utc(day, tz)[-1] + timedelta(minutes=SLOT_MIN)
        rows = db.get_lwt_learning_rows(_z(start), _z(end))
        try:
            from .thermal_learning import get_building_tau_hours
            tau = float(get_building_tau_hours())
        except Exception:
            tau = None
        fit = fit_ua_c_joint(rows, tau_prior_h=tau, tz=tz)
        day_start, day_end = _z(day_slots_utc(day, tz)[0]), _z(end)
        day_rows = [r for r in rows if day_start <= r["slot_time_utc"] < day_end]
        return {"joint_fit": fit, "joint_window_days": JOINT_WINDOW_DAYS,
                "night_rise_per_band": night_rise_per_band(day_rows)}
    except Exception:
        logger.warning("lwt_learning joint fit failed", exc_info=True)
        return {"joint_fit": None, "joint_window_days": JOINT_WINDOW_DAYS, "night_rise_per_band": []}
