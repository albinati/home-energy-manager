"""Adaptive closed-loop LOAD bias corrector (Phase 2 — analog of the PV one).

The residual base-load forecast is the median of 120 days of history, so it's
unbiased at the LEVEL (overall) but carries a per-LOCAL-hour bias from seasonal /
occupancy regime shift (the median lags a trend). This module measures that bias
from ``load_error_log`` and produces an ADDITIVE per-local-hour correction
(kWh/slot) — additive, not multiplicative like PV, because the load bias is a
level offset, not a magnitude scaling.

Closed loop (the #488 lesson — accumulate, don't decay toward the raw input when
the input is your own output): each refresh nudges the previous correction by the
RESIDUAL error of the already-corrected committed forecast::

    new_bias = old_bias + damping · raw_bias        (raw_bias = mean(actual − committed_forecast))

so it ramps to full correction while the corrected forecast still mis-predicts,
and settles when raw_bias ≈ 0. Warm-starts from the measured bias (we already
have history). Hard-clamped to ±LOAD_RECENT_BIAS_MAX_KWH.

DEFAULT OFF: the table is refreshed nightly (cheap, observable) but the optimizer
only APPLIES it when ``LOAD_RECENT_BIAS_ENABLED``.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .config import config

logger = logging.getLogger(__name__)


def _local_hour_bias(window_days: int) -> tuple[dict[int, list[float]], ZoneInfo]:
    """Recency-weighted accumulator per local hour: {hour: [sum_w_err, sum_w, n]}."""
    from . import db as _db

    half_life = float(getattr(config, "LOAD_RECENT_BIAS_HALFLIFE_DAYS", 7))
    min_kwh = float(getattr(config, "LOAD_RECENT_BIAS_MIN_KWH", 0.05))
    tz = ZoneInfo(config.BULLETPROOF_TIMEZONE)

    now = datetime.now(UTC)
    start = now - timedelta(days=window_days)
    rows = _db.get_load_error_log_range(
        start.strftime("%Y-%m-%dT%H:%M:%SZ"), now.strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    acc: dict[int, list[float]] = {}
    for r in rows:
        f = r.get("forecast_kwh")
        a = r.get("actual_kwh")
        if f is None or a is None or f < min_kwh or a < min_kwh:
            continue
        try:
            ts = datetime.fromisoformat(str(r["slot_time_utc"]).replace("Z", "+00:00"))
        except (ValueError, TypeError, KeyError):
            continue
        lh = ts.astimezone(tz).hour
        age_days = max(0.0, (now - ts).total_seconds() / 86400.0)
        w = 0.5 ** (age_days / half_life) if half_life > 0 else 1.0
        d = acc.setdefault(lh, [0.0, 0.0, 0.0])
        d[0] += w * (float(a) - float(f))  # additive error (actual − forecast)
        d[1] += w
        d[2] += 1
    return acc, tz


def compute_load_recent_bias_by_hour_local() -> tuple[dict[int, float], dict[int, float], dict[int, int], dict[str, Any]]:
    """Return ``(applied_bias, raw_bias, samples, diag)`` — additive kWh/slot per
    local hour. ``applied_bias`` accumulates on the previous correction; ``raw_bias``
    is the measured recency-weighted residual error this refresh."""
    from . import db as _db

    window = int(getattr(config, "LOAD_RECENT_BIAS_WINDOW_DAYS", 21))
    damping = float(getattr(config, "LOAD_RECENT_BIAS_DAMPING", 0.5))
    max_kwh = float(getattr(config, "LOAD_RECENT_BIAS_MAX_KWH", 0.3))
    min_samples = int(getattr(config, "LOAD_RECENT_BIAS_MIN_SAMPLES", 3))

    try:
        prev = _db.get_load_recent_bias()
    except Exception:  # pragma: no cover — cold start / missing table
        prev = {}

    acc, _tz = _local_hour_bias(window)
    applied: dict[int, float] = {}
    raw: dict[int, float] = {}
    samples: dict[int, int] = {}
    for h, (sw, w, n) in acc.items():
        if w <= 0 or n < min_samples:
            continue
        raw_bias = sw / w
        if h in prev:
            # Accumulate on the previous correction by the RESIDUAL error of the
            # already-corrected forecast → ramps to full, settles at raw_bias≈0.
            new_bias = float(prev[h]) + damping * raw_bias
        else:
            new_bias = raw_bias  # warm start — we already have the historical error
        new_bias = max(-max_kwh, min(max_kwh, new_bias))
        raw[h] = round(raw_bias, 4)
        applied[h] = round(new_bias, 4)
        samples[h] = int(n)
    diag = {"window_days": window, "damping": damping, "clamp_kwh": max_kwh,
            "min_samples": min_samples, "n_hours": len(applied)}
    return applied, raw, samples, diag


def refresh_load_recent_bias() -> int:
    """Recompute + persist the additive load-bias table (nightly cron, after the
    error-log rebuild). Refreshing is cheap and has NO LP effect unless enabled."""
    from . import db as _db

    applied, raw, samples, diag = compute_load_recent_bias_by_hour_local()
    if not applied:
        logger.info("load_recent_bias: nothing to compute (%s)", diag)
        return 0
    ca = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    n = _db.upsert_load_recent_bias(applied, raw, samples, ca)
    logger.info(
        "load_recent_bias refreshed: %d hours; applied=%s raw=%s",
        n, {h: applied[h] for h in sorted(applied)}, {h: raw[h] for h in sorted(raw)},
    )
    return n


def backtest_load_recent_bias(window_days: int | None = None) -> dict[str, Any]:
    """Offline what-if: would the additive corrector have reduced the error over
    the persisted load_error_log? Computes the per-local-hour correction from the
    window, replays ``corrected = max(0, forecast + bias[hour])`` per slot, and
    reports MAE/bias BEFORE vs AFTER. Read-only — never writes, never touches the
    LP. This is the gate for deciding whether to enable the corrector.
    """
    from . import db as _db

    window = int(window_days or getattr(config, "LOAD_RECENT_BIAS_WINDOW_DAYS", 21))
    min_kwh = float(getattr(config, "LOAD_RECENT_BIAS_MIN_KWH", 0.05))
    # Compute the correction map from a clean (no-prev) run so the backtest reflects
    # the warm-start correction the loop converges to, not a half-ramped state.
    acc, tz = _local_hour_bias(window)
    max_kwh = float(getattr(config, "LOAD_RECENT_BIAS_MAX_KWH", 0.3))
    min_samples = int(getattr(config, "LOAD_RECENT_BIAS_MIN_SAMPLES", 3))
    bias = {}
    for h, (sw, w, n) in acc.items():
        if w > 0 and n >= min_samples:
            bias[h] = max(-max_kwh, min(max_kwh, sw / w))

    now = datetime.now(UTC)
    start = now - timedelta(days=window)
    rows = _db.get_load_error_log_range(
        start.strftime("%Y-%m-%dT%H:%M:%SZ"), now.strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    before_abs = before_sum = after_abs = after_sum = 0.0
    n = 0
    for r in rows:
        f = r.get("forecast_kwh")
        a = r.get("actual_kwh")
        if f is None or a is None or f < min_kwh:
            continue
        try:
            ts = datetime.fromisoformat(str(r["slot_time_utc"]).replace("Z", "+00:00"))
        except (ValueError, TypeError, KeyError):
            continue
        lh = ts.astimezone(tz).hour
        corrected = max(0.0, float(f) + bias.get(lh, 0.0))
        e0 = float(a) - float(f)
        e1 = float(a) - corrected
        before_abs += abs(e0); before_sum += e0
        after_abs += abs(e1); after_sum += e1
        n += 1
    if n == 0:
        return {"n": 0, "note": "no paired slots in window"}
    mae0, mae1 = before_abs / n, after_abs / n
    return {
        "window_days": window,
        "n_slots": n,
        "n_hours_corrected": len(bias),
        "before": {"mae_kwh": round(mae0, 4), "bias_kwh": round(before_sum / n, 4)},
        "after": {"mae_kwh": round(mae1, 4), "bias_kwh": round(after_sum / n, 4)},
        "mae_reduction_kwh": round(mae0 - mae1, 4),
        "mae_reduction_pct": round((mae0 - mae1) / mae0 * 100, 2) if mae0 > 0 else 0.0,
        "correction_by_hour_local": {str(h): round(bias[h], 4) for h in sorted(bias)},
    }
