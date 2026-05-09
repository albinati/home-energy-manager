"""Tests for the morning vs afternoon PV forecast bias summariser.

The underlying ``weather.evaluate_pv_forecast_accuracy`` is exercised by
``test_pv_forecast_accuracy.py``; here we mock it to keep these tests fast
and pinpoint the binning + headline logic.
"""
from __future__ import annotations

from typing import Any

import pytest


def _fake_eval(per_hour: dict[int, dict[str, float]] | None,
               n_paired: int = 100,
               overall: dict[str, float] | None = None) -> dict[str, Any]:
    return {
        "window_days": 14,
        "n_paired": int(n_paired),
        "overall": overall or {
            "mae_kw": 0.40, "rmse_kw": 0.55, "bias_kw": 0.05,
            "mean_actual_kw": 0.80, "mean_pred_kw": 0.75,
            "mape_pct": 35.0, "n": int(n_paired),
        },
        "per_hour": per_hour or {},
    }


def test_under_forecast_morning_renders_positive_bias(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mornings under-forecast (actual > predicted) → morning bias_kw > 0."""
    from src.analytics import pv_bias_report
    from src import weather as weather_mod

    per_hour = {h: {"bias_kw": 0.20, "mae_kw": 0.30, "rmse_kw": 0.40, "n": 10}
                for h in (4, 5, 6, 7, 8, 9, 10, 11)}
    per_hour.update({h: {"bias_kw": -0.05, "mae_kw": 0.20, "rmse_kw": 0.25, "n": 10}
                     for h in (12, 13, 14, 15, 16, 17, 18, 19)})
    monkeypatch.setattr(weather_mod, "evaluate_pv_forecast_accuracy",
                        lambda window_days: _fake_eval(per_hour, n_paired=160))

    out = pv_bias_report.summarise_pv_bias(window_days=14)
    assert out["ok"] is True
    assert out["window_days"] == 14
    assert out["n_paired"] == 160
    assert out["morning"]["bias_kw"] == pytest.approx(0.20, abs=0.01)
    assert out["morning"]["n"] == 80  # 8 hours × 10
    assert out["afternoon"]["bias_kw"] == pytest.approx(-0.05, abs=0.01)
    assert "am +0.20 kW under" in out["headline"]
    assert "pm" in out["headline"] and "0.05" in out["headline"]


def test_over_forecast_uses_minus_sign_in_headline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Afternoons over-forecast → headline labels them with 'over'."""
    from src.analytics import pv_bias_report
    from src import weather as weather_mod

    per_hour = {h: {"bias_kw": -0.50, "mae_kw": 0.55, "rmse_kw": 0.70, "n": 12}
                for h in (12, 13, 14, 15, 16, 17, 18, 19)}
    monkeypatch.setattr(weather_mod, "evaluate_pv_forecast_accuracy",
                        lambda window_days: _fake_eval(per_hour, n_paired=96))

    out = pv_bias_report.summarise_pv_bias(window_days=14)
    assert out["afternoon"]["bias_kw"] == pytest.approx(-0.50)
    assert out["morning"]["bias_kw"] is None  # nothing in morning hours
    assert "pm" in out["headline"] and "over" in out["headline"]


def test_empty_data_returns_skip_signal(monkeypatch: pytest.MonkeyPatch) -> None:
    """No paired samples → ``n_paired=0`` and a 'no paired samples' headline.
    Callers (the morning brief) treat this as 'skip the line'."""
    from src.analytics import pv_bias_report
    from src import weather as weather_mod

    monkeypatch.setattr(weather_mod, "evaluate_pv_forecast_accuracy",
                        lambda window_days: _fake_eval({}, n_paired=0))

    out = pv_bias_report.summarise_pv_bias(window_days=14)
    assert out["ok"] is True
    assert out["n_paired"] == 0
    assert "no paired samples" in out["headline"]
    assert out["morning"]["bias_kw"] is None
    assert out["afternoon"]["bias_kw"] is None


def test_weighted_aggregation_uses_sample_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hours with more samples should pull the weighted bias_kw toward them.

    1 hour with bias +1.0 (n=100) and 7 hours with bias 0.0 (n=1 each)
    → morning bias ≈ 100 / 107 ≈ 0.93, NOT the simple average 0.125.
    """
    from src.analytics import pv_bias_report
    from src import weather as weather_mod

    per_hour: dict[int, dict[str, float]] = {}
    for h in (4, 5, 6, 7, 8, 9, 10):
        per_hour[h] = {"bias_kw": 0.0, "mae_kw": 0.1, "rmse_kw": 0.1, "n": 1}
    per_hour[11] = {"bias_kw": 1.0, "mae_kw": 1.0, "rmse_kw": 1.0, "n": 100}
    monkeypatch.setattr(weather_mod, "evaluate_pv_forecast_accuracy",
                        lambda window_days: _fake_eval(per_hour, n_paired=107))

    out = pv_bias_report.summarise_pv_bias(window_days=14)
    assert out["morning"]["n"] == 107
    # weighted = (0×7 + 1.0×100) / 107 ≈ 0.935
    assert out["morning"]["bias_kw"] == pytest.approx(0.935, abs=0.01)


def test_morning_brief_skips_line_when_no_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """The brief helper returns None when n_paired == 0 so the line drops out."""
    from src.analytics import daily_brief
    from src.analytics import pv_bias_report

    monkeypatch.setattr(pv_bias_report, "summarise_pv_bias",
                        lambda window_days: {"ok": True, "n_paired": 0,
                                              "headline": "PV bias 14 d: no paired samples yet"})
    assert daily_brief._pv_bias_line() is None


def test_morning_brief_renders_headline_when_data_present(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.analytics import daily_brief
    from src.analytics import pv_bias_report

    headline = "PV bias 14 d: am +0.18 kW under, pm −0.05 kW over (n=160)"
    monkeypatch.setattr(pv_bias_report, "summarise_pv_bias",
                        lambda window_days: {"ok": True, "n_paired": 160, "headline": headline})
    assert daily_brief._pv_bias_line() == headline
