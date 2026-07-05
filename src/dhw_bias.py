"""OPEN-LOOP DHW bucket-bias corrector (the DHW analog of load_bias).

The pinned DHW forecast (``dhw_policy.forecast_dhw_load_per_slot``) has the
LEVEL corrected by the trailing auto-scale (#534) but carries a per-2h-bucket
SHAPE error: prod ``dhw_error_log`` shows the daytime warmup window
over-forecast ~3-4x in summer while the 13:00 warmup-transition bucket has been
observed ~4x UNDER. The shape error is partly the tank's real warmup/decay
inertia spilling across buckets — the bucket factors absorb that timing error
empirically, without a physics rewrite.

This module measures the per-LOCAL-2h-bucket ratio from ``dhw_error_log`` and
produces a MULTIPLICATIVE correction per bucket. Design decisions, each earned
the hard way (the first cut used the PV/load damped-accumulation pattern and
died in adversarial review — saturating clamps while disabled, limit-cycling
while enabled):

* **Open-loop estimation, no accumulation.** Each error-log row records the
  ``applied_factor`` that was in force when its forecast was committed (1.0
  while disabled). Learning de-biases first — ``raw = forecast /
  applied_factor`` — and estimates ``factor = Σw·actual / Σw·raw`` directly.
  There is NO ``prev ×`` update, so nothing compounds night over night: the
  estimator converges to the true ratio at the recency-window speed whether
  the correction is applied or not, and the backtest evaluates exactly the
  object production would apply.
* **Ratio-of-sums, not mean-of-ratios** — DHW bucket energies are small
  (0.03-0.45 kWh) so per-row ratios explode on tiny denominators; the
  recency-weighted Σactual/Σraw is robust (same lesson as the PV calibration
  median→ratio-of-sums switch).
* **Asymmetric filter** — rows need a RAW forecast above a floor (ratio
  denominator; raw, not committed, so a shrunk bucket can't starve its own
  learning) but low/zero ``actual_kwh`` rows are KEPT: actual≈0 against a
  real forecast is exactly the daytime over-forecast signal. NULL actuals
  (missing Daikin split) are dropped — the rebuild no longer coerces them
  to 0.
* **Normal-mode only.** Rows are stamped with the mode at rebuild; learning
  uses normal-mode rows and application happens only when ``mode ==
  "normal"``. Guests is comfort-critical (morning showers a normal-summer
  factor would shrink) and vacation forecasts ~0 — both stay at the raw
  schedule.

The stored factors are shape-RAW. Total-preservation is enforced at
APPLICATION time in ``forecast_dhw_load_per_slot``: the factors are divided by
the energy-share-weighted mean over the nominal bucket shares, so the forecast
daily total is unchanged and the level stays the auto-scale's job. Required,
not cosmetic: the auto-scale is open-loop w.r.t. the committed forecast
(measured daily actual ÷ fixed nominal constants) and would never back off if
this corrector also moved the level.

Known honest limitation: Onecta 2h buckets are integer kWh, so sub-1 kWh
buckets can truncate to measured 0 — a systematic down-pull on small buckets,
bounded by the clamp floor and partially cancelled by the total-preserving
normalization. The telemetry-integral refinement (#425) improves the actuals
when heartbeat data exists.

DEFAULT OFF: the table refreshes nightly (cheap, observable, and — being
open-loop — stable while disabled) but the forecast only APPLIES it when
``DHW_BUCKET_BIAS_ENABLED``; the corrected value feeds a hard LP equality
(K2 pin), so enabling is gated on the backtest endpoint. Factors older than
``DHW_BUCKET_BIAS_MAX_AGE_DAYS`` are treated as absent at application.
"""
from __future__ import annotations

import logging
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .config import config

logger = logging.getLogger(__name__)

_N_BUCKETS = 12  # local 2h buckets, matching dhw_error_log / daikin_consumption_2hourly


def _tz() -> ZoneInfo:
    return ZoneInfo(getattr(config, "BULLETPROOF_TIMEZONE", "Europe/London"))


def _contaminated_day_buckets(start_day: date, end_day: date) -> set[tuple[str, int]]:
    """(day_iso, bucket_idx) pairs whose measured DHW energy is NOT the
    schedule's doing — negative-price boosts (deliberate max-heating the
    forecast budgets separately), user manual tank overrides, and the
    firmware's weekly legionella cycle (never in the forecast at all).
    Learning from any of them would poison ordinary buckets' factors."""
    from . import db as _db

    tz = _tz()
    out: set[tuple[str, int]] = set()

    def _mark(window_start_utc: datetime, window_end_utc: datetime) -> None:
        cur = window_start_utc
        while cur < window_end_utc:
            loc = cur.astimezone(tz)
            out.add((loc.date().isoformat(), loc.hour // 2))
            cur += timedelta(hours=1)  # bucket granularity is 2h; 1h step can't skip one
        loc_end = (window_end_utc - timedelta(seconds=1)).astimezone(tz)
        out.add((loc_end.date().isoformat(), loc_end.hour // 2))

    try:
        for s, e in _db.get_dhw_boost_windows(start_day.isoformat(), end_day.isoformat()):
            try:
                ws = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
                we = datetime.fromisoformat(str(e).replace("Z", "+00:00"))
            except (ValueError, TypeError):
                continue
            _mark(ws, we)
    except Exception:  # pragma: no cover - defensive
        logger.debug("dhw_bucket_bias: boost-window read failed", exc_info=True)

    if bool(getattr(config, "DHW_LEGIONELLA_STANDOFF_ENABLED", True)):
        dow = int(getattr(config, "DHW_LEGIONELLA_STANDOFF_DOW", 6))
        h = int(getattr(config, "DHW_LEGIONELLA_STANDOFF_START_HOUR_UTC", 11))
        m = int(getattr(config, "DHW_LEGIONELLA_STANDOFF_START_MINUTE_UTC", 0))
        dur = int(getattr(config, "DHW_LEGIONELLA_STANDOFF_DURATION_MINUTES", 120))
        d = start_day
        while d <= end_day:
            if d.weekday() == dow:
                ws = datetime.combine(d, time(h, m), tzinfo=UTC)
                _mark(ws, ws + timedelta(minutes=dur))
            d += timedelta(days=1)
    return out


def _usable_rows(
    window_days: int, rows: list | None = None, *, today: date | None = None
) -> tuple[list[dict[str, Any]], date]:
    """Fetch + filter error-log rows: contaminated (day, bucket) pairs out,
    non-normal-mode rows out, NULL actuals out, raw forecast floored. Each
    returned row gains ``raw`` (the de-biased forecast — the open-loop
    denominator). Shared by learning and the backtest so both see the same
    sample. ``today`` anchors recency (LOCAL date — the day keys are local)."""
    from . import db as _db

    min_forecast = float(getattr(config, "DHW_BUCKET_BIAS_MIN_FORECAST_KWH", 0.05))
    today = today or datetime.now(_tz()).date()
    start_day = today - timedelta(days=window_days)
    if rows is None:
        rows = _db.get_dhw_error_log_range(start_day.isoformat(), today.isoformat())
    contaminated = _contaminated_day_buckets(start_day, today)

    out: list[dict[str, Any]] = []
    for r in rows:
        f = r.get("forecast_kwh")
        a = r.get("actual_kwh")
        if f is None or a is None:
            continue  # NULL actual = missing split, NOT zero — dropped honestly
        mode = r.get("mode")
        if mode is not None and str(mode) != "normal":
            continue  # guests/vacation shapes must not train the normal factors
        try:
            day = date.fromisoformat(str(r["day"]))
            b = int(r["bucket_idx"])
        except (ValueError, TypeError, KeyError):
            continue
        if (day.isoformat(), b) in contaminated:
            continue
        applied = float(r.get("applied_factor") or 1.0)
        raw = float(f) / applied if applied > 0 else float(f)
        if raw < min_forecast:
            continue  # raw-side floor: a shrunk bucket can't starve its own learning
        out.append({"day": day, "bucket": b, "raw": raw, "actual": float(a)})
    return out, today


def _bucket_ratios(usable: list[dict[str, Any]], *, today: date) -> dict[int, dict]:
    """Recency-weighted per-bucket accumulator over pre-filtered rows.
    Bucket ratio = Σw·actual / Σw·raw (weighted ratio-of-sums)."""
    half_life = float(getattr(config, "DHW_BUCKET_BIAS_HALFLIFE_DAYS", 5))
    acc: dict[int, dict] = {}
    for r in usable:
        age_days = max(0, (today - r["day"]).days)
        w = 0.5 ** (age_days / half_life) if half_life > 0 else 1.0
        d = acc.setdefault(r["bucket"], {"sf": 0.0, "sa": 0.0, "n": 0, "days": set()})
        d["sf"] += w * r["raw"]
        d["sa"] += w * r["actual"]
        d["n"] += 1
        d["days"].add(r["day"])
    return acc


def _factors_from_acc(
    acc: dict[int, dict], *, lo: float, hi: float, min_days: int
) -> dict[int, float]:
    """Clamp + min-DISTINCT-DAYS gate over an accumulator → {bucket: factor}."""
    out: dict[int, float] = {}
    for b, d in acc.items():
        if d["sf"] <= 0 or len(d["days"]) < min_days:
            continue
        out[b] = max(lo, min(hi, d["sa"] / d["sf"]))
    return out


def compute_dhw_bucket_bias() -> tuple[dict[int, float], dict[int, int], dict[int, int], dict[str, Any]]:
    """Return ``(factors, samples, days, diag)`` — the open-loop multiplicative
    factor per local 2h bucket. Idempotent over the same data: re-running the
    refresh never compounds (there is no prior-state feedback)."""
    window = int(getattr(config, "DHW_BUCKET_BIAS_WINDOW_DAYS", 14))
    lo = float(getattr(config, "DHW_BUCKET_BIAS_MIN", 0.25))
    hi = float(getattr(config, "DHW_BUCKET_BIAS_MAX", 3.0))
    min_days = int(getattr(config, "DHW_BUCKET_BIAS_MIN_DAYS", 3))

    usable, today = _usable_rows(window)
    acc = _bucket_ratios(usable, today=today)
    factors = _factors_from_acc(acc, lo=lo, hi=hi, min_days=min_days)
    samples = {b: int(acc[b]["n"]) for b in factors}
    days = {b: len(acc[b]["days"]) for b in factors}
    at_clamp = [b for b, f in factors.items() if f <= lo + 1e-9 or f >= hi - 1e-9]
    diag = {"window_days": window, "clamp": [lo, hi], "min_days": min_days,
            "n_buckets": len(factors), "at_clamp": at_clamp,
            "learned_from": "normal-mode de-biased rows",
            "decontaminated": "tank boosts + user overrides + legionella"}
    return factors, samples, days, diag


def refresh_dhw_bucket_bias() -> int:
    """Recompute + persist the bucket-bias table (nightly cron, right after the
    dhw_error_log rebuild — the rebuild stamps ``applied_factor`` BEFORE this
    refresh moves the table). Open-loop and cheap; NO LP effect unless
    DHW_BUCKET_BIAS_ENABLED."""
    from . import db as _db

    factors, samples, days, diag = compute_dhw_bucket_bias()
    if not factors:
        logger.info("dhw_bucket_bias: nothing to compute (%s)", diag)
        return 0
    ca = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    n = _db.upsert_dhw_bucket_bias(factors, factors, samples, ca, days=days)
    if diag["at_clamp"]:
        logger.warning(
            "dhw_bucket_bias: %d bucket(s) at the clamp %s — true ratio beyond "
            "%s; inspect /api/v1/dhw/error-log before (re)enabling",
            len(diag["at_clamp"]), sorted(diag["at_clamp"]), diag["clamp"],
        )
    logger.info(
        "dhw_bucket_bias refreshed: %d buckets; factors=%s",
        n, {b: round(factors[b], 3) for b in sorted(factors)},
    )
    return n


def normalized_factors(factors: dict[int, float], mode: str) -> dict[int, float]:
    """Shape-only view of ``factors`` for ``mode``: divided by the nominal
    energy-share-weighted mean so applying them preserves the forecast's daily
    total (level remains the auto-scale's job; see module docstring). Buckets
    without a learned factor enter the mean as 1.0 — and the returned dict is
    COMPLETE (all 12 buckets, each carrying the 1/norm renormalization),
    otherwise unlearned buckets would dodge the renormalization and the total
    would drift. Returns ``{}`` for an empty input."""
    if not factors:
        return {}
    from .dhw_policy import _nominal_bucket_shares

    shares = _nominal_bucket_shares(mode)
    norm = sum(s * float(factors.get(b, 1.0)) for b, s in shares.items())
    if norm <= 0:
        return {}
    return {b: float(factors.get(b, 1.0)) / norm for b in range(_N_BUCKETS)}


def factors_in_force(mode: str) -> dict[int, float]:
    """The normalized factors production is actually applying right now: empty
    unless enabled, mode == normal, and the stored table fresh (within
    ``DHW_BUCKET_BIAS_MAX_AGE_DAYS``). Used by the forecast AND by the
    error-log rebuild (to stamp ``applied_factor``), so the learner's
    de-biasing sees exactly what the forecast did."""
    if not bool(getattr(config, "DHW_BUCKET_BIAS_ENABLED", False)):
        return {}
    if mode != "normal":
        return {}
    from . import db as _db

    try:
        max_age = int(getattr(config, "DHW_BUCKET_BIAS_MAX_AGE_DAYS", 7))
        stored = _db.get_dhw_bucket_bias(max_age_days=max_age)
    except Exception:  # noqa: BLE001 — forecast must not fail on bias reads
        return {}
    return normalized_factors(stored, mode)


def _evaluate(
    usable: list[dict[str, Any]], factors: dict[int, float]
) -> dict[str, Any] | None:
    """Replay ``corrected = raw × factor[bucket]`` over pre-filtered rows;
    MAE/bias before (raw, factor 1.0) vs after. Uses the same de-biased sample
    as learning, so the number describes exactly what enabling would apply."""
    before_abs = before_sum = after_abs = after_sum = 0.0
    n = 0
    for r in usable:
        corrected = r["raw"] * factors.get(r["bucket"], 1.0)
        e0, e1 = r["actual"] - r["raw"], r["actual"] - corrected
        before_abs += abs(e0); before_sum += e0
        after_abs += abs(e1); after_sum += e1
        n += 1
    if n == 0:
        return None
    mae0, mae1 = before_abs / n, after_abs / n
    return {
        "n_buckets_evaluated": n,
        "before": {"mae_kwh": round(mae0, 4), "bias_kwh": round(before_sum / n, 4)},
        "after": {"mae_kwh": round(mae1, 4), "bias_kwh": round(after_sum / n, 4)},
        "mae_reduction_kwh": round(mae0 - mae1, 4),
        "mae_reduction_pct": round((mae0 - mae1) / mae0 * 100, 2) if mae0 > 0 else 0.0,
    }


def backtest_dhw_bucket_bias(window_days: int | None = None) -> dict[str, Any]:
    """Offline what-if for the corrector — the gate for enabling it. Reports:

    * ``in_sample`` — factors fit on the whole window, evaluated on the same
      rows (optimistic).
    * ``out_of_sample`` — factors fit on the OLDER half, evaluated on the
      RECENT half (honest; this is the number to decide on).

    Because learning is open-loop, the evaluated factors ARE what a refresh
    would store and production would apply (normalized) — no gap between the
    gate and the live behavior. Fitting, filtering and evaluation all share
    ``_usable_rows``. Read-only.
    """
    window = int(window_days or getattr(config, "DHW_BUCKET_BIAS_WINDOW_DAYS", 14))
    lo = float(getattr(config, "DHW_BUCKET_BIAS_MIN", 0.25))
    hi = float(getattr(config, "DHW_BUCKET_BIAS_MAX", 3.0))
    min_days = int(getattr(config, "DHW_BUCKET_BIAS_MIN_DAYS", 3))

    usable, today = _usable_rows(window)
    if not usable:
        return {"n_rows": 0, "note": "no usable dhw_error_log rows in window"}

    # In-sample: fit on all, evaluate on all.
    fac_all = normalized_factors(
        _factors_from_acc(_bucket_ratios(usable, today=today),
                          lo=lo, hi=hi, min_days=min_days),
        "normal",
    )
    in_sample = _evaluate(usable, fac_all)

    # Out-of-sample: split at the window midpoint; fit older, eval recent.
    mid = today - timedelta(days=window // 2)
    older = [r for r in usable if r["day"] < mid]
    recent = [r for r in usable if r["day"] >= mid]
    out_of_sample = None
    if older and recent:
        fac_old = normalized_factors(
            _factors_from_acc(_bucket_ratios(older, today=today),
                              lo=lo, hi=hi, min_days=max(1, min_days // 2)),
            "normal",
        )
        out_of_sample = _evaluate(recent, fac_old)

    return {
        "window_days": window,
        "evaluated_with": "normalized open-loop factors (normal-mode shares)",
        "n_rows_usable": len(usable),
        "n_buckets_corrected": len(fac_all),
        "in_sample": in_sample,
        "out_of_sample": out_of_sample,
        "factor_by_bucket": {str(b): round(fac_all[b], 4) for b in sorted(fac_all)},
    }
