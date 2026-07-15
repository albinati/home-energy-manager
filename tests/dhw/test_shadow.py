"""The economic shadow and its enable gate (#714).

The gate is the last thing between the regime and production, so its refusals matter as
much as its approvals: cheaper is not enough — a single cold shower disqualifies, and so
does a thin run of days or a quota breach.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from src import db as _db
from src.config import config
from src.dhw import shadow


@pytest.fixture()
def tmp_db(tmp_path, monkeypatch):
    path = tmp_path / "shadow.db"
    monkeypatch.setenv("DB_PATH", str(path))
    monkeypatch.setattr(_db, "_db_path", lambda: path)
    _db.init_db()
    monkeypatch.setattr(config, "BULLETPROOF_TIMEZONE", "UTC", raising=False)
    monkeypatch.setattr(config, "DHW_LP_OWNED_GATE_MIN_DAYS", 14, raising=False)
    monkeypatch.setattr(config, "DHW_LP_OWNED_GATE_MIN_SAVING_PENCE", 3.0, raising=False)
    monkeypatch.setattr(config, "DHW_LP_OWNED_GATE_MAX_ROWS", 6, raising=False)
    return path


def _seed(day: str, *, delta_p: float, deficit: float = 0.0, rows: int = 4):
    _db.insert_dhw_shadow({
        "run_at_utc": f"{day}T12:00:00+00:00",
        "day": day, "cost_pinned_p": 100.0, "cost_lp_owned_p": 100.0 + delta_p,
        "delta_p": delta_p, "comfort_deficit_c": deficit, "n_tank_rows": rows,
    })


def _days(n: int) -> list[str]:
    base = datetime.now(UTC).date() - timedelta(days=n)
    return [(base + timedelta(days=i)).isoformat() for i in range(n)]


# ---------------------------------------------------------------------------
# The cost metric
# ---------------------------------------------------------------------------


def test_grid_cost_is_imports_bought_minus_exports_sold():
    plan = SimpleNamespace(
        import_kwh=[1.0, 2.0, 0.0], export_kwh=[0.0, 0.0, 1.0],
    )
    cost = shadow.grid_cost_pence(plan, [10.0, 20.0, 30.0], [5.0, 5.0, 15.0])
    assert cost == pytest.approx(1.0 * 10 + 2.0 * 20 - 1.0 * 15)  # 35


def test_comfort_deficit_reads_the_tank_against_the_floor(monkeypatch):
    from zoneinfo import ZoneInfo

    starts = [datetime(2026, 7, 8, 20, 0, tzinfo=UTC)]  # a 20:00 shower boundary
    plan = SimpleNamespace(
        dhw_lp_owned=True, slot_starts_utc=starts, tank_temp_c=[41.0, 41.0],
    )
    # A tank at 41 against a 45 floor is 4 °C short — a cold shower.
    d = shadow.comfort_deficit_c(plan, ZoneInfo("UTC"), "normal")
    assert d == pytest.approx(4.0, abs=0.5)

    plan.tank_temp_c = [46.0, 46.0]  # hot enough
    assert shadow.comfort_deficit_c(plan, ZoneInfo("UTC"), "normal") == 0.0


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def test_gate_opens_on_a_clean_cheaper_run(tmp_db):
    for d in _days(14):
        _seed(d, delta_p=-10.0)  # 10p/day cheaper, comfort-clean
    gate = shadow.evaluate_gate()
    assert gate["ready"] is True
    assert gate["median_saving_pence"] == pytest.approx(10.0)
    assert gate["comfort_breach_days"] == 0


def test_a_single_cold_shower_disqualifies_the_whole_run(tmp_db):
    """Comfort is not for sale. Fourteen cheaper days, but one left the tank cold — the
    gate must refuse, no matter how large the saving."""
    days = _days(14)
    for d in days:
        _seed(d, delta_p=-15.0)
    _seed(days[7], delta_p=-15.0, deficit=3.0)  # one breach
    gate = shadow.evaluate_gate()
    assert gate["ready"] is False
    assert gate["comfort_breach_days"] == 1


def test_cheaper_but_not_by_enough_does_not_open(tmp_db):
    for d in _days(14):
        _seed(d, delta_p=-1.0)  # cheaper, but below the 3p bar
    assert shadow.evaluate_gate()["ready"] is False


def test_a_thin_run_does_not_open_however_good(tmp_db):
    for d in _days(5):
        _seed(d, delta_p=-20.0)
    gate = shadow.evaluate_gate()
    assert gate["ready"] is False
    assert gate["days"] == 5


def test_a_quota_breach_blocks_the_gate(tmp_db):
    for d in _days(14):
        _seed(d, delta_p=-10.0, rows=9)  # cheaper + clean, but too many Daikin rows
    assert shadow.evaluate_gate()["ready"] is False


def test_the_suggestion_fires_once(tmp_db, monkeypatch):
    for d in _days(14):
        _seed(d, delta_p=-10.0)
    monkeypatch.setattr(config, "DHW_LP_OWNED_ENABLED", False, raising=False)

    sent = []
    import src.notifier as notifier
    monkeypatch.setattr(notifier, "notify",
                        lambda *a, **k: sent.append(a), raising=False)

    shadow.maybe_suggest_enable()
    shadow.maybe_suggest_enable()  # second call must be a no-op
    assert len(sent) == 1


def test_no_suggestion_when_already_enabled(tmp_db, monkeypatch):
    for d in _days(14):
        _seed(d, delta_p=-10.0)
    monkeypatch.setattr(config, "DHW_LP_OWNED_ENABLED", True, raising=False)
    sent = []
    import src.notifier as notifier
    monkeypatch.setattr(notifier, "notify", lambda *a, **k: sent.append(a), raising=False)
    shadow.maybe_suggest_enable()
    assert sent == []
