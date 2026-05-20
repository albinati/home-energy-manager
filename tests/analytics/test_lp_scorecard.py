"""Tests for ``src/analytics/lp_scorecard.py`` — the LP optimisation scorecard.

The scorecard composes data from three DB sources (lp_solution_snapshot,
pv_realtime_history, forecast_skill_log) into a single structured report
graded A-D. These tests cover the structural shape + each section's
None-able fallback, plus the composite grade decision matrix.
"""
from __future__ import annotations

from datetime import date

import pytest

from src import db
from src.analytics import lp_scorecard


@pytest.fixture(autouse=True)
def _init_db() -> None:
    db.init_db()


# ---------------------------------------------------------------------------
# Empty-DB shape — every section None-safe + grade=N/A
# ---------------------------------------------------------------------------

def test_build_lp_scorecard_returns_complete_shape_on_empty_db() -> None:
    card = lp_scorecard.build_lp_scorecard(date(2026, 5, 19))
    assert card["day"] == "2026-05-19"
    assert "forecast_accuracy" in card
    assert "dispatch_accuracy" in card
    assert "economic_value" in card
    assert "grade" in card
    # Empty DB → all sections degrade cleanly
    assert card["forecast_accuracy"] == {"available": False}
    assert card["dispatch_accuracy"]["n_slots_with_plan"] == 0
    assert card["dispatch_accuracy"]["n_slots_with_real"] == 0
    assert card["grade"] == "N/A"


# ---------------------------------------------------------------------------
# Grade matrix — based on dispatch accuracy + economic value
# ---------------------------------------------------------------------------

def test_grade_A_when_high_accuracy_and_avoided_cost_positive() -> None:
    dispatch = {
        "import_accuracy_pct": 95.0,
        "export_accuracy_pct": 92.0,
        "charge_accuracy_pct": 90.0,
    }
    economic = {"lp_avoided_cost_p": 50.0}
    assert lp_scorecard._compute_grade(dispatch, economic) == "A"


def test_grade_B_when_moderate_accuracy_and_avoided_cost_positive() -> None:
    dispatch = {
        "import_accuracy_pct": 80.0,
        "export_accuracy_pct": 78.0,
        "charge_accuracy_pct": 75.0,
    }
    economic = {"lp_avoided_cost_p": 10.0}
    assert lp_scorecard._compute_grade(dispatch, economic) == "B"


def test_grade_C_when_economic_value_positive_but_accuracy_lower() -> None:
    dispatch = {
        "import_accuracy_pct": 65.0,
        "export_accuracy_pct": 62.0,
        "charge_accuracy_pct": 60.0,
    }
    economic = {"lp_avoided_cost_p": 5.0}
    assert lp_scorecard._compute_grade(dispatch, economic) == "C"


def test_grade_D_when_accuracy_low_and_overspent() -> None:
    dispatch = {
        "import_accuracy_pct": 40.0,
        "export_accuracy_pct": 30.0,
        "charge_accuracy_pct": 45.0,
    }
    economic = {"lp_avoided_cost_p": -20.0}
    assert lp_scorecard._compute_grade(dispatch, economic) == "D"


def test_grade_NA_when_no_accuracy_data() -> None:
    """When all three accuracy pcts are None (plan kWh ~0), grade is N/A."""
    dispatch = {
        "import_accuracy_pct": None,
        "export_accuracy_pct": None,
        "charge_accuracy_pct": None,
    }
    economic = {"lp_avoided_cost_p": 100.0}
    assert lp_scorecard._compute_grade(dispatch, economic) == "N/A"


def test_grade_C_when_accuracy_ok_but_economic_value_missing() -> None:
    """Without lp_avoided_cost_p, grade falls back to C tier (accuracy threshold only)."""
    dispatch = {
        "import_accuracy_pct": 70.0,
        "export_accuracy_pct": 65.0,
    }
    economic: dict = {}
    assert lp_scorecard._compute_grade(dispatch, economic) == "C"


# ---------------------------------------------------------------------------
# Effective-plan-export semantics (strict_savings filter awareness)
# ---------------------------------------------------------------------------

def test_effective_plan_export_zero_when_not_peak_export_dispatched() -> None:
    """LP variable ``export_kwh=1.84`` doesn't count when the dispatch
    classifier downgraded the slot to ``standard``."""
    plan_row = {"export_kwh": 1.84, "dispatched_kind": "standard"}
    assert lp_scorecard._effective_plan_export(plan_row) == 0.0


def test_effective_plan_export_uses_export_kwh_when_peak_export_dispatched() -> None:
    plan_row = {"export_kwh": 1.84, "dispatched_kind": "peak_export"}
    assert lp_scorecard._effective_plan_export(plan_row) == 1.84


def test_effective_plan_export_handles_missing_dispatched_kind() -> None:
    """No dispatch_decisions row → conservative: don't count as export."""
    plan_row = {"export_kwh": 1.84, "dispatched_kind": None}
    assert lp_scorecard._effective_plan_export(plan_row) == 0.0
