"""Regression-baseline evaluator for *load* forecast accuracy (#231 analog for load).

Compares the LP's per-slot predicted total household load against measured load
from ``pv_realtime_history.load_power_kw``. Per-slot predicted total reconstructs
as ``base_load_json[slot_index] + dhw_kwh + space_kwh`` from the most recent
LP snapshot for that slot.

Also runs a daily aggregate check of *predicted* Daikin (``Σ dhw_kwh + space_kwh``)
against Onecta-measured ``daikin_consumption_daily.kwh_total``. We have no
per-slot Daikin meter, so this is the only way to quantify the physics-model
bias — and it's the diagnostic that drives any future Daikin calibration work.

Hour-of-day buckets are local (``BULLETPROOF_TIMEZONE``) because load is tied
to occupancy, not solar position. This matches the residual-load profile
in :func:`db.half_hourly_residual_load_profile_kwh`.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo


def evaluate_load_forecast_accuracy(
    window_days: int = 30,
    *,
    min_kwh: float = 0.01,
) -> dict[str, Any]:
    """Compare predicted vs realized household load across the last N days.

    For each unique ``slot_time_utc`` in the window, picks the latest LP run
    (``MAX(run_id)``) that produced a prediction for that slot. Predicted
    total = ``base_load_json[slot_index] + dhw_kwh + space_kwh``. Actual = mean
    ``load_power_kw`` in the 30-min slot window × 0.5 h.

    Slots where both predicted AND actual fall below ``min_kwh`` are excluded.

    Returns::

        {
            "window_days": 30,
            "n_paired": 1340,
            "overall": { mae_kwh_per_slot, rmse_kwh_per_slot, bias_kwh_per_slot,
                         mean_actual_kwh_per_slot, mean_pred_kwh_per_slot,
                         mape_pct, n },
            "per_hour_local": { 0..23 → same stats, keyed by local hour },
            "daikin_daily_check": { n_days, mae_kwh_per_day, rmse_kwh_per_day,
                                    bias_kwh_per_day,
                                    mean_predicted_daikin_kwh_per_day,
                                    mean_actual_daikin_kwh_per_day,
                                    mape_pct },
        }
    """
    from .. import db as _db
    from ..config import config

    end = date.today()
    start = end - timedelta(days=window_days)

    tz = ZoneInfo(config.BULLETPROOF_TIMEZONE)

    # 1. Pull latest predicted load per slot (joined with inputs for base_load_json)
    pred_by_slot: dict[str, dict[str, Any]] = {}
    with _db._lock:
        conn = _db.get_connection()
        try:
            cur = conn.execute(
                """
                SELECT s.slot_time_utc,
                       s.run_id,
                       s.slot_index,
                       s.dhw_kwh,
                       s.space_kwh,
                       i.base_load_json
                  FROM lp_solution_snapshot s
                  JOIN lp_inputs_snapshot   i  ON i.run_id = s.run_id
                  JOIN (
                        SELECT slot_time_utc, MAX(run_id) AS max_run
                          FROM lp_solution_snapshot
                         WHERE substr(slot_time_utc, 1, 10) BETWEEN ? AND ?
                         GROUP BY slot_time_utc
                       ) latest
                       ON latest.slot_time_utc = s.slot_time_utc
                      AND latest.max_run       = s.run_id
                """,
                (start.isoformat(), end.isoformat()),
            )
            for row in cur.fetchall():
                slot = _normalise_iso(row["slot_time_utc"])
                if slot is None:
                    continue
                try:
                    base_loads = json.loads(row["base_load_json"] or "[]")
                except (TypeError, ValueError):
                    continue
                idx = int(row["slot_index"])
                if idx >= len(base_loads):
                    continue
                base = float(base_loads[idx])
                dhw = float(row["dhw_kwh"] or 0.0)
                space = float(row["space_kwh"] or 0.0)
                pred_by_slot[slot] = {
                    "predicted_kwh": base + dhw + space,
                    "predicted_daikin_kwh": dhw + space,
                }

            # 2. Pull all load samples in window
            cur = conn.execute(
                """SELECT captured_at, load_power_kw
                     FROM pv_realtime_history
                    WHERE substr(captured_at, 1, 10) BETWEEN ? AND ?
                      AND load_power_kw IS NOT NULL""",
                (start.isoformat(), end.isoformat()),
            )
            samples = cur.fetchall()

            # 3. Pull Daikin daily ground truth
            cur = conn.execute(
                """SELECT date, kwh_total FROM daikin_consumption_daily
                    WHERE date BETWEEN ? AND ?
                      AND kwh_total IS NOT NULL""",
                (start.isoformat(), end.isoformat()),
            )
            daikin_actual_by_date = {row["date"]: float(row["kwh_total"]) for row in cur.fetchall()}
        finally:
            conn.close()

    # 4. Bucket samples into 30-min slots → mean load_kw × 0.5 = slot kWh
    actual_kw_by_slot: dict[str, list[float]] = defaultdict(list)
    for row in samples:
        ts = _parse_iso(row["captured_at"])
        if ts is None:
            continue
        slot_start = ts.replace(minute=0 if ts.minute < 30 else 30, second=0, microsecond=0)
        actual_kw_by_slot[slot_start.isoformat()].append(float(row["load_power_kw"]))

    # 5. Pair + bucket per local hour-of-day
    pairs_per_hour: dict[int, list[tuple[float, float]]] = defaultdict(list)
    all_pairs: list[tuple[float, float]] = []
    pred_daikin_by_date: dict[str, float] = defaultdict(float)

    for slot_iso, pred in pred_by_slot.items():
        ts = _parse_iso(slot_iso)
        if ts is None:
            continue
        local_hour = ts.astimezone(tz).hour
        local_date_iso = ts.astimezone(tz).date().isoformat()
        pred_daikin_by_date[local_date_iso] += pred["predicted_daikin_kwh"]

        kw_samples = actual_kw_by_slot.get(slot_iso)
        if not kw_samples:
            continue
        actual_kwh = (sum(kw_samples) / len(kw_samples)) * 0.5
        pred_kwh = pred["predicted_kwh"]
        if pred_kwh < min_kwh and actual_kwh < min_kwh:
            continue
        all_pairs.append((pred_kwh, actual_kwh))
        pairs_per_hour[local_hour].append((pred_kwh, actual_kwh))

    # 6. Daikin daily check (only days where Onecta actual is available)
    daikin_pairs: list[tuple[float, float]] = []
    for d_iso, actual_kwh in daikin_actual_by_date.items():
        if d_iso in pred_daikin_by_date:
            daikin_pairs.append((pred_daikin_by_date[d_iso], actual_kwh))

    return {
        "window_days": window_days,
        "n_paired": len(all_pairs),
        "overall": _slot_stats(all_pairs),
        "per_hour_local": {h: _slot_stats(p) for h, p in sorted(pairs_per_hour.items())},
        "daikin_daily_check": _daily_stats(daikin_pairs),
    }


def _slot_stats(pairs: list[tuple[float, float]]) -> dict[str, float]:
    if not pairs:
        return {
            "mae_kwh_per_slot": 0.0,
            "rmse_kwh_per_slot": 0.0,
            "bias_kwh_per_slot": 0.0,
            "mean_actual_kwh_per_slot": 0.0,
            "mean_pred_kwh_per_slot": 0.0,
            "mape_pct": 0.0,
            "n": 0,
        }
    n = len(pairs)
    errs = [a - p for p, a in pairs]
    mae = sum(abs(e) for e in errs) / n
    rmse = (sum(e * e for e in errs) / n) ** 0.5
    bias = sum(errs) / n
    mean_actual = sum(a for _, a in pairs) / n
    mean_pred = sum(p for p, _ in pairs) / n
    ape = [abs(a - p) / a * 100 for p, a in pairs if a > 0.05]
    mape = sum(ape) / len(ape) if ape else 0.0
    return {
        "mae_kwh_per_slot": round(mae, 4),
        "rmse_kwh_per_slot": round(rmse, 4),
        "bias_kwh_per_slot": round(bias, 4),
        "mean_actual_kwh_per_slot": round(mean_actual, 4),
        "mean_pred_kwh_per_slot": round(mean_pred, 4),
        "mape_pct": round(mape, 2),
        "n": n,
    }


def _daily_stats(pairs: list[tuple[float, float]]) -> dict[str, float]:
    if not pairs:
        return {
            "n_days": 0,
            "mae_kwh_per_day": 0.0,
            "rmse_kwh_per_day": 0.0,
            "bias_kwh_per_day": 0.0,
            "mean_predicted_daikin_kwh_per_day": 0.0,
            "mean_actual_daikin_kwh_per_day": 0.0,
            "mape_pct": 0.0,
        }
    n = len(pairs)
    errs = [a - p for p, a in pairs]
    mae = sum(abs(e) for e in errs) / n
    rmse = (sum(e * e for e in errs) / n) ** 0.5
    bias = sum(errs) / n
    mean_pred = sum(p for p, _ in pairs) / n
    mean_actual = sum(a for _, a in pairs) / n
    ape = [abs(a - p) / a * 100 for p, a in pairs if a > 0.5]
    mape = sum(ape) / len(ape) if ape else 0.0
    return {
        "n_days": n,
        "mae_kwh_per_day": round(mae, 4),
        "rmse_kwh_per_day": round(rmse, 4),
        "bias_kwh_per_day": round(bias, 4),
        "mean_predicted_daikin_kwh_per_day": round(mean_pred, 4),
        "mean_actual_daikin_kwh_per_day": round(mean_actual, 4),
        "mape_pct": round(mape, 2),
    }


def _parse_iso(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC)


def _normalise_iso(raw: Any) -> str | None:
    """Return a canonical UTC ISO key for slot_time_utc — matches the format
    used when bucketing pv_realtime_history samples into 30-min slots."""
    ts = _parse_iso(raw)
    return ts.isoformat() if ts else None
