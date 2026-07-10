"""Plan-vs-uploaded-schedule coherence audit (#670).

``dispatch_decisions`` only audits the peak-export robustness filter; the
lossy-but-legitimate Fox translations (8-group compression squash, trivial
SelfUse drop, 23 h horizon trim) had no audit trail. These tests pin the
per-slot summary computed at Fox upload time:

* intact plan → 100 % match (trivial SelfUse drops count as matches);
* a ForceCharge slot squashed into a SelfUse group → mismatch + severe;
* horizon-trimmed D+1 slots → ``horizon_trim``, never severe;
* cap-truncated tail (replan scheduled) → ``group_cap_truncation``, never severe.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src import db
from src.config import config
from src.foxess.models import SchedulerGroup
from src.scheduler import optimizer
from src.scheduler.lp_dispatch import summarize_plan_dispatch_coherence
from src.scheduler.optimizer import TZ, HalfHourSlot, _merge_fox_groups

T0 = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)  # fixed date — no midnight flakes
RESERVE = int(config.MIN_SOC_RESERVE_PERCENT)


def _slot(i: int, kind: str, price: float = 10.0) -> HalfHourSlot:
    start = T0 + i * timedelta(minutes=30)
    return HalfHourSlot(
        start_utc=start,
        end_utc=start + timedelta(minutes=30),
        price_pence=price,
        kind=kind,
        lp_grid_import_w=3000 if kind in ("cheap", "negative") else None,
        target_soc_pct=95 if kind in ("cheap", "negative") else None,
    )


def _fc_group_for(slot: HalfHourSlot) -> SchedulerGroup:
    # Production convention: half-hour ends are stored :30 = EXCLUSIVE
    # (_merge_fox_groups only rewrites on-the-hour ends to :59 inclusive).
    ls = slot.start_utc.astimezone(TZ())
    le = ls + timedelta(minutes=30)
    return SchedulerGroup(
        start_hour=ls.hour, start_minute=ls.minute,
        end_hour=le.hour, end_minute=le.minute,
        work_mode="ForceCharge", min_soc_on_grid=RESERVE, fd_soc=95, fd_pwr=3000,
    )


def test_intact_plan_is_fully_coherent(monkeypatch) -> None:
    """Groups that survive merge untouched → 100 % match; the trivial-SelfUse
    drop is behaviour-neutral (firmware default) and must NOT create noise."""
    monkeypatch.setattr(config, "FOX_SKIP_TRIVIAL_SELFUSE_GROUPS", True)
    slots = [_slot(0, "cheap"), _slot(1, "cheap"), _slot(2, "standard"), _slot(3, "standard")]
    groups = _merge_fox_groups(slots, max_groups=8)
    summary = summarize_plan_dispatch_coherence(slots, groups)
    assert summary["total_slots"] == 4
    assert summary["matched"] == 4
    assert summary["mismatched"] == 0
    assert summary["severe_count"] == 0
    assert summary["trivial_selfuse_drops"] == 2  # dropped groups counted as matches


def test_squashed_force_charge_tail_is_severe() -> None:
    """A compression squash that converts ForceCharge into SelfUse is planned
    grid-charge silently lost — the exact loss class #670 makes visible."""
    slots = [_slot(0, "cheap"), _slot(1, "cheap"), _slot(2, "standard")]
    groups = [  # e.g. Camada 3 squashed the FC pair into an all-day SelfUse
        SchedulerGroup(0, 0, 23, 59, work_mode="SelfUse", min_soc_on_grid=RESERVE),
    ]
    summary = summarize_plan_dispatch_coherence(slots, groups)
    assert summary["matched"] == 1  # the standard slot matches SelfUse
    assert summary["mismatched"] == 2
    assert summary["severe_count"] == 2
    assert all(s["expected"] == "ForceCharge" and s["actual"] == "SelfUse" for s in summary["severe"])
    assert summary["mismatches"] == [
        {"expected": "ForceCharge", "actual": "SelfUse", "reason": "group_cap_compression", "count": 2},
    ]


def test_half_hour_boundary_slot_attributed_to_successor_group() -> None:
    """Regression (#675 review): _merge_fox_groups stores half-hour group ends
    as :30 = EXCLUSIVE (only on-the-hour ends become :59 inclusive). A
    non-SelfUse group ending :30 with a different-mode successor starting :30
    must NOT swallow the successor's first slot — an inclusive lookup produced
    recurring false group_cap_compression mismatches and occasional false
    SEVERE (e.g. solar_charge hold ending, ForceCharge/Discharge starting)."""
    slots = [_slot(0, "solar_charge"), _slot(1, "cheap"), _slot(2, "cheap")]
    groups = _merge_fox_groups(slots, max_groups=8)
    # Preconditions: the REAL builder kept both windows, back-to-back at :30.
    assert [g.work_mode for g in groups] == ["SelfUse", "ForceCharge"]
    assert (groups[0].end_hour, groups[0].end_minute) == (
        groups[1].start_hour, groups[1].start_minute,
    ), "predecessor must end exactly where the successor starts (:30 exclusive)"
    summary = summarize_plan_dispatch_coherence(slots, groups)
    assert summary["mismatched"] == 0, summary["mismatches"]
    assert summary["severe_count"] == 0
    assert summary["matched"] == 3


def test_horizon_trimmed_slots_counted_not_severe(monkeypatch) -> None:
    """D+1 slots past the daily-cyclic dispatch cutoff are re-dispatched by the
    next re-solve — legitimate loss, tallied under horizon_trim, never severe.
    Their minute-of-day collides with the D0 group; the lookup must not match."""
    monkeypatch.setattr(config, "FOX_DISPATCH_HORIZON_HOURS", 23.0)
    slots = [_slot(0, "cheap"), _slot(1, "standard"), _slot(48, "cheap")]  # +24 h FC
    groups = [_fc_group_for(slots[0])]
    summary = summarize_plan_dispatch_coherence(slots, groups)
    assert summary["severe_count"] == 0
    assert summary["matched"] == 2  # D0 FC + trivial standard
    assert summary["mismatches"] == [
        {"expected": "ForceCharge", "actual": "absent", "reason": "horizon_trim", "count": 1},
    ]


def test_cap_truncated_tail_not_severe_but_bare_drop_is() -> None:
    """A slot dropped by the 8-group-cap truncation has a scheduled MPC replan
    → not severe. The same absent FC BEFORE the replan boundary is severe."""
    slots = [_slot(0, "cheap"), _slot(2, "cheap"), _slot(6, "cheap")]
    groups = [_fc_group_for(slots[0])]
    replan_at = T0 + timedelta(hours=2)  # slot at +1 h uncovered, boundary before +3 h
    summary = summarize_plan_dispatch_coherence(slots, groups, replan_at_utc=replan_at)
    assert summary["matched"] == 1
    assert summary["severe_count"] == 1  # only the pre-replan bare drop (+1 h)
    assert summary["severe"][0]["reason"] == "other"
    assert {(m["reason"], m["count"]) for m in summary["mismatches"]} == {
        ("other", 1),
        ("group_cap_truncation", 1),
    }


def test_e2e_optimizer_writes_coherence_action_log(monkeypatch) -> None:
    """The real LP path must write ONE ``plan_dispatch_coherence`` action_log
    event per upload — the wiring is wrapped in try/except, so only an e2e
    assertion catches a silently-swallowed regression."""
    from src.scheduler.lp_optimizer import LpPlan

    tariff = "E-1R-AGILE-24-10-01-C"
    db.init_db()
    monkeypatch.setattr(config, "BULLETPROOF_TIMEZONE", "Europe/London")
    monkeypatch.setattr(config, "OCTOPUS_TARIFF_CODE", tariff)
    monkeypatch.setattr(config, "OPTIMIZER_BACKEND", "lp")
    monkeypatch.setattr(config, "OPENCLAW_READ_ONLY", True)
    now = datetime(2026, 5, 20, 18, 0, tzinfo=UTC)  # fixed — no midnight flakes
    monkeypatch.setattr(optimizer, "_now_utc", lambda: now)

    rows = []
    vf = datetime(2026, 5, 20, 0, 0, tzinfo=UTC)
    for _ in range(96):  # two seeded days, flat 10p
        vt = vf + timedelta(minutes=30)
        rows.append({
            "valid_from": vf.isoformat().replace("+00:00", "Z"),
            "valid_to": vt.isoformat().replace("+00:00", "Z"),
            "value_inc_vat": 10.0,
        })
        vf = vt
    db.save_agile_rates(rows, tariff)

    def _stub_solve(*, slot_starts_utc, price_pence, **_kw):
        n = len(slot_starts_utc)
        plan = LpPlan(ok=True, status="Optimal", objective_pence=0.0)
        plan.slot_starts_utc = list(slot_starts_utc)
        plan.price_pence = list(price_pence)
        plan.temp_outdoor_c = [12.0] * n
        for f in ("import_kwh", "export_kwh", "battery_charge_kwh", "battery_discharge_kwh",
                  "pv_use_kwh", "pv_curtail_kwh", "dhw_electric_kwh", "space_electric_kwh",
                  "lwt_offset_c"):
            setattr(plan, f, [0.0] * n)
        plan.tank_temp_c = [45.0] * (n + 1)
        plan.soc_kwh = [5.0] * (n + 1)
        return plan

    monkeypatch.setattr("src.scheduler.lp_optimizer.solve_lp", _stub_solve)
    result = optimizer.run_optimizer(fox=None, daikin=None)
    assert result.get("optimizer_backend") == "lp", f"LP path not taken: {result}"

    logged = db.get_action_logs(device="foxess", action="plan_dispatch_coherence", limit=5)
    assert len(logged) == 1, f"expected exactly one coherence event, got {len(logged)}"
    p = logged[0]["params"]
    assert p["total_slots"] > 0
    assert p["severe_count"] == 0
    assert p["mismatched"] == 0  # flat all-SelfUse plan translates losslessly
    assert logged[0]["result"] == "success"
    assert p["fox_uploaded"] is False  # read-only run — audit still fires
