"""LP backtest / replay harness.

Re-runs :func:`solve_lp` for past optimizer runs using the inputs that were
durably snapshotted at the time. Lets us answer:

1. Did today's solver code regress vs the code that ran on day D? — replay
   each of D's runs on its own snapshot, compare the new objective and the
   £-cost of the replayed plan against the original.

2. Did the recalc cadence we ran (n recalcs/day) beat fewer/more recalcs? —
   chain replays through subsets of the day's runs, score each cadence's
   total dispatch against the actual Agile rates for that day.

3. Would today's *config* do better if we re-ran it on D's market? — same as
   (1) but with current config (mode="forward") instead of the snapshotted
   config (mode="honest").

State propagation between chained replays trusts the LP's own forward
integration (``lp_solution_snapshot.soc_kwh`` / ``tank_temp_c``) — the plan
is assumed to be followed perfectly. Absolute £ totals therefore differ
from prod realised totals; *deltas* between solvers/cadences on the same
chain are clean.

This module never touches Fox/Daikin and never fetches live weather. All
inputs come from SQLite snapshots:

* ``lp_inputs_snapshot``   — config, base load, initial SoC/tank/indoor.
* ``lp_solution_snapshot`` — slot vector + price + original dispatch.
* ``meteo_forecast_history`` — weather forecast as the LP saw it.
* ``agile_rates`` / ``agile_export_rates`` — actual published prices.
"""
from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date as _date, datetime, timedelta
from typing import Any, Iterable, Iterator, Literal
from zoneinfo import ZoneInfo

from .. import db
from ..config import config
from ..weather import (
    HourlyForecast,
    WeatherLpSeries,
    compute_heating_demand_factor,
    estimate_pv_kw,
    forecast_to_lp_inputs,
)
from .lp_optimizer import LpInitialState, LpPlan, solve_lp

logger = logging.getLogger(__name__)

Mode = Literal["honest", "forward"]
WeatherFidelity = Literal["honest", "approx", "missing"]


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class LpReplayResult:
    """Outcome of one single-run replay (Layer 1)."""

    ok: bool
    error: str | None = None

    run_id: int = 0
    plan_date: str = ""
    run_at_utc: str = ""
    mode: Mode = "honest"
    weather_fidelity: WeatherFidelity = "approx"
    skipped_config_keys: list[str] = field(default_factory=list)

    original_objective_pence: float | None = None
    original_objective_reconstructed: bool = True
    replayed_objective_pence: float = 0.0
    delta_objective_pence: float = 0.0  # replayed - original

    original_cost_at_actual_p: float = 0.0
    replayed_cost_at_actual_p: float = 0.0
    delta_cost_at_actual_p: float = 0.0  # replayed - original; negative = today's code saves more
    svt_shadow_p: float = 0.0
    original_savings_vs_svt_p: float = 0.0
    replayed_savings_vs_svt_p: float = 0.0

    slot_diffs: list[dict[str, Any]] = field(default_factory=list)

    # Internal — Layer 2 chains use this. Not exposed via JSON.
    _replayed_plan: LpPlan | None = None


@dataclass
class LpDayReplayResult:
    """Outcome of a multi-run chained replay across one day (Layer 2)."""

    ok: bool
    error: str | None = None

    plan_date: str = ""
    cadence_label: str = ""
    mode: Mode = "honest"
    recalc_run_ids: list[int] = field(default_factory=list)
    recalc_timestamps_utc: list[str] = field(default_factory=list)
    runs: list[LpReplayResult] = field(default_factory=list)

    active_slots: list[dict[str, Any]] = field(default_factory=list)
    total_original_cost_p: float = 0.0
    total_replayed_cost_p: float = 0.0
    total_delta_cost_p: float = 0.0
    total_svt_shadow_p: float = 0.0
    total_original_savings_vs_svt_p: float = 0.0
    total_replayed_savings_vs_svt_p: float = 0.0

    fidelity_notes: str = (
        "plan-followed-perfectly idealisation: state propagated from each plan's "
        "predicted SoC/tank into the next recalc, no execution-noise injection"
    )


@dataclass
class LpCadenceSweepResult:
    """Outcome of a multi-cadence sweep across one day (Layer 3)."""

    ok: bool
    error: str | None = None

    plan_date: str = ""
    mode: Mode = "honest"
    rows: list[LpDayReplayResult] = field(default_factory=list)
    best_cadence_label: str = ""
    best_total_replayed_savings_vs_svt_p: float = 0.0


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def resolve_run_id_for_date(
    local_date: str,
    *,
    which: Literal["first", "last"] = "first",
    tz_name: str | None = None,
) -> int | None:
    """Pick a single run_id for a given local calendar day.

    ``local_date`` is ``YYYY-MM-DD``. Returns the first (or last) optimizer_log
    row whose ``run_at`` falls inside that local day.
    """
    try:
        d = _date.fromisoformat(local_date)
    except ValueError:
        return None
    tz = ZoneInfo(tz_name or config.BULLETPROOF_TIMEZONE)
    local_start = datetime.combine(d, datetime.min.time(), tzinfo=tz)
    local_end = local_start + timedelta(days=1)
    utc_start = local_start.astimezone(UTC).isoformat()
    utc_end = local_end.astimezone(UTC).isoformat()

    order = "ASC" if which == "first" else "DESC"
    with db._lock:
        conn = db.get_connection()
        try:
            cur = conn.execute(
                f"""SELECT id FROM optimizer_log
                   WHERE run_at >= ? AND run_at < ?
                   ORDER BY run_at {order} LIMIT 1""",
                (utc_start, utc_end),
            )
            row = cur.fetchone()
            return int(row[0]) if row else None
        finally:
            conn.close()


def list_run_ids_for_date(
    local_date: str,
    *,
    tz_name: str | None = None,
) -> list[tuple[int, str]]:
    """Replayable (run_id, run_at_utc) tuples for a local calendar day.

    Restricted to runs that have a corresponding ``lp_inputs_snapshot`` row —
    i.e. LP runs (not heuristic fallbacks) and runs after the V11 snapshot
    migration. Ordered by run_at ASC.
    """
    try:
        d = _date.fromisoformat(local_date)
    except ValueError:
        return []
    tz = ZoneInfo(tz_name or config.BULLETPROOF_TIMEZONE)
    local_start = datetime.combine(d, datetime.min.time(), tzinfo=tz)
    local_end = local_start + timedelta(days=1)
    utc_start = local_start.astimezone(UTC).isoformat()
    utc_end = local_end.astimezone(UTC).isoformat()
    with db._lock:
        conn = db.get_connection()
        try:
            cur = conn.execute(
                """SELECT o.id, o.run_at FROM optimizer_log o
                   INNER JOIN lp_inputs_snapshot s ON s.run_id = o.id
                   WHERE o.run_at >= ? AND o.run_at < ?
                   ORDER BY o.run_at ASC""",
                (utc_start, utc_end),
            )
            return [(int(r[0]), str(r[1])) for r in cur.fetchall()]
        finally:
            conn.close()


def replay_run(
    run_id: int,
    *,
    mode: Mode = "honest",
    initial_override: LpInitialState | None = None,
) -> LpReplayResult:
    """Replay one past optimizer run on its frozen snapshot inputs.

    See module docstring for honest vs forward mode semantics.
    """
    inputs = db.get_lp_inputs(run_id)
    if not inputs:
        return LpReplayResult(
            ok=False, error=f"no lp_inputs_snapshot for run_id={run_id}", run_id=run_id, mode=mode,
        )
    slots = db.get_lp_solution_slots(run_id)
    if not slots:
        return LpReplayResult(
            ok=False, error=f"no lp_solution_snapshot for run_id={run_id}", run_id=run_id, mode=mode,
        )

    run_at_utc = str(inputs.get("run_at_utc") or "")
    plan_date = str(inputs.get("plan_date") or "")

    # Slot vector + prices come from the snapshot — preserves DST 46/50 exactly.
    slot_starts_utc: list[datetime] = []
    price_pence: list[float] = []
    for s in slots:
        try:
            t = _parse_iso(str(s["slot_time_utc"]))
        except (KeyError, ValueError, TypeError) as e:
            return LpReplayResult(
                ok=False, error=f"bad slot_time_utc in snapshot: {e}",
                run_id=run_id, mode=mode, run_at_utc=run_at_utc, plan_date=plan_date,
            )
        slot_starts_utc.append(t)
        price_pence.append(float(s.get("price_p") or 0.0))

    # base_load_json is what the LP saw at solve-time. Truth-as-the-LP-saw-it.
    try:
        base_load_kwh = [float(x) for x in json.loads(inputs.get("base_load_json") or "[]")]
    except (TypeError, ValueError, json.JSONDecodeError) as e:
        return LpReplayResult(
            ok=False, error=f"bad base_load_json: {e}",
            run_id=run_id, mode=mode, run_at_utc=run_at_utc, plan_date=plan_date,
        )
    if len(base_load_kwh) != len(slot_starts_utc):
        return LpReplayResult(
            ok=False,
            error=f"base_load length {len(base_load_kwh)} != slot count {len(slot_starts_utc)}",
            run_id=run_id, mode=mode, run_at_utc=run_at_utc, plan_date=plan_date,
        )

    # Initial state — caller may override (Layer 2 chaining).
    if initial_override is not None:
        initial = initial_override
    else:
        initial = LpInitialState(
            soc_kwh=float(inputs.get("soc_initial_kwh") or 0.0),
            tank_temp_c=float(inputs.get("tank_initial_c") or 45.0),
            indoor_temp_c=float(inputs.get("indoor_initial_c") or 20.0),
            soc_source=str(inputs.get("soc_source") or "snapshot"),
            tank_source=str(inputs.get("tank_source") or "snapshot"),
            indoor_source=str(inputs.get("indoor_source") or "snapshot"),
        )

    # Weather: prefer the exact forecast fetch referenced by the LP snapshot;
    # otherwise fall back to the latest fetch before the run timestamp.
    weather, weather_fidelity = _reconstruct_weather(
        run_at_utc,
        slot_starts_utc,
        forecast_fetch_at_utc=str(inputs.get("forecast_fetch_at_utc") or ""),
    )
    if weather_fidelity == "missing":
        return LpReplayResult(
            ok=False, error="weather snapshot missing for this run",
            run_id=run_id, mode=mode, run_at_utc=run_at_utc, plan_date=plan_date,
            weather_fidelity="missing",
        )

    # Export prices for the same window — None if outgoing tariff not fetched/configured.
    export_price_pence = _build_export_prices(slot_starts_utc)

    # Micro-climate offset: snapshot value in honest mode, current config in forward.
    if mode == "honest":
        mco = float(inputs.get("micro_climate_offset_c") or 0.0)
    else:
        try:
            mco = float(db.get_micro_climate_offset_c(getattr(config, "DAIKIN_MICRO_CLIMATE_LOOKBACK", 96)))
        except Exception:
            mco = 0.0

    tz = ZoneInfo(config.BULLETPROOF_TIMEZONE)

    # Apply config overrides (honest only) and solve.
    overrides_payload: dict[str, Any] | None = None
    if mode == "honest":
        try:
            overrides_payload = json.loads(inputs.get("config_snapshot_json") or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            overrides_payload = {}

    skipped_keys: list[str] = []
    with _config_overrides(overrides_payload, skipped_keys):
        try:
            plan = solve_lp(
                slot_starts_utc=slot_starts_utc,
                price_pence=price_pence,
                base_load_kwh=base_load_kwh,
                weather=weather,
                initial=initial,
                tz=tz,
                micro_climate_offset_c=mco,
                export_price_pence=export_price_pence,
            )
        except Exception as e:  # solver failure — surface, don't crash
            return LpReplayResult(
                ok=False, error=f"solve_lp raised: {e}",
                run_id=run_id, mode=mode, run_at_utc=run_at_utc, plan_date=plan_date,
                weather_fidelity=weather_fidelity, skipped_config_keys=skipped_keys,
            )

    if not plan.ok:
        return LpReplayResult(
            ok=False, error=f"solver status: {plan.status}",
            run_id=run_id, mode=mode, run_at_utc=run_at_utc, plan_date=plan_date,
            weather_fidelity=weather_fidelity, skipped_config_keys=skipped_keys,
            replayed_objective_pence=float(plan.objective_pence),
        )

    # Score against actual published rates (the £ counterfactual).
    score = _score_against_actuals(slot_starts_utc, slots, plan)

    # Reconstruct the original objective from snapshot dispatch + price (no penalties).
    original_objective_recon = _reconstruct_original_objective_from_slots(slots, export_price_pence)

    slot_diffs = _build_slot_diffs(slots, plan)

    return LpReplayResult(
        ok=True,
        run_id=run_id,
        plan_date=plan_date,
        run_at_utc=run_at_utc,
        mode=mode,
        weather_fidelity=weather_fidelity,
        skipped_config_keys=skipped_keys,
        original_objective_pence=original_objective_recon,
        original_objective_reconstructed=True,
        replayed_objective_pence=float(plan.objective_pence),
        delta_objective_pence=float(plan.objective_pence) - original_objective_recon,
        original_cost_at_actual_p=score["original_cost_p"],
        replayed_cost_at_actual_p=score["replayed_cost_p"],
        delta_cost_at_actual_p=score["replayed_cost_p"] - score["original_cost_p"],
        svt_shadow_p=score["svt_shadow_p"],
        original_savings_vs_svt_p=score["svt_shadow_p"] - score["original_cost_p"],
        replayed_savings_vs_svt_p=score["svt_shadow_p"] - score["replayed_cost_p"],
        slot_diffs=slot_diffs,
        _replayed_plan=plan,
    )


def replay_day(
    local_date: str,
    *,
    cadence: str = "original",
    mode: Mode = "honest",
) -> LpDayReplayResult:
    """Chain-replay all (or a subset of) the day's optimizer runs.

    ``cadence`` (subset of original run_ids — see module docstring):
      - ``"original"`` — every run_id that fired that local day.
      - ``"first"`` — only the first run_id of the day.
      - ``"first:N"`` — only the first N run_ids.
      - ``"stride:K"`` — every K-th run_id (so ``stride:2`` is every other).
      - ``"subset:0,2,5"`` — specific 0-indexed positions in the day's run list.

    Custom-clock cadences (e.g. ``"hourly"``, ``"fixed:00:05,12:00"``) are a
    deliberate v2 — they require synthesising inputs the LP never saw, which
    would taint the comparison. Until that arrives, use subset cadences over
    the real recalc sequence.
    """
    runs = list_run_ids_for_date(local_date)
    if not runs:
        return LpDayReplayResult(
            ok=False, error=f"no optimizer_log rows for {local_date}",
            plan_date=local_date, cadence_label=cadence, mode=mode,
        )

    try:
        selected = _apply_cadence_filter(runs, cadence)
    except ValueError as e:
        return LpDayReplayResult(
            ok=False, error=f"bad cadence: {e}",
            plan_date=local_date, cadence_label=cadence, mode=mode,
        )
    if not selected:
        return LpDayReplayResult(
            ok=False, error=f"cadence {cadence!r} matched zero runs",
            plan_date=local_date, cadence_label=cadence, mode=mode,
        )

    # Boundary timestamps: each run is "active" until the next selected run starts.
    selected_run_ids = [rid for rid, _ in selected]
    selected_timestamps = [ts for _, ts in selected]

    tz = ZoneInfo(config.BULLETPROOF_TIMEZONE)
    local_d = _date.fromisoformat(local_date)
    day_end_utc = (
        datetime.combine(local_d + timedelta(days=1), datetime.min.time(), tzinfo=tz).astimezone(UTC)
    )

    boundaries_utc: list[datetime] = []
    for ts in selected_timestamps:
        boundaries_utc.append(_parse_iso(ts))
    boundaries_utc.append(day_end_utc)  # last run runs through end-of-day

    results: list[LpReplayResult] = []
    state_override: LpInitialState | None = None

    for k, run_id in enumerate(selected_run_ids):
        next_boundary = boundaries_utc[k + 1]

        result = replay_run(run_id, mode=mode, initial_override=state_override)
        results.append(result)
        if not result.ok:
            return LpDayReplayResult(
                ok=False, error=f"chain broke at run_id={run_id}: {result.error}",
                plan_date=local_date, cadence_label=cadence, mode=mode,
                recalc_run_ids=selected_run_ids[: k + 1],
                recalc_timestamps_utc=selected_timestamps[: k + 1],
                runs=results,
            )

        # Seed next recalc from THIS replay's predicted state at the boundary.
        plan = result._replayed_plan
        if plan is not None and k + 1 < len(selected_run_ids):
            state_override = _state_at(plan, next_boundary)

    # Concatenate active slot windows: each run's plan slots in [t_k, t_{k+1}).
    total_original_p = 0.0
    total_replayed_p = 0.0
    total_svt_p = 0.0
    active: list[dict[str, Any]] = []
    for k, result in enumerate(results):
        plan = result._replayed_plan
        if plan is None:
            continue
        t_lo = boundaries_utc[k]
        t_hi = boundaries_utc[k + 1]
        # Slots whose START falls in [t_lo, t_hi). Original t_lo equals the run's
        # first slot, so this is the "until next recalc" window.
        for i, st in enumerate(plan.slot_starts_utc):
            if not (t_lo <= st < t_hi):
                continue
            agile_p = float(plan.price_pence[i]) if i < len(plan.price_pence) else 0.0
            export_p = _export_for_slot(st)
            replayed_imp = float(plan.import_kwh[i]) if i < len(plan.import_kwh) else 0.0
            replayed_exp = float(plan.export_kwh[i]) if i < len(plan.export_kwh) else 0.0
            replayed_cost = replayed_imp * agile_p - replayed_exp * export_p

            # Original dispatch on the same slot — pull from the snapshot whose
            # window covers this t (the run that fired at boundaries_utc[k]).
            orig_row = _find_original_slot(result.run_id, st)
            if orig_row is not None:
                orig_imp = float(orig_row.get("import_kwh") or 0.0)
                orig_exp = float(orig_row.get("export_kwh") or 0.0)
                orig_cost = orig_imp * agile_p - orig_exp * export_p
            else:
                orig_imp = 0.0
                orig_exp = 0.0
                orig_cost = 0.0

            svt_p = _svt_shadow_for_slot(st)
            active.append({
                "slot_time_utc": st.isoformat(),
                "active_run_id": result.run_id,
                "price_p": agile_p,
                "export_price_p": export_p,
                "original_import_kwh": orig_imp,
                "original_export_kwh": orig_exp,
                "original_cost_p": orig_cost,
                "replayed_import_kwh": replayed_imp,
                "replayed_export_kwh": replayed_exp,
                "replayed_cost_p": replayed_cost,
                "svt_shadow_p": svt_p,
            })
            total_original_p += orig_cost
            total_replayed_p += replayed_cost
            total_svt_p += svt_p

    return LpDayReplayResult(
        ok=True,
        plan_date=local_date,
        cadence_label=cadence,
        mode=mode,
        recalc_run_ids=selected_run_ids,
        recalc_timestamps_utc=selected_timestamps,
        runs=results,
        active_slots=active,
        total_original_cost_p=total_original_p,
        total_replayed_cost_p=total_replayed_p,
        total_delta_cost_p=total_replayed_p - total_original_p,
        total_svt_shadow_p=total_svt_p,
        total_original_savings_vs_svt_p=total_svt_p - total_original_p,
        total_replayed_savings_vs_svt_p=total_svt_p - total_replayed_p,
    )


def sweep_cadences(
    local_date: str,
    *,
    cadences: Iterable[str] = (
        "original",
        "first",
        "first:2",
        "stride:2",
    ),
    mode: Mode = "honest",
) -> LpCadenceSweepResult:
    """Run :func:`replay_day` across multiple cadences and rank by savings vs SVT."""
    rows: list[LpDayReplayResult] = []
    for c in cadences:
        rows.append(replay_day(local_date, cadence=c, mode=mode))

    ok_rows = [r for r in rows if r.ok]
    if not ok_rows:
        return LpCadenceSweepResult(
            ok=False,
            error="no cadence produced a valid replay (see rows for per-cadence errors)",
            plan_date=local_date, mode=mode, rows=rows,
        )

    best = max(ok_rows, key=lambda r: r.total_replayed_savings_vs_svt_p)
    return LpCadenceSweepResult(
        ok=True,
        plan_date=local_date,
        mode=mode,
        rows=rows,
        best_cadence_label=best.cadence_label,
        best_total_replayed_savings_vs_svt_p=best.total_replayed_savings_vs_svt_p,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _parse_iso(s: str) -> datetime:
    """Parse a UTC ISO timestamp; tolerate trailing 'Z' and missing tz."""
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


@contextmanager
def _config_overrides(
    snapshot: dict[str, Any] | None,
    skipped_out: list[str],
) -> Iterator[None]:
    """Save-set-restore each key on the live ``config`` object.

    Mirrors the simulate_plan pattern in src/mcp_server.py:206-236. Keys not
    present on the current Config (schema drift) are recorded in ``skipped_out``
    so the result can surface them rather than silently no-op.
    """
    if not snapshot:
        yield
        return

    saved: dict[str, Any] = {}
    try:
        for k, v in snapshot.items():
            if hasattr(config, k):
                saved[k] = getattr(config, k)
                try:
                    setattr(config, k, v)
                except Exception:
                    skipped_out.append(k)
            else:
                skipped_out.append(k)
        yield
    finally:
        for k, v in saved.items():
            try:
                setattr(config, k, v)
            except Exception:
                pass


def _reconstruct_weather(
    run_at_utc: str,
    slot_starts_utc: list[datetime],
    *,
    forecast_fetch_at_utc: str = "",
) -> tuple[WeatherLpSeries, WeatherFidelity]:
    """Rebuild WeatherLpSeries from meteo_forecast_history.

    Picks the most recent forecast fetch strictly before ``run_at_utc`` (the
    forecast the LP saw when it solved). Returns ``("missing")`` fidelity when
    no fetch exists — caller surfaces an error.

    V11-A (#194): cloud cover is now stored on ``meteo_forecast_history``
    when present. When the snapshot row carries ``cloud_cover_pct``, replay
    feeds it into the cloud-attenuation step in ``forecast_to_lp_inputs``,
    closing the historical PV-fidelity gap. For pre-V11-A snapshots the
    column is NULL and we fall back to the legacy 0% (no attenuation) path
    — same behaviour as before, just confined to ageing rows.
    """
    rows: list[dict[str, Any]] = []
    if forecast_fetch_at_utc:
        rows = db.get_meteo_forecast_at(forecast_fetch_at_utc)
    if not rows and run_at_utc:
        rows = db.get_meteo_forecast_history_latest_before(run_at_utc)
    # Empty array → no prior fetch existed.
    if not rows:
        empty = WeatherLpSeries(
            slot_starts_utc=list(slot_starts_utc),
            temperature_outdoor_c=[10.0] * len(slot_starts_utc),
            shortwave_radiation_wm2=[0.0] * len(slot_starts_utc),
            cloud_cover_pct=[0.0] * len(slot_starts_utc),
            pv_kwh_per_slot=[0.0] * len(slot_starts_utc),
            cop_space=[3.0] * len(slot_starts_utc),
            cop_dhw=[2.5] * len(slot_starts_utc),
        )
        return empty, "missing"

    forecast: list[HourlyForecast] = []
    has_cloud_data = False
    for r in rows:
        try:
            t = _parse_iso(str(r["slot_time"]))
        except (KeyError, ValueError, TypeError):
            continue
        temp_c = float(r.get("temp_c") or 10.0)
        rad = float(r.get("solar_w_m2") or 0.0)
        cloud_raw = r.get("cloud_cover_pct")
        if cloud_raw is None:
            cloud = 0.0
        else:
            try:
                cloud = float(cloud_raw)
                has_cloud_data = True
            except (TypeError, ValueError):
                cloud = 0.0
        forecast.append(
            HourlyForecast(
                time_utc=t,
                temperature_c=temp_c,
                cloud_cover_pct=cloud,
                shortwave_radiation_wm2=rad,
                estimated_pv_kw=(
                    float(r["direct_pv_kw"])
                    if r.get("direct_pv_kw") is not None
                    else estimate_pv_kw(rad)
                ),
                heating_demand_factor=compute_heating_demand_factor(temp_c),
                pv_direct=r.get("direct_pv_kw") is not None,
            )
        )

    weather = forecast_to_lp_inputs(forecast, slot_starts_utc, pv_scale=1.0)
    fidelity: WeatherFidelity = "honest" if has_cloud_data else "approx"
    return weather, fidelity


def _build_export_prices(slot_starts_utc: list[datetime]) -> list[float] | None:
    """Map per-slot starts to ``agile_export_rates`` value, or None when no outgoing tariff."""
    if not slot_starts_utc:
        return None
    if not (config.OCTOPUS_EXPORT_TARIFF_CODE or "").strip():
        return None
    period_from = slot_starts_utc[0].isoformat()
    period_to = (slot_starts_utc[-1] + timedelta(minutes=30)).isoformat()
    rows = db.get_agile_export_rates_in_range(period_from, period_to)
    if not rows:
        return None
    by_start: dict[str, float] = {}
    for r in rows:
        try:
            iso_norm = str(r["valid_from"]).replace("+00:00", "Z")
            by_start[iso_norm] = float(r["value_inc_vat"])
        except (KeyError, TypeError, ValueError):
            continue
    flat = float(config.EXPORT_RATE_PENCE)
    out: list[float] = []
    matched = 0
    for st in slot_starts_utc:
        key = st.isoformat().replace("+00:00", "Z")
        v = by_start.get(key)
        if v is not None:
            out.append(v)
            matched += 1
        else:
            out.append(flat)
    if matched == 0:
        return None
    return out


def _export_for_slot(slot_utc: datetime) -> float:
    """Best-effort actual export rate for one slot. Falls back to flat constant."""
    iso = slot_utc.isoformat().replace("+00:00", "Z")
    rows = db.get_agile_export_rates_in_range(slot_utc.isoformat(), (slot_utc + timedelta(minutes=30)).isoformat())
    for r in rows:
        if str(r.get("valid_from", "")).replace("+00:00", "Z") == iso:
            try:
                return float(r["value_inc_vat"])
            except (KeyError, TypeError, ValueError):
                pass
    return float(config.EXPORT_RATE_PENCE)


def _svt_shadow_for_slot(slot_utc: datetime) -> float:
    """SVT shadow cost for one slot, summed from execution_log within the slot window."""
    from_ts = slot_utc.isoformat()
    to_ts = (slot_utc + timedelta(minutes=30)).isoformat()
    rows = db.get_execution_logs(from_ts=from_ts, to_ts=to_ts, limit=4)
    total = 0.0
    for r in rows:
        v = r.get("cost_svt_shadow_pence")
        if v is not None:
            try:
                total += float(v)
            except (TypeError, ValueError):
                pass
    return total


def _find_original_slot(run_id: int, slot_utc: datetime) -> dict[str, Any] | None:
    """Return the lp_solution_snapshot row for one slot of a known run."""
    iso_targets = {
        slot_utc.isoformat(),
        slot_utc.isoformat().replace("+00:00", "Z"),
    }
    for s in db.get_lp_solution_slots(run_id):
        if str(s.get("slot_time_utc", "")) in iso_targets:
            return s
    return None


def _score_against_actuals(
    slot_starts_utc: list[datetime],
    original_slots: list[dict[str, Any]],
    replayed_plan: LpPlan,
) -> dict[str, float]:
    """£-cost of original vs replayed plan, both priced at actual published rates.

    The LP saw published Agile prices when it solved (Octopus publishes a day
    ahead), so ``actual ≈ snapshot price_p``. Any £ delta therefore reflects
    dispatch decision changes only — the regression signal we want.
    """
    # Pull actual rates for the entire slot window in one go.
    tariff = (config.OCTOPUS_TARIFF_CODE or "").strip()
    rates_by_start: dict[str, float] = {}
    if tariff and slot_starts_utc:
        period_from = slot_starts_utc[0]
        period_to = slot_starts_utc[-1] + timedelta(minutes=30)
        for r in db.get_rates_for_period(tariff, period_from, period_to):
            iso = str(r.get("valid_from", "")).replace("+00:00", "Z")
            try:
                rates_by_start[iso] = float(r["value_inc_vat"])
            except (KeyError, TypeError, ValueError):
                continue

    def _agile(slot: datetime) -> float:
        iso = slot.isoformat().replace("+00:00", "Z")
        return rates_by_start.get(iso, 0.0)

    export_actual = [_export_for_slot(s) for s in slot_starts_utc]

    original_cost = 0.0
    for s in original_slots:
        try:
            t = _parse_iso(str(s["slot_time_utc"]))
        except (KeyError, ValueError, TypeError):
            continue
        agile = _agile(t)
        # Find this slot's index for export price.
        try:
            idx = slot_starts_utc.index(t)
            ep = export_actual[idx]
        except ValueError:
            ep = float(config.EXPORT_RATE_PENCE)
        imp = float(s.get("import_kwh") or 0.0)
        exp = float(s.get("export_kwh") or 0.0)
        original_cost += imp * agile - exp * ep

    replayed_cost = 0.0
    for i, t in enumerate(replayed_plan.slot_starts_utc):
        agile = _agile(t)
        ep = export_actual[i] if i < len(export_actual) else float(config.EXPORT_RATE_PENCE)
        imp = float(replayed_plan.import_kwh[i]) if i < len(replayed_plan.import_kwh) else 0.0
        exp = float(replayed_plan.export_kwh[i]) if i < len(replayed_plan.export_kwh) else 0.0
        replayed_cost += imp * agile - exp * ep

    # SVT shadow for the slot window.
    svt = 0.0
    if slot_starts_utc:
        from_ts = slot_starts_utc[0].isoformat()
        to_ts = (slot_starts_utc[-1] + timedelta(minutes=30)).isoformat()
        for row in db.get_execution_logs(from_ts=from_ts, to_ts=to_ts, limit=2000):
            v = row.get("cost_svt_shadow_pence")
            if v is None:
                continue
            try:
                svt += float(v)
            except (TypeError, ValueError):
                continue

    return {
        "original_cost_p": original_cost,
        "replayed_cost_p": replayed_cost,
        "svt_shadow_p": svt,
    }


def _reconstruct_original_objective_from_slots(
    original_slots: list[dict[str, Any]],
    export_price_pence: list[float] | None,
) -> float:
    """Approximate the original LP's objective from snapshotted dispatch + price.

    This equals the solver's grid component (Σ import·price − Σ export·export_price)
    minus the soft penalties (cycle, comfort, tank overshoot). Good enough for
    relative comparison; full fidelity needs a new column on lp_inputs_snapshot.
    """
    flat_export = float(config.EXPORT_RATE_PENCE)
    total = 0.0
    for i, s in enumerate(original_slots):
        try:
            agile = float(s.get("price_p") or 0.0)
            imp = float(s.get("import_kwh") or 0.0)
            exp = float(s.get("export_kwh") or 0.0)
        except (TypeError, ValueError):
            continue
        ep = (
            export_price_pence[i]
            if export_price_pence is not None and i < len(export_price_pence)
            else flat_export
        )
        total += imp * agile - exp * ep
    return total


def _build_slot_diffs(
    original_slots: list[dict[str, Any]],
    plan: LpPlan,
) -> list[dict[str, Any]]:
    """Per-slot diff table for downstream rendering / debugging."""
    out: list[dict[str, Any]] = []
    n = min(len(original_slots), len(plan.slot_starts_utc))
    for i in range(n):
        orig = original_slots[i]
        st = plan.slot_starts_utc[i]
        d = {
            "slot_index": int(orig.get("slot_index", i)),
            "slot_time_utc": st.isoformat(),
            "price_p": float(plan.price_pence[i]) if i < len(plan.price_pence) else 0.0,
            "orig_import_kwh": float(orig.get("import_kwh") or 0.0),
            "new_import_kwh": float(plan.import_kwh[i]) if i < len(plan.import_kwh) else 0.0,
            "orig_export_kwh": float(orig.get("export_kwh") or 0.0),
            "new_export_kwh": float(plan.export_kwh[i]) if i < len(plan.export_kwh) else 0.0,
            "orig_charge_kwh": float(orig.get("charge_kwh") or 0.0),
            "new_charge_kwh": float(plan.battery_charge_kwh[i]) if i < len(plan.battery_charge_kwh) else 0.0,
            "orig_discharge_kwh": float(orig.get("discharge_kwh") or 0.0),
            "new_discharge_kwh": float(plan.battery_discharge_kwh[i]) if i < len(plan.battery_discharge_kwh) else 0.0,
            "orig_dhw_kwh": float(orig.get("dhw_kwh") or 0.0),
            "new_dhw_kwh": float(plan.dhw_electric_kwh[i]) if i < len(plan.dhw_electric_kwh) else 0.0,
            "orig_space_kwh": float(orig.get("space_kwh") or 0.0),
            "new_space_kwh": float(plan.space_electric_kwh[i]) if i < len(plan.space_electric_kwh) else 0.0,
            "orig_soc_kwh": float(orig.get("soc_kwh") or 0.0),
            "new_soc_kwh": float(plan.soc_kwh[i + 1]) if i + 1 < len(plan.soc_kwh) else 0.0,
            "orig_tank_temp_c": float(orig.get("tank_temp_c") or 0.0),
            "new_tank_temp_c": float(plan.tank_temp_c[i + 1]) if i + 1 < len(plan.tank_temp_c) else 0.0,
        }
        for k in ("import", "export", "charge", "discharge", "dhw", "space"):
            d[f"d_{k}_kwh"] = d[f"new_{k}_kwh"] - d[f"orig_{k}_kwh"]
        out.append(d)
    return out


def _state_at(plan: LpPlan, when_utc: datetime) -> LpInitialState:
    """Pluck SoC / tank / indoor from the plan at the slot whose start ≤ when_utc < next.

    Used to seed the next chained replay's initial state. The plan's per-slot
    state arrays are length N+1 (state at start of slot i is index i; state at
    end is index i+1). We want the state AT ``when_utc``, i.e. the start of the
    slot beginning at ``when_utc``, or the final state if past the horizon.
    """
    starts = plan.slot_starts_utc
    if not starts:
        return LpInitialState(soc_kwh=0.0, tank_temp_c=45.0, indoor_temp_c=20.0)
    if when_utc <= starts[0]:
        idx = 0
    elif when_utc >= starts[-1]:
        idx = len(starts)  # → final state
    else:
        # Find first slot whose start >= when_utc.
        idx = 0
        for i, st in enumerate(starts):
            if st >= when_utc:
                idx = i
                break
    soc = plan.soc_kwh[idx] if idx < len(plan.soc_kwh) else (plan.soc_kwh[-1] if plan.soc_kwh else 0.0)
    tank = plan.tank_temp_c[idx] if idx < len(plan.tank_temp_c) else (plan.tank_temp_c[-1] if plan.tank_temp_c else 45.0)
    indoor = plan.indoor_temp_c[idx] if idx < len(plan.indoor_temp_c) else (plan.indoor_temp_c[-1] if plan.indoor_temp_c else 20.0)
    return LpInitialState(
        soc_kwh=float(soc),
        tank_temp_c=float(tank),
        indoor_temp_c=float(indoor),
        soc_source="replay_chain",
        tank_source="replay_chain",
        indoor_source="replay_chain",
    )


def _apply_cadence_filter(
    runs: list[tuple[int, str]],
    cadence: str,
) -> list[tuple[int, str]]:
    """Filter the day's runs by cadence DSL.

    See :func:`replay_day` for syntax.
    """
    cadence = (cadence or "original").strip().lower()
    if cadence == "original":
        return list(runs)
    if cadence == "first":
        return runs[:1]
    if cadence.startswith("first:"):
        try:
            n = int(cadence.split(":", 1)[1])
        except ValueError as e:
            raise ValueError(f"first:N must be integer, got {cadence!r}") from e
        if n <= 0:
            raise ValueError(f"first:N must be positive, got {n}")
        return runs[:n]
    if cadence.startswith("stride:"):
        try:
            k = int(cadence.split(":", 1)[1])
        except ValueError as e:
            raise ValueError(f"stride:K must be integer, got {cadence!r}") from e
        if k <= 0:
            raise ValueError(f"stride:K must be positive, got {k}")
        return [r for i, r in enumerate(runs) if i % k == 0]
    if cadence.startswith("subset:"):
        try:
            indices = [int(x.strip()) for x in cadence.split(":", 1)[1].split(",") if x.strip()]
        except ValueError as e:
            raise ValueError(f"subset:idx,idx,... must be ints, got {cadence!r}") from e
        return [runs[i] for i in indices if 0 <= i < len(runs)]
    raise ValueError(f"unknown cadence {cadence!r}")
