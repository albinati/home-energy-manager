"""Smoke test for OPTIMIZER_BACKEND=heuristic — catches silent rot in the fallback path.

The heuristic backend is the safety net when PuLP/CBC fails to solve. No prod
config sets it, so it can rot without anyone noticing — until the day PuLP fails
and the rot bites. This test seeds a realistic 48-slot day, runs the heuristic,
and asserts a non-empty plan comes back.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src import db
from src.config import config as app_config
from src.scheduler import optimizer

TARIFF = "E-1R-AGILE-TEST-HEURISTIC"


@pytest.fixture(autouse=True)
def _init_db() -> None:
    db.init_db()


@pytest.fixture(autouse=True)
def _heuristic_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_config, "BULLETPROOF_TIMEZONE", "Europe/London")
    monkeypatch.setattr(app_config, "OCTOPUS_TARIFF_CODE", TARIFF)
    monkeypatch.setattr(app_config, "OPTIMIZER_BACKEND", "heuristic")
    monkeypatch.setattr(app_config, "OPENCLAW_READ_ONLY", True)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _seed_realistic_day(start: datetime) -> None:
    """48 slots with a clear cheap-night / peak-evening pattern."""
    rows = []
    vf = start
    for i in range(48):
        hour = (vf.hour + 0.5 * (i % 2))  # 0.5h slot spacing
        if 1 <= vf.hour < 5:
            price = -2.0  # negative overnight
        elif 5 <= vf.hour < 16:
            price = 10.0  # standard
        elif 16 <= vf.hour < 19:
            price = 35.0  # peak
        else:
            price = 12.0
        vt = vf + timedelta(minutes=30)
        rows.append({"valid_from": _iso(vf), "valid_to": _iso(vt), "value_inc_vat": price})
        vf = vt
    db.save_agile_rates(rows, TARIFF)


def test_heuristic_fallback_returns_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    """A realistic 48-slot day must produce a non-empty heuristic plan."""
    now = datetime(2026, 4, 22, 18, 0, tzinfo=UTC)
    monkeypatch.setattr(optimizer, "_now_utc", lambda: now)
    _seed_realistic_day(datetime(2026, 4, 22, 0, 0, tzinfo=UTC))

    result = optimizer.run_optimizer(fox=None, daikin=None)

    assert isinstance(result, dict), f"expected dict, got {type(result)}"
    assert result.get("ok") is True, f"heuristic failed: {result.get('error')}"
    assert result.get("optimizer_backend") == "heuristic", (
        f"expected heuristic backend, got {result.get('optimizer_backend')}"
    )
    # The heuristic must emit slot counts (proves it built and classified slots).
    counts = result.get("counts") or {}
    total_slots = sum(int(v) for v in counts.values())
    assert total_slots > 0, f"heuristic produced 0 slots: counts={counts}"
    # And it should have generated at least one Daikin action for our peak window.
    assert isinstance(result.get("daikin_actions"), int), (
        f"missing daikin_actions count: {result}"
    )


def test_heuristic_caps_fox_dispatch_at_24h(monkeypatch: pytest.MonkeyPatch) -> None:
    """Issue #208: when both today's and tomorrow's Agile rates are present, the
    heuristic must NOT emit Fox V3 groups for D+1 slots that share an
    hour-of-day with D+0 slots (Fox V3 is daily-cyclic — overlap = duplicates
    on the inverter). Mirrors the LP-side cap from db8a59c."""
    now = datetime(2026, 4, 22, 14, 0, tzinfo=UTC)
    monkeypatch.setattr(optimizer, "_now_utc", lambda: now)
    # Seed BOTH days so the plan window is the full 48 h horizon.
    _seed_realistic_day(datetime(2026, 4, 22, 0, 0, tzinfo=UTC))
    _seed_realistic_day(datetime(2026, 4, 23, 0, 0, tzinfo=UTC))

    captured: dict[str, list] = {}

    def _capture_merge(slots, **kwargs):
        captured["slots"] = list(slots)
        # call through to keep the rest of the code path sane
        return _real_merge(slots, **kwargs)

    _real_merge = optimizer._merge_fox_groups
    monkeypatch.setattr(optimizer, "_merge_fox_groups", _capture_merge)

    result = optimizer.run_optimizer(fox=None, daikin=None)
    assert result.get("ok") is True

    slots = captured["slots"]
    assert slots, "heuristic produced no slots to merge"
    # 24 h = 48 half-hour slots; cap must hold.
    span_hours = (slots[-1].end_utc - slots[0].start_utc).total_seconds() / 3600
    assert span_hours <= 24.0, (
        f"heuristic dispatch span exceeded 24 h: {span_hours:.2f} h "
        f"({slots[0].start_utc.isoformat()} -> {slots[-1].end_utc.isoformat()}) — "
        f"would emit overlapping Fox V3 groups (#208)"
    )


def test_heuristic_uses_shared_upload_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """The heuristic must route through ``upload_fox_if_operational`` — that's
    where the overlap-detection backstop lives (#208). If a future refactor
    inlines the upload again, the backstop won't fire."""
    now = datetime(2026, 4, 22, 14, 0, tzinfo=UTC)
    monkeypatch.setattr(optimizer, "_now_utc", lambda: now)
    _seed_realistic_day(datetime(2026, 4, 22, 0, 0, tzinfo=UTC))

    from src.scheduler import lp_dispatch

    calls: list[Any] = []

    def _capture_upload(fox, groups):  # type: ignore[no-untyped-def]
        calls.append(groups)
        return False

    monkeypatch.setattr(lp_dispatch, "upload_fox_if_operational", _capture_upload)
    monkeypatch.setattr(optimizer, "upload_fox_if_operational", _capture_upload, raising=False)

    fox = type("FakeFox", (), {"api_key": "x"})()
    result = optimizer.run_optimizer(fox=fox, daikin=None)
    assert result.get("ok") is True
    assert calls, "upload_fox_if_operational was never called by heuristic"


# ---------------------------------------------------------------------------
# Safety invariant — heuristic must NOT ship Fox V3 groups with the LP-unaware
# default ``(fdPwr=FOX_FORCE_CHARGE_NORMAL_PWR, fdSoc=95)`` combination that
# destructively grid-overcharges the battery.
#
# Background: this was the prod bug audited 2026-05-18 (£0.35-1.30/day waste,
# ~110 events/30d). PR #338 made the LP-fallback path skip the heuristic
# entirely, so the bug can no longer fire automatically. But the heuristic
# backend is still callable via ``OPTIMIZER_BACKEND=heuristic`` — if it ever
# gets re-enabled, the same destructive groups would ship. This test exercises
# that path explicitly and asserts the safety invariant.
#
# Marked ``xfail(strict=True)`` because the heuristic currently DOES emit the
# dangerous defaults — it has no LP-derived ``lp_grid_import_w`` /
# ``target_soc_pct`` per slot, so ``_slot_fox_tuple`` falls back to the
# ``FOX_FORCE_CHARGE_NORMAL_PWR=3000`` / ``fdSoc=95`` literals at
# ``src/scheduler/optimizer.py:344-347``. Flip strict=True back to ``False``
# (or remove the xfail entirely) once the heuristic is taught to compute safe
# per-slot params or replaced by a Self-Use-only emitter.
# ---------------------------------------------------------------------------

# pyrefly: ignore  # pytest marker import is dynamic
_HEURISTIC_DANGEROUS_FDPWR_W = 3000  # matches config.FOX_FORCE_CHARGE_NORMAL_PWR default
_HEURISTIC_DANGEROUS_FDSOC_PCT = 95


def _seed_cheap_peak_only_day(start: datetime) -> None:
    """48 slots with cheap-overnight + peak-evening but NO negative slots.

    This is the prod profile that reliably reproduced the bug — see upload
    #775 in the 2026-05-18 audit (``neg=0 cheap=25 std=51 peak=20``). Without
    a negative slot adjacent to a cheap slot, ``_merge_adjacent_force_charge_rows``
    has nothing to blend into the cheap window, so the heuristic's default
    ``(fdPwr=3000, fdSoc=95)`` survives unmerged through to the upload payload.
    """
    rows = []
    vf = start
    for _ in range(48):
        if 1 <= vf.hour < 5:
            price = 8.0   # cheap (but POSITIVE — no negative-price merging)
        elif 5 <= vf.hour < 16:
            price = 15.0  # standard
        elif 16 <= vf.hour < 19:
            price = 35.0  # peak
        else:
            price = 18.0
        vt = vf + timedelta(minutes=30)
        rows.append({"valid_from": _iso(vf), "valid_to": _iso(vt), "value_inc_vat": price})
        vf = vt
    db.save_agile_rates(rows, TARIFF)


@pytest.mark.xfail(
    strict=True,
    reason="Heuristic emits ForceCharge[fdPwr=3000, fdSoc=95] defaults because it has "
    "no LP-derived per-slot hints (see PR #338 + project_heuristic_fox_dispatch_bug "
    "memory). Tracked as follow-up: either rewire heuristic to Self-Use-only on the "
    "Fox surface, or compute LP-aware fdPwr/fdSoc inside the heuristic. When fixed, "
    "remove this xfail and the test should pass.",
)
def test_heuristic_does_not_ship_dangerous_force_charge_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The heuristic backend must never upload Fox V3 ForceCharge groups with
    the ``(fdPwr=3000, fdSoc=95)`` default combination — those drive the
    inverter to grid-charge at full power until 95 % SoC, regardless of what
    economics the LP would have planned. This is the prod bug audited
    2026-05-18 (£0.35-1.30/day waste).

    Test uses a cheap-overnight + peak-evening profile with NO negative
    slots, which is what reliably reproduced the bug in prod (see upload
    #775). With negative slots adjacent, the merge would blend pwr/soc
    values and the literal defaults wouldn't survive — that's why the
    prior ``_seed_realistic_day`` profile didn't catch this.
    """
    # Pick a "now" that aligns with the cheap window so the 24h dispatch cap
    # keeps the cheap slots in the upload. Heuristic runs from "now" forward.
    now = datetime(2026, 4, 22, 0, 0, tzinfo=UTC)
    monkeypatch.setattr(optimizer, "_now_utc", lambda: now)
    _seed_cheap_peak_only_day(datetime(2026, 4, 22, 0, 0, tzinfo=UTC))

    from src.scheduler import lp_dispatch

    captured_groups: list[Any] = []

    def _capture_upload(fox, groups):  # type: ignore[no-untyped-def]
        captured_groups.extend(groups)
        return False

    monkeypatch.setattr(lp_dispatch, "upload_fox_if_operational", _capture_upload)
    monkeypatch.setattr(optimizer, "upload_fox_if_operational", _capture_upload, raising=False)

    fox = type("FakeFox", (), {"api_key": "x"})()
    result = optimizer.run_optimizer(fox=fox, daikin=None)
    assert result.get("ok") is True, f"heuristic backend failed to produce a plan: {result}"
    assert captured_groups, "no V3 groups were uploaded — test setup broken"

    dangerous = [
        g for g in captured_groups
        if getattr(g, "work_mode", None) == "ForceCharge"
        and getattr(g, "fd_pwr", None) == _HEURISTIC_DANGEROUS_FDPWR_W
        and getattr(g, "fd_soc", None) == _HEURISTIC_DANGEROUS_FDSOC_PCT
    ]
    assert not dangerous, (
        f"Heuristic shipped {len(dangerous)} ForceCharge group(s) with the "
        f"dangerous default (fdPwr={_HEURISTIC_DANGEROUS_FDPWR_W} W, "
        f"fdSoc={_HEURISTIC_DANGEROUS_FDSOC_PCT} %). Fox would grid-charge at "
        f"full power until 95 % SoC on every cycle, ignoring LP economics. "
        f"Offending groups: {dangerous}"
    )
