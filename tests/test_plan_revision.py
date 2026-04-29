"""Tests for the PLAN_REVISION emit gate (V12)."""
from __future__ import annotations

import pytest


def _emit(monkeypatch):
    """Helper: capture notify_plan_revision calls and return the list."""
    captured: list[tuple] = []
    from src import notifier

    def _fake(body, *, trigger_reason=None):
        captured.append((body, trigger_reason))

    monkeypatch.setattr(notifier, "notify_plan_revision", _fake)
    return captured


def test_no_emit_on_plan_push_trigger(monkeypatch):
    """Plan-push has its own notify path; suppress the duplicate."""
    from src.scheduler import runner

    captured = _emit(monkeypatch)
    runner._maybe_notify_plan_revision(
        {"max_soc_delta_pct": 50.0, "sum_grid_delta_kwh": 5.0, "sum_charge_delta_kwh": 5.0, "overlap_count": 8},
        trigger_reason="plan_push",
    )
    assert captured == []


def test_no_emit_when_delta_below_threshold(monkeypatch):
    """Non-cron trigger but the deltas are too small — silence is correct."""
    from src.scheduler import runner

    captured = _emit(monkeypatch)
    runner._maybe_notify_plan_revision(
        {"max_soc_delta_pct": 1.0, "sum_grid_delta_kwh": 0.1, "sum_charge_delta_kwh": 0.0, "overlap_count": 8},
        trigger_reason="forecast_revision",
    )
    assert captured == []


def test_emit_on_material_soc_change(monkeypatch):
    """Material SoC delta from a forecast-revision trigger → one ping."""
    from src.scheduler import runner

    monkeypatch.setattr(runner.config, "PLAN_REVISION_MIN_SOC_DELTA_PERCENT", 10.0, raising=False)
    monkeypatch.setattr(runner.config, "PLAN_REVISION_MIN_GRID_DELTA_KWH", 1.0, raising=False)

    captured = _emit(monkeypatch)
    runner._maybe_notify_plan_revision(
        {"max_soc_delta_pct": 15.0, "sum_grid_delta_kwh": 0.1, "sum_charge_delta_kwh": 0.0, "overlap_count": 8},
        trigger_reason="forecast_revision",
    )
    assert len(captured) == 1
    body, trigger = captured[0]
    assert "forecast_revision" in body
    assert "SoC max-Δ=15.0%" in body
    assert trigger == "forecast_revision"


def test_emit_on_material_grid_change(monkeypatch):
    """Material grid delta — even with small SoC delta — also pings."""
    from src.scheduler import runner

    monkeypatch.setattr(runner.config, "PLAN_REVISION_MIN_SOC_DELTA_PERCENT", 10.0, raising=False)
    monkeypatch.setattr(runner.config, "PLAN_REVISION_MIN_GRID_DELTA_KWH", 1.0, raising=False)

    captured = _emit(monkeypatch)
    runner._maybe_notify_plan_revision(
        {"max_soc_delta_pct": 1.0, "sum_grid_delta_kwh": 2.5, "sum_charge_delta_kwh": 0.0, "overlap_count": 8},
        trigger_reason="tier_boundary",
    )
    assert len(captured) == 1
    body, trigger = captured[0]
    assert trigger == "tier_boundary"
    assert "grid Δ=2.50 kWh" in body


def test_no_emit_when_delta_is_none(monkeypatch):
    """No-op safe when there's no prior plan to diff against."""
    from src.scheduler import runner

    captured = _emit(monkeypatch)
    runner._maybe_notify_plan_revision(None, trigger_reason="soc_drift")
    assert captured == []
