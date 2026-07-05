"""Adaptive closed-loop DHW bucket-bias corrector (the DHW analog of load_bias).

The pinned DHW forecast (``dhw_policy.forecast_dhw_load_per_slot``) has the
LEVEL corrected by the trailing auto-scale (#534) but carries a per-2h-bucket
SHAPE error: prod ``dhw_error_log`` shows the daytime warmup window
over-forecast ~3-4x in summer while the 13:00 warmup-transition bucket has been
observed ~4x UNDER. The shape error is partly the tank's real warmup/decay
inertia spilling across buckets — the bucket factors absorb that timing error
empirically, without a physics rewrite.

This module measures the residual per-LOCAL-2h-bucket ratio from
``dhw_error_log`` and produces a MULTIPLICATIVE correction per bucket. Two
deliberate deviations from the PV/load twins:

* **Ratio-of-sums, not mean-of-ratios** — DHW bucket energies are small
  (0.03-0.45 kWh) so per-row ratios explode on tiny denominators; the
  recency-weighted Σactual/Σforecast is robust (same lesson as the PV
  calibration median→ratio-of-sums switch).
* **Asymmetric filter** — rows need ``forecast_kwh`` above a floor (ratio
  denominator) but low/zero ``actual_kwh`` rows are KEPT: actual≈0 against a
  1.5-2 kWh forecast is exactly the daytime over-forecast signal. The PV-style
  symmetric min-kwh drop would discard it.

The stored factors are shape-RAW. Total-preservation is enforced at
APPLICATION time in ``forecast_dhw_load_per_slot``: the factors are divided by
the energy-share-weighted mean over the *current mode's* nominal shares, so the
forecast daily total is unchanged and the level stays the auto-scale's job.
Required, not just tidy: the auto-scale is open-loop w.r.t. the committed
forecast (measured daily actual ÷ fixed nominal constants) and would never back
off if this corrector also moved the level — two integrators on one error, one
of them blind.

Closed loop (the #488 lesson — the error log's forecast side is post-bias once
enabled): each refresh nudges the previous factor by the residual ratio of the
already-corrected forecast, ``applied = prev × (1 + damping·(ratio − 1))``,
warm-started from the raw ratio. Hard-clamped to
[DHW_BUCKET_BIAS_MIN, DHW_BUCKET_BIAS_MAX].

DEFAULT OFF: the table refreshes nightly (cheap, observable) but the forecast
only APPLIES it when ``DHW_BUCKET_BIAS_ENABLED`` — the corrected value feeds a
hard LP equality (K2 pin), so enabling is gated on the backtest endpoint.
"""
from __future__ import annotations

import logging
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .config import config

logger = logging.getLogger(__name__)

_N_BUCKETS = 12  # local 2h buckets, matching dhw_error_log / daikin_consumption_2hourly


def _contaminated_day_buckets(start_day: date, end_day: date) -> set[tuple[str, int]]:
    """(day_iso, bucket_idx) pairs whose measured DHW energy is NOT the
    schedule's doing — negative-price boosts (deliberate max-heating the
    forecast budgets separately) and the firmware's weekly legionella cycle
    (never in the forecast at all). Learning from either would poison the
    factors of ordinary buckets."""
    from . import db as _db

    tz = ZoneInfo(config.BULLETPROOF_TIMEZONE)
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


def _bucket_ratios(
    window_days: int, rows: list | None = None, *, now: datetime | None = None
) -> dict[int, dict]:
    """Recency-weighted per-bucket accumulator over ``dhw_error_log`` rows.
    Returns ``{bucket: {"sf":.., "sa":.., "n":.., "days": set()}}`` where the
    bucket ratio is sa/sf (weighted ratio-of-sums). ``rows`` overridable for
    the backtest holdout."""
    from . import db as _db

    half_life = float(getattr(config, "DHW_BUCKET_BIAS_HALFLIFE_DAYS", 5))
    min_forecast = float(getattr(config, "DHW_BUCKET_BIAS_MIN_FORECAST_KWH", 0.05))

    now = now or datetime.now(UTC)
    end_day = now.date()
    start_day = end_day - timedelta(days=window_days)
    if rows is None:
        rows = _db.get_dhw_error_log_range(start_day.isoformat(), end_day.isoformat())
    contaminated = _contaminated_day_buckets(start_day, end_day)

    acc: dict[int, dict] = {}
    for r in rows:
        f = r.get("forecast_kwh")
        a = r.get("actual_kwh")
        if f is None or a is None or float(f) < min_forecast:
            continue  # actual≈0 rows are kept on purpose — that's the signal
        try:
            day = date.fromisoformat(str(r["day"]))
            b = int(r["bucket_idx"])
        except (ValueError, TypeError, KeyError):
            continue
        if (day.isoformat(), b) in contaminated:
            continue
        age_days = max(0.0, (end_day - day).days)
        w = 0.5 ** (age_days / half_life) if half_life > 0 else 1.0
        d = acc.setdefault(b, {"sf": 0.0, "sa": 0.0, "n": 0, "days": set()})
        d["sf"] += w * float(f)
        d["sa"] += w * float(a)
        d["n"] += 1
        d["days"].add(day)
    return acc


def _factors_from_acc(
    acc: dict[int, dict], *, lo: float, hi: float, min_days: int
) -> dict[int, float]:
    """Clamp + min-DISTINCT-DAYS gate over an accumulator → {bucket: raw ratio}."""
    out: dict[int, float] = {}
    for b, d in acc.items():
        if d["sf"] <= 0 or len(d["days"]) < min_days:
            continue
        out[b] = max(lo, min(hi, d["sa"] / d["sf"]))
    return out


def compute_dhw_bucket_bias() -> tuple[dict[int, float], dict[int, float], dict[int, int], dict[str, Any]]:
    """Return ``(applied, raw, samples, diag)`` — multiplicative factor per
    local 2h bucket. ``applied`` accumulates on the previous factor; ``raw`` is
    this refresh's measured recency-weighted ratio-of-sums."""
    from . import db as _db

    window = int(getattr(config, "DHW_BUCKET_BIAS_WINDOW_DAYS", 14))
    damping = float(getattr(config, "DHW_BUCKET_BIAS_DAMPING", 0.5))
    lo = float(getattr(config, "DHW_BUCKET_BIAS_MIN", 0.25))
    hi = float(getattr(config, "DHW_BUCKET_BIAS_MAX", 3.0))
    min_days = int(getattr(config, "DHW_BUCKET_BIAS_MIN_DAYS", 3))

    try:
        prev = _db.get_dhw_bucket_bias()
    except Exception:  # pragma: no cover — cold start / missing table
        prev = {}

    acc = _bucket_ratios(window)
    raw_map = _factors_from_acc(acc, lo=lo, hi=hi, min_days=min_days)
    applied: dict[int, float] = {}
    raw: dict[int, float] = {}
    samples: dict[int, int] = {}
    for b, ratio in raw_map.items():
        if b in prev:
            # Nudge the previous factor by the RESIDUAL ratio of the already-
            # corrected forecast → ramps to full correction, settles at ratio≈1.
            new = float(prev[b]) * (1.0 + damping * (ratio - 1.0))
        else:
            new = ratio  # warm start — the historical error is already measured
        new = max(lo, min(hi, new))
        raw[b] = round(ratio, 4)
        applied[b] = round(new, 4)
        samples[b] = int(acc[b]["n"])
    diag = {"window_days": window, "damping": damping, "clamp": [lo, hi],
            "min_days": min_days, "n_buckets": len(applied),
            "decontaminated": "tank_negative_boost + legionella windows"}
    return applied, raw, samples, diag


def refresh_dhw_bucket_bias() -> int:
    """Recompute + persist the bucket-bias table (nightly cron, right after the
    dhw_error_log rebuild). Refreshing is cheap and has NO LP effect unless
    DHW_BUCKET_BIAS_ENABLED."""
    from . import db as _db

    applied, raw, samples, diag = compute_dhw_bucket_bias()
    if not applied:
        logger.info("dhw_bucket_bias: nothing to compute (%s)", diag)
        return 0
    ca = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    n = _db.upsert_dhw_bucket_bias(applied, raw, samples, ca)
    logger.info(
        "dhw_bucket_bias refreshed: %d buckets; applied=%s raw=%s",
        n, {b: applied[b] for b in sorted(applied)}, {b: raw[b] for b in sorted(raw)},
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


def _evaluate(rows: list, factors: dict[int, float], min_forecast: float) -> dict[str, Any] | None:
    """Replay ``corrected = forecast × factor[bucket]`` over ``rows``;
    MAE/bias before vs after."""
    before_abs = before_sum = after_abs = after_sum = 0.0
    n = 0
    for r in rows:
        f = r.get("forecast_kwh")
        a = r.get("actual_kwh")
        if f is None or a is None or float(f) < min_forecast:
            continue
        try:
            b = int(r["bucket_idx"])
        except (ValueError, TypeError, KeyError):
            continue
        corrected = float(f) * factors.get(b, 1.0)
        e0, e1 = float(a) - float(f), float(a) - corrected
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

    Evaluation uses the total-preserving normalized factors (normal-mode
    shares — the dominant regime), i.e. exactly what the LP would see.
    Read-only — never writes, never touches the LP.
    """
    from . import db as _db

    window = int(window_days or getattr(config, "DHW_BUCKET_BIAS_WINDOW_DAYS", 14))
    lo = float(getattr(config, "DHW_BUCKET_BIAS_MIN", 0.25))
    hi = float(getattr(config, "DHW_BUCKET_BIAS_MAX", 3.0))
    min_days = int(getattr(config, "DHW_BUCKET_BIAS_MIN_DAYS", 3))
    min_forecast = float(getattr(config, "DHW_BUCKET_BIAS_MIN_FORECAST_KWH", 0.05))

    now = datetime.now(UTC)
    end_day = now.date()
    start_day = end_day - timedelta(days=window)
    rows = _db.get_dhw_error_log_range(start_day.isoformat(), end_day.isoformat())
    if not rows:
        return {"n_rows": 0, "note": "no dhw_error_log rows in window"}

    # In-sample: fit on all, evaluate on all.
    acc_all = _bucket_ratios(window, rows=rows, now=now)
    fac_all = normalized_factors(
        _factors_from_acc(acc_all, lo=lo, hi=hi, min_days=min_days), "normal"
    )
    in_sample = _evaluate(rows, fac_all, min_forecast)

    # Out-of-sample: split at the window midpoint; fit older, eval recent.
    mid = (end_day - timedelta(days=window // 2)).isoformat()
    older = [r for r in rows if str(r["day"]) < mid]
    recent = [r for r in rows if str(r["day"]) >= mid]
    out_of_sample = None
    if older and recent:
        acc_old = _bucket_ratios(window, rows=older, now=now)
        fac_old = normalized_factors(
            _factors_from_acc(acc_old, lo=lo, hi=hi, min_days=max(1, min_days // 2)),
            "normal",
        )
        out_of_sample = _evaluate(recent, fac_old, min_forecast)

    return {
        "window_days": window,
        "evaluated_with": "normalized factors (normal-mode shares)",
        "n_buckets_corrected": len(fac_all),
        "in_sample": in_sample,
        "out_of_sample": out_of_sample,
        "factor_by_bucket": {str(b): round(fac_all[b], 4) for b in sorted(fac_all)},
    }
