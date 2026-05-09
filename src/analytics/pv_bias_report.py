"""Morning vs afternoon PV forecast bias.

Wraps :func:`src.weather.evaluate_pv_forecast_accuracy` and bins the per-hour
``bias_kw`` series into ``morning`` (04–12 UTC) and ``afternoon`` (12–20 UTC)
aggregates so the morning brief and the ``get_pv_forecast_bias`` MCP tool can
say things like "PV bias 14 d: am +0.18 kW under-forecast, pm −0.05 kW over".

Sign convention follows :func:`evaluate_pv_forecast_accuracy`:
``bias_kw = mean(actual − predicted)``. Positive → forecast was too low
(under-forecast); negative → forecast was too high (over-forecast).
"""
from __future__ import annotations

from typing import Any

from .. import weather

# Hour bins use UTC because the underlying tables key on UTC ``captured_at`` /
# ``slot_time``. London ≡ UTC in winter and UTC+1 in summer; the bins
# 04–12 / 12–20 UTC straddle that drift cleanly enough for a daily report.
MORNING_HOURS: tuple[int, ...] = (4, 5, 6, 7, 8, 9, 10, 11)
AFTERNOON_HOURS: tuple[int, ...] = (12, 13, 14, 15, 16, 17, 18, 19)


def _aggregate_bin(per_hour: dict[int, dict[str, Any]], hours: tuple[int, ...]) -> dict[str, Any]:
    rows = [per_hour[h] for h in hours if h in per_hour]
    n = sum(int(r.get("n", 0)) for r in rows)
    if n <= 0:
        return {"bias_kw": None, "mae_kw": None, "rmse_kw": None, "n": 0, "hours": list(hours)}
    weighted_bias = sum(float(r.get("bias_kw", 0.0)) * int(r.get("n", 0)) for r in rows) / n
    weighted_mae = sum(float(r.get("mae_kw", 0.0)) * int(r.get("n", 0)) for r in rows) / n
    sq = sum((float(r.get("rmse_kw", 0.0)) ** 2) * int(r.get("n", 0)) for r in rows) / n
    return {
        "bias_kw": round(weighted_bias, 3),
        "mae_kw": round(weighted_mae, 3),
        "rmse_kw": round(sq ** 0.5, 3),
        "n": n,
        "hours": list(hours),
    }


def summarise_pv_bias(window_days: int = 14) -> dict[str, Any]:
    """Compute the morning/afternoon PV-forecast bias summary.

    Returns a dict shaped for both the morning brief and the MCP tool::

        {
            "ok": True,
            "window_days": 14,
            "n_paired": 245,
            "morning": {"bias_kw": 0.18, "mae_kw": 0.32, "rmse_kw": 0.45, "n": 88, "hours": [...]},
            "afternoon": {"bias_kw": -0.05, ..., "n": 120, "hours": [...]},
            "overall": {"bias_kw": ..., "mae_kw": ..., "rmse_kw": ..., "mape_pct": ...},
            "headline": "am +0.18 kW under, pm −0.05 kW over (14 d, n=245)",
        }

    When the underlying paired sample is empty the function still returns
    ``ok: True`` with ``n_paired: 0`` and ``headline: "no paired samples"`` —
    callers can treat that as "skip the line in the brief".
    """
    raw = weather.evaluate_pv_forecast_accuracy(window_days=int(window_days))
    per_hour = raw.get("per_hour") or {}
    overall = raw.get("overall") or {}
    n_paired = int(raw.get("n_paired", 0))

    morning = _aggregate_bin(per_hour, MORNING_HOURS)
    afternoon = _aggregate_bin(per_hour, AFTERNOON_HOURS)

    headline = _format_headline(window_days, n_paired, morning, afternoon)

    return {
        "ok": True,
        "window_days": int(window_days),
        "n_paired": n_paired,
        "morning": morning,
        "afternoon": afternoon,
        "overall": {
            "bias_kw": overall.get("bias_kw"),
            "mae_kw": overall.get("mae_kw"),
            "rmse_kw": overall.get("rmse_kw"),
            "mape_pct": overall.get("mape_pct"),
            "mean_actual_kw": overall.get("mean_actual_kw"),
            "mean_pred_kw": overall.get("mean_pred_kw"),
        },
        "headline": headline,
    }


def _format_bin(label: str, b: dict[str, Any]) -> str | None:
    bias = b.get("bias_kw")
    if bias is None:
        return None
    direction = "under" if bias > 0 else ("over" if bias < 0 else "neutral")
    sign = "+" if bias > 0 else ("−" if bias < 0 else "")
    # Use a real minus sign for over-forecast so screen-readers + Telegram
    # render consistently. abs() avoids "−-0.05".
    return f"{label} {sign}{abs(bias):.2f} kW {direction}"


def _format_headline(window_days: int, n_paired: int, morning: dict, afternoon: dict) -> str:
    if n_paired <= 0:
        return f"PV bias {window_days} d: no paired samples yet"
    parts = [p for p in (_format_bin("am", morning), _format_bin("pm", afternoon)) if p]
    if not parts:
        return f"PV bias {window_days} d: no paired samples in morning/afternoon bins"
    return f"PV bias {window_days} d: {', '.join(parts)} (n={n_paired})"
