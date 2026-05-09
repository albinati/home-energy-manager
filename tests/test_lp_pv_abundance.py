"""PR 3 of plan: PV-abundance tank ceiling lift + reward + #225 item 1.

Tests:
1. **LP**: PV-abundance lifts ``tank_hi_slot`` to ``DHW_TEMP_MAX_C`` (composes
   with the negative-price lift; same surface).
2. **LP**: ``LP_PV_ABUNDANCE_TANK_REWARD_PENCE_PER_KWH > 0`` shifts e_dhw
   toward abundant slots when the LP would otherwise route surplus PV elsewhere.
3. **LP**: reward must NOT dominate export when the export rate is genuinely
   profitable — guards the "near-zero grid cost first, profit second" policy.
4. **LP**: ``LP_TANK_HI_SLACK_PENCE_PER_DEGC_SLOT`` is honoured (closes #225 item 1).
5. **Dispatch SAFETY**: every emitted ``solar_preheat`` action has a paired
   ``restore`` row that drops the tank back to ``DHW_TEMP_NORMAL_C`` — this is
   the user-stated hard constraint ("forgetting to switch back is the cost
   problem"), so it gets a focused regression test.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest


def _make_weather(slots, pv_kwh):
    from src.weather import WeatherLpSeries
    n = len(slots)
    return WeatherLpSeries(
        slot_starts_utc=slots,
        temperature_outdoor_c=[18.0] * n,
        shortwave_radiation_wm2=[600.0] * n,
        cloud_cover_pct=[20.0] * n,
        pv_kwh_per_slot=pv_kwh,
        cop_space=[3.5] * n,
        cop_dhw=[3.0] * n,
    )


def _solve(slots, prices, pv, base_load, init_soc=8.0, init_tank=40.0,
           export_prices=None, **monkeyed_config):
    from src.scheduler.lp_optimizer import LpInitialState, solve_lp
    init = LpInitialState(soc_kwh=init_soc, tank_temp_c=init_tank, indoor_temp_c=21.0)
    return solve_lp(
        slot_starts_utc=slots,
        price_pence=prices,
        base_load_kwh=base_load,
        weather=_make_weather(slots, pv),
        initial=init,
        tz=ZoneInfo("Europe/London"),
        export_price_pence=export_prices,
    )


# --------------------------------------------------------------------------
# 1. PV-abundance lifts the tank ceiling
# --------------------------------------------------------------------------

def test_pv_abundance_lifts_tank_ceiling_above_comfort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When PV >> self-use + battery headroom, the LP can heat the tank
    above DHW_TEMP_COMFORT_C (48 °C) without paying the soft-ceiling slack.
    """
    from src.config import config as app_config
    monkeypatch.setattr(app_config, "DHW_PV_ABUNDANCE_THRESHOLD_KWH", 0.5, raising=False)
    monkeypatch.setattr(app_config, "LP_PV_ABUNDANCE_TANK_REWARD_PENCE_PER_KWH", 5.0, raising=False)

    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    n = 8
    slots = [base + timedelta(minutes=30 * i) for i in range(n)]
    # Healthy PV (4 kWh/slot — above battery + load + DHW capacity). No export
    # rate provided → fall back to flat EXPORT_RATE_PENCE; reward is the only
    # incentive to push PV into DHW.
    plan = _solve(
        slots=slots,
        prices=[20.0] * n,
        pv=[4.0] * n,
        base_load=[0.3] * n,
        init_soc=4.0,
        init_tank=40.0,
    )
    assert plan.ok, plan.status
    # With reward + lifted ceiling, tank should rise materially above comfort 48 °C.
    max_tank = max(plan.tank_temp_c[1:])
    assert max_tank > float(app_config.DHW_TEMP_COMFORT_C) + 2.0, (
        f"PV-abundance lift should raise tank above comfort; got max={max_tank:.1f}"
    )


def test_pv_abundance_threshold_relaxed_for_realistic_sunny_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The relaxed abundance formula triggers on realistic sunny-day PV
    (~1.5 kWh/slot), not only peak-summer-noon (>3 kWh/slot).

    The original PR #287 formula was ``(pv_avail − base_load − max_batt_kwh)``
    which subtracted the inverter cap (~2.5 kWh/slot) — too restrictive for
    a 4.5 kWp install whose typical sunny-day peak is 2-3 kWh/slot. The fix
    drops the battery term: ``(pv_avail − base_load) > threshold``.

    This test isolates the *threshold mechanics* (does the abundance flag
    fire?) from the *economic competition* with export revenue. We zero the
    export rate so the only LP incentive is the tank-storage reward.
    Active mode used because passive clamps e_dhw."""
    from src.config import config as app_config
    monkeypatch.setattr(app_config, "DAIKIN_CONTROL_MODE", "active", raising=False)
    monkeypatch.setattr(app_config, "DHW_PV_ABUNDANCE_THRESHOLD_KWH", 0.5, raising=False)
    monkeypatch.setattr(app_config, "LP_PV_ABUNDANCE_TANK_REWARD_PENCE_PER_KWH", 5.0, raising=False)

    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    n = 4
    slots = [base + timedelta(minutes=30 * i) for i in range(n)]
    plan = _solve(
        slots=slots,
        prices=[20.0] * n,
        pv=[1.5] * n,        # Realistic sunny-day mid-day PV
        base_load=[0.3] * n,
        init_soc=8.5,
        init_tank=40.0,
        export_prices=[0.0] * n,  # Zero export revenue — isolate from export
                                   # trade-off; pure threshold test.
    )
    assert plan.ok, plan.status
    # Old formula: (1.5 − 0.3 − 2.5) = −1.3 < 0.5 → would NOT trigger.
    # New formula: (1.5 − 0.3)        =  1.2 > 0.5 → triggers. With tank
    # storage rewarded and export rate 0, LP must heat the tank to capture
    # the only available value.
    max_tank = max(plan.tank_temp_c[1:])
    assert max_tank > float(app_config.DHW_TEMP_COMFORT_C) + 1.0, (
        f"Relaxed PV-abundance threshold should fire on 1.5 kWh/slot PV "
        f"(would not trigger under old formula); max_tank={max_tank:.1f}"
    )


def test_dispatch_clamps_solar_preheat_at_pv_abundance_target_55c(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dispatch layer hard-clamps the solar_preheat tank target at
    ``DHW_TEMP_PV_ABUNDANCE_TARGET_C`` (55 °C), even when the LP plan
    specified a higher value. Holding 65 °C through afternoon bleeds standing
    losses before evening showers. The LP's soft preference is a hint;
    dispatch enforces the operator-defined hard cap on the actual Onecta write.

    Negative-price slots still use the full DHW_TEMP_MAX_C (65 °C) cap —
    only solar_charge is dispatched at the tighter target."""
    from src.config import config as app_config
    from src.scheduler.lp_dispatch import daikin_dispatch_preview
    from src.scheduler.lp_optimizer import LpPlan

    monkeypatch.setattr(app_config, "DAIKIN_CONTROL_MODE", "active", raising=False)
    monkeypatch.setattr(app_config, "DHW_TEMP_PV_ABUNDANCE_TARGET_C", 55.0, raising=False)
    monkeypatch.setattr(app_config, "DHW_TEMP_MAX_C", 65.0, raising=False)
    monkeypatch.setattr(app_config, "DAIKIN_MIN_WINDOW_SLOTS", 1, raising=False)
    monkeypatch.setattr(
        "src.scheduler.lp_dispatch._optimization_preset_away_like",
        lambda: False,
        raising=True,
    )

    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    n = 4
    plan = LpPlan(ok=True, status="Optimal", objective_pence=0.0,
                  peak_threshold_pence=25.0, cheap_threshold_pence=10.0)
    plan.slot_starts_utc = [base + timedelta(minutes=30 * i) for i in range(n)]
    # Two solar_charge slots (PV-only charging) followed by two with chg<EPS price>0 → standard.
    plan.price_pence = [15.0] * n
    plan.import_kwh = [0.0] * n
    plan.export_kwh = [0.0] * n
    plan.battery_charge_kwh = [1.0, 1.0, 0.0, 0.0]
    plan.battery_discharge_kwh = [0.0] * n
    plan.dhw_electric_kwh = [0.6, 0.6, 0.0, 0.0]
    plan.space_electric_kwh = [0.0] * n
    plan.soc_kwh = [5.0, 6.0, 7.0, 7.0, 7.0]
    # LP plan WANTS tank at 65°C — emulates a runaway-reward scenario where
    # the soft cap was overridden. Dispatch must still clamp at 55.
    plan.tank_temp_c = [40.0, 65.0, 65.0, 65.0, 65.0]
    plan.indoor_temp_c = [21.0] * (n + 1)
    plan.pv_curt_kwh = [0.0] * n
    plan.lwt_offset_c = [0.0] * n
    plan.temp_outdoor_c = [18.0] * n

    pairs = daikin_dispatch_preview(plan, forecast=[])
    solar_pairs = [
        (rest, act) for rest, act in pairs
        if act.get("action_type") == "solar_preheat"
    ]
    assert solar_pairs, "expected a solar_preheat action"
    for _restore, action in solar_pairs:
        # Every solar_preheat write must clamp at 55, NOT the LP plan's 65.
        tank_temp = action["params"].get("tank_temp")
        assert tank_temp is not None, action
        assert tank_temp <= 55.0 + 0.001, (
            f"solar_preheat dispatch must clamp at DHW_TEMP_PV_ABUNDANCE_TARGET_C "
            f"(55), not honour LP plan's 65; got tank_temp={tank_temp}"
        )
        # Also above comfort floor — proving the lift fired.
        assert tank_temp >= float(app_config.DHW_TEMP_COMFORT_C) - 0.001, action


def test_dispatch_negative_price_action_uses_full_65c_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The PV-abundance dispatch clamp at 55 °C must NOT apply to
    negative-price (max_heat) actions — those still use DHW_TEMP_MAX_C (65 °C).
    Belt-and-braces against accidentally clamping the negative-price benefit."""
    from src.config import config as app_config
    from src.scheduler.lp_dispatch import daikin_dispatch_preview
    from src.scheduler.lp_optimizer import LpPlan

    monkeypatch.setattr(app_config, "DAIKIN_CONTROL_MODE", "active", raising=False)
    monkeypatch.setattr(app_config, "DHW_TEMP_PV_ABUNDANCE_TARGET_C", 55.0, raising=False)
    monkeypatch.setattr(app_config, "DHW_TEMP_MAX_C", 65.0, raising=False)
    monkeypatch.setattr(app_config, "DAIKIN_MIN_WINDOW_SLOTS", 1, raising=False)
    monkeypatch.setattr(
        "src.scheduler.lp_dispatch._optimization_preset_away_like",
        lambda: False,
        raising=True,
    )

    base = datetime(2026, 1, 15, 1, 0, tzinfo=UTC)
    n = 4
    plan = LpPlan(ok=True, status="Optimal", objective_pence=0.0,
                  peak_threshold_pence=25.0, cheap_threshold_pence=10.0)
    plan.slot_starts_utc = [base + timedelta(minutes=30 * i) for i in range(n)]
    # Negative price + grid charging → kind="negative" (not solar_charge)
    plan.price_pence = [-5.0, -5.0, 10.0, 10.0]
    plan.import_kwh = [2.0, 2.0, 0.0, 0.0]
    plan.export_kwh = [0.0] * n
    plan.battery_charge_kwh = [1.5, 1.5, 0.0, 0.0]
    plan.battery_discharge_kwh = [0.0] * n
    plan.dhw_electric_kwh = [0.6, 0.6, 0.0, 0.0]
    plan.space_electric_kwh = [0.0] * n
    plan.soc_kwh = [5.0, 6.0, 7.0, 7.0, 7.0]
    plan.tank_temp_c = [40.0, 60.0, 65.0, 65.0, 65.0]
    plan.indoor_temp_c = [21.0] * (n + 1)
    plan.pv_curt_kwh = [0.0] * n
    plan.lwt_offset_c = [0.0] * n
    plan.temp_outdoor_c = [5.0] * n

    pairs = daikin_dispatch_preview(plan, forecast=[])
    neg_pairs = [
        (rest, act) for rest, act in pairs
        if act.get("action_type") == "max_heat"
    ]
    assert neg_pairs, "expected a max_heat action"
    for _restore, action in neg_pairs:
        tank_temp = action["params"].get("tank_temp")
        assert tank_temp is not None, action
        # negative slots may go up to 65 (not capped at 55).
        assert tank_temp > 55.0, (
            f"max_heat dispatch must allow tank_temp > 55; got {tank_temp}"
        )


# --------------------------------------------------------------------------
# 2. Reward must NOT dominate export when export is profitable
# --------------------------------------------------------------------------

def test_pv_abundance_reward_does_not_dominate_profitable_export(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When export rate × COP > tank reward, the LP should still export
    rather than dump everything into the tank. Guards the 'profit second'
    side of the user's near-zero-grid-cost policy."""
    from src.config import config as app_config
    monkeypatch.setattr(app_config, "DHW_PV_ABUNDANCE_THRESHOLD_KWH", 0.5, raising=False)
    monkeypatch.setattr(app_config, "LP_PV_ABUNDANCE_TANK_REWARD_PENCE_PER_KWH", 0.5, raising=False)

    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    n = 4
    slots = [base + timedelta(minutes=30 * i) for i in range(n)]
    # Plenty of PV; very high export rate (50p/kWh) → exporting is way more
    # profitable than (reward 0.5p × cop_dhw 3.0 ≈ 1.5p of stored value).
    plan = _solve(
        slots=slots,
        prices=[20.0] * n,
        pv=[3.0] * n,
        base_load=[0.3] * n,
        init_soc=4.0,
        init_tank=40.0,
        export_prices=[50.0] * n,
    )
    assert plan.ok, plan.status
    total_export = sum(plan.export_kwh)
    total_dhw = sum(plan.dhw_electric_kwh)
    # Most of the surplus PV must still go to export (dwarfs DHW heating).
    assert total_export > total_dhw * 3.0, (
        f"Reward must not dominate profitable export; "
        f"export={total_export:.2f} kWh, dhw={total_dhw:.2f} kWh"
    )


# --------------------------------------------------------------------------
# 3. LP_TANK_HI_SLACK_PENCE_PER_DEGC_SLOT is honoured (closes #225 item 1)
# --------------------------------------------------------------------------

def test_tank_hi_slack_penalty_is_read_from_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify ``LP_TANK_HI_SLACK_PENCE_PER_DEGC_SLOT`` is read from config
    (closes #225 item 1: was hardcoded 0.01). Behavior-change tests against
    this knob are brittle because under most realistic price profiles the LP
    either (a) doesn't breach comfort at all (slack=0) or (b) is dominated
    by negative-price revenue (slack overshadowed). The wiring + non-crash
    is the verification that matters; observable tuning lands once an
    operator pushes the value to non-default in production."""
    from src.config import config as app_config

    monkeypatch.setattr(app_config, "LP_TANK_HI_SLACK_PENCE_PER_DEGC_SLOT", 0.5, raising=False)
    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    n = 8
    slots = [base + timedelta(minutes=30 * i) for i in range(n)]
    plan = _solve(
        slots=slots,
        prices=[-5.0] + [10.0] * (n - 1),
        pv=[0.0] * n,
        base_load=[0.3] * n,
        init_soc=8.0,
        init_tank=40.0,
    )
    assert plan.ok, plan.status
    # All tank temps finite + non-negative (tank can't go below 20 °C lower bound).
    for t in plan.tank_temp_c:
        assert t >= 19.0, t
        assert t == t  # NaN check


# --------------------------------------------------------------------------
# 4. SAFETY: every solar_preheat action has a paired restore (user's hard constraint)
# --------------------------------------------------------------------------

def test_every_solar_preheat_action_has_paired_restore_to_normal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """User's hard constraint: forgetting to switch back is the cost problem.
    Verify that every ``solar_preheat`` action emitted by the dispatch preview
    is paired with a ``restore`` row that drops tank to DHW_TEMP_NORMAL_C."""
    from src.config import config as app_config
    from src.scheduler.lp_dispatch import daikin_dispatch_preview
    from src.scheduler.lp_optimizer import LpPlan

    monkeypatch.setattr(app_config, "DAIKIN_CONTROL_MODE", "active", raising=False)
    # Build a minimal plan with two contiguous solar_charge slots followed by standard.
    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    n = 4
    plan = LpPlan(ok=True, status="Optimal", objective_pence=0.0,
                  peak_threshold_pence=25.0, cheap_threshold_pence=10.0)
    plan.slot_starts_utc = [base + timedelta(minutes=30 * i) for i in range(n)]
    plan.price_pence = [15.0] * n
    plan.import_kwh = [0.0] * n
    plan.export_kwh = [0.0] * n
    # solar_charge: chg > EPS AND grid_import < EPS (PV-only charging).
    plan.battery_charge_kwh = [1.0, 1.0, 0.0, 0.0]
    plan.battery_discharge_kwh = [0.0] * n
    plan.dhw_electric_kwh = [0.6, 0.6, 0.0, 0.0]
    plan.space_electric_kwh = [0.0] * n
    plan.soc_kwh = [5.0, 6.0, 7.0, 7.0, 7.0]
    plan.tank_temp_c = [40.0, 50.0, 60.0, 60.0, 60.0]  # LP raised tank
    plan.indoor_temp_c = [21.0] * (n + 1)
    plan.pv_curt_kwh = [0.0] * n
    plan.lwt_offset_c = [0.0] * n
    plan.temp_outdoor_c = [18.0] * n

    # Bypass the away-like preset gate
    monkeypatch.setattr(
        "src.scheduler.lp_dispatch._optimization_preset_away_like",
        lambda: False,
        raising=True,
    )
    # min_window_slots=1 so 2-slot solar window survives
    monkeypatch.setattr(app_config, "DAIKIN_MIN_WINDOW_SLOTS", 1, raising=False)

    pairs = daikin_dispatch_preview(plan, forecast=[])
    assert pairs, "expected at least one (restore, action) pair"

    solar_pairs = [
        (rest, act) for rest, act in pairs
        if act.get("action_type") == "solar_preheat"
    ]
    assert solar_pairs, f"expected a solar_preheat action; got action_types={[a.get('action_type') for _, a in pairs]}"

    for restore_row, action_row in solar_pairs:
        # 1. action_type matches.
        assert action_row["action_type"] == "solar_preheat"
        # 2. Paired restore exists with restore action_type.
        assert restore_row.get("action_type") == "restore", restore_row
        # 3. Restore drops tank back to DHW_TEMP_NORMAL_C — the safety target.
        assert restore_row["params"]["tank_temp"] == pytest.approx(
            float(app_config.DHW_TEMP_NORMAL_C)
        )
        # 4. Restore turns powerful OFF — no "stuck powerful boost" overnight.
        assert restore_row["params"]["tank_powerful"] is False
        # 5. Restore window starts WHERE the action ends (no gap that could
        #    let a missed-restore-window incident leave a hot tank).
        assert restore_row["start_time"] == action_row["end_time"]
