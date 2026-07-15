"""DHW (tank) policy — fixed deterministic daily schedule that replaces
the LP-driven tank arbitrage stack (PR G/H/I/J).

Per user 2026-05-23:

    Overnight (22:00 → 13:00 next day):  tank = SETBACK (37 °C)
    Daytime  (13:00 → 22:00):            tank = NORMAL  (45 °C, evening showers)
    Guests mode:                          tank = NORMAL 24 h (no setback —
                                            morning showers possible)
    Vacation mode:                        no actions (Daikin firmware handles)
    Negative-price slots (Outgoing < 0):  override to BOOST (60 °C) for the
                                            duration of the negative window

The policy intentionally does NOT optimize for tariff arbitrage on DHW
(except the negative-price case where the grid is paying us). The user's
explicit constraint: "battery first, tank second; don't drain the battery
overnight just to keep water hot when no shower is happening." The
~£20-50/year of DHW arb savings are sacrificed in exchange for operational
simplicity and removing an entire class of bugs (drift checks, restore
preservation chains, override propagation conflicts).

This module is the SINGLE SOURCE OF TRUTH for tank actions when
``DHW_FIXED_SCHEDULE_ENABLED=True``. The LP still optimizes battery /
forecasts space heating, but emits no tank-write actions.
"""
from __future__ import annotations

import json
import logging
import statistics
import time as _time
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from . import db
from .config import config

logger = logging.getLogger(__name__)


def _tz_local() -> ZoneInfo:
    return ZoneInfo(getattr(config, "BULLETPROOF_TIMEZONE", "Europe/London"))


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _make_action(
    *,
    action_type: str,
    start_utc: datetime,
    end_utc: datetime,
    tank_temp_c: int,
    tank_powerful: bool = False,
) -> dict[str, Any]:
    """Build an action_schedule-shaped row dict ready for db.upsert_action."""
    return {
        "device": "daikin",
        "action_type": action_type,
        "start_time": _iso_z(start_utc),
        "end_time": _iso_z(end_utc),
        "params": {
            "tank_power": True,
            "tank_temp": int(tank_temp_c),
            "tank_powerful": tank_powerful,
            "dhw_policy": True,  # marker that this row came from dhw_policy
        },
    }


def _shower_in_span(start_utc: datetime, end_utc: datetime, tz: ZoneInfo) -> bool:
    """True if any configured DHW shower window overlaps ``[start, end)`` in
    local time (#tank-precool guard). Parses ``DHW_SHOWER_SCHEDULE``
    (``"HH:MM-HH:MM,…"``). Conservative: any parse trouble → True (don't
    pre-cool when unsure). Empty schedule → False.
    """
    sched = (getattr(config, "DHW_SHOWER_SCHEDULE", "") or "").strip()
    if not sched or end_utc <= start_utc:
        return False
    windows: list[tuple[int, int]] = []
    for part in sched.split(","):
        part = part.strip()
        if "-" not in part:
            continue
        try:
            a, b = part.split("-", 1)
            sh, sm = a.split(":")
            eh, em = b.split(":")
            windows.append((int(sh) * 60 + int(sm), int(eh) * 60 + int(em)))
        except ValueError:
            return True  # unparseable → assume a shower could be there
    if not windows:
        return False
    day = start_utc.astimezone(tz).date()
    last = end_utc.astimezone(tz).date()
    while day <= last:
        midnight = datetime(day.year, day.month, day.day, tzinfo=tz)
        for ws, we in windows:
            sw = (midnight + timedelta(minutes=ws)).astimezone(UTC)
            ew = (midnight + timedelta(minutes=we)).astimezone(UTC)
            if sw < end_utc and ew > start_utc:
                return True
        day = day + timedelta(days=1)
    return False


def _detect_negative_windows(
    agile_rates: list[dict[str, Any]] | None,
    horizon_start_utc: datetime,
    horizon_end_utc: datetime,
) -> list[tuple[datetime, datetime]]:
    """Group consecutive negative-price 30-min slots into contiguous windows.

    ``agile_rates`` is a list of dicts with at least ``valid_from`` (ISO
    UTC) and ``value_inc_vat`` (pence/kWh). Returns list of (start, end)
    UTC datetime tuples; empty when no negative slots in horizon.
    """
    if not agile_rates:
        return []
    neg_slot_starts: list[datetime] = []
    for r in agile_rates:
        try:
            ts_raw = r.get("valid_from") or r.get("slot_time_utc")
            rate = float(r.get("value_inc_vat", r.get("rate_p", 999)))
        except (TypeError, ValueError):
            continue
        if not ts_raw or rate >= 0:
            continue
        try:
            ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            continue
        if not (horizon_start_utc <= ts < horizon_end_utc):
            continue
        neg_slot_starts.append(ts)
    if not neg_slot_starts:
        return []
    neg_slot_starts.sort()
    # Group consecutive 30-min slots
    windows: list[tuple[datetime, datetime]] = []
    cur_start = neg_slot_starts[0]
    cur_end = cur_start + timedelta(minutes=30)
    for ts in neg_slot_starts[1:]:
        if ts == cur_end:  # contiguous
            cur_end = ts + timedelta(minutes=30)
        else:
            windows.append((cur_start, cur_end))
            cur_start = ts
            cur_end = ts + timedelta(minutes=30)
    windows.append((cur_start, cur_end))
    return windows


# --- Price-aware warmup start hour (#681) ----------------------------------
# The warmup START hour is chosen once per plan-date to minimise the mean
# Agile IMPORT price of its two 30-min transition slots, within the bounded
# window [DHW_WARMUP_WINDOW_START_LOCAL, DHW_WARMUP_WINDOW_END_LOCAL). The
# choice is PERSISTED (runtime_settings ``dhw_warmup_hour_<YYYY-MM-DD>``) the
# first time it is resolved for a date and returned verbatim thereafter —
# "persist-once" — so re-plans at different wall-clock times keep the K2 pin
# (forecast_dhw_load_per_slot) coherent with the fired warmup row and never
# thrash the restore covenant. Default OFF: when disabled the resolver returns
# the static DHW_WARMUP_START_HOUR_LOCAL byte-for-byte and only SHADOW-LOGS the
# would-pick delta so the deltas can be observed before enabling (#640 pattern).
#
# SINGLE WRITER (review #683). ``resolve_warmup_hour_local`` (and the
# horizon convenience ``resolve_warmup_hours_for_horizon``) is the ONLY thing
# that persists a hour, and it is called ONLY from the LP-solve path
# (``optimizer._run_optimizer_lp``), which holds the raw rate rows and can tell
# REAL Agile from the horizon-extender's synthetic priors (``fetched_at ==
# "prior"``). Two guards protect the observation itself:
#   1. A date is only picked/persisted when its WHOLE candidate window
#      [START, END) is covered by REAL rates. So D+1 (whose Agile publishes
#      ~16:00 local — the horizon tail is priors before then) is NOT frozen
#      from median priors; it resolves cleanly once the real rates land.
#   2. A truncated price range (a display/dispatch caller fetching only
#      [13, setback)) leaves the [11,13) slots absent → window not real-covered
#      → static fallback, no persist. Belt-and-suspenders with the single-writer
#      rule below.
# Everything else — ``generate_daily_tank_schedule`` (dispatch/display),
# ``forecast_dhw_load_per_slot`` (the K2 pin), ``_nominal_daily_total_kwh`` and
# ``_nominal_bucket_shares`` — is a pure READER via ``_read_warmup_hour``
# (persisted-or-static, never resolves, never persists).

# Shadow-log dedupe — one INFO line per (date, static, chosen) per process.
# NB: this set (and the ``dhw_warmup_hour_<date>`` kv rows) accrue one entry per
# plan-date; both are tiny (a date string / one SQLite row per day) and the kv
# rows are swept lazily in ``_persist_warmup_hour`` (keys older than ~7 days).
_warmup_shadow_logged: set[tuple[str, int, int]] = set()


def _price_aware_enabled() -> bool:
    return bool(getattr(config, "DHW_WARMUP_PRICE_AWARE_ENABLED", False))


def _static_warmup_hour() -> int:
    return int(getattr(config, "DHW_WARMUP_START_HOUR_LOCAL", 13))


def _warmup_window_bounds() -> tuple[int, int]:
    lo = int(getattr(config, "DHW_WARMUP_WINDOW_START_LOCAL", 11))
    hi = int(getattr(config, "DHW_WARMUP_WINDOW_END_LOCAL", 16))
    return lo, hi


def _warmup_setting_key(d: date) -> str:
    return f"dhw_warmup_hour_{d.isoformat()}"


def _persisted_warmup_hour(d: date) -> int | None:
    """The persist-once value for *d*, or None. Reads the ``runtime_settings``
    kv table directly (schema-free free-form key) — the same generic per-key
    store used by e.g. ``dhw_bias_enable_suggested_at``, so no new table."""
    try:
        raw = db.get_runtime_setting(_warmup_setting_key(d))
    except Exception:  # pragma: no cover - defensive: resolver must not fail
        return None
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


_WARMUP_KEY_PREFIX = "dhw_warmup_hour_"
# DISTINCT prefix from the K2 pin key above (``dhw_warmup_hour_``): the pin
# readers (``_read_warmup_hour`` / ``_persisted_warmup_hour``) match the
# ``dhw_warmup_hour_`` prefix EXACTLY and can NEVER see a ``dhw_warmup_shadow_``
# row, so persisting the observational would-pick delta is zero-risk to the LP
# pin / fired warmup hour. Read only by ``read_warmup_shadow`` (the
# /status/feedback surface). "hour_" and "shadow_" share no prefix relationship.
_WARMUP_SHADOW_KEY_PREFIX = "dhw_warmup_shadow_"
_WARMUP_KEY_TTL_DAYS = 7


def _sweep_stale_warmup_keys(today: date) -> None:
    """Finding 4 (review #683): drop ``dhw_warmup_hour_<date>`` (K2 pin),
    ``dhw_warmup_shadow_<date>`` (observational would-pick) AND
    ``dhw_early_setback_<date>`` (drawdown fire time) rows older than ~7 days
    so the kv table can't accrue forever. Cheap + best-effort — a single
    ``list_runtime_settings`` scan on the once-a-day persist, never fatal."""
    try:
        cutoff = today - timedelta(days=_WARMUP_KEY_TTL_DAYS)
        for row in db.list_runtime_settings():
            key = str(row.get("key", ""))
            for prefix in (
                _WARMUP_KEY_PREFIX,
                _WARMUP_SHADOW_KEY_PREFIX,
                _EARLY_SETBACK_KEY_PREFIX,
            ):
                if not key.startswith(prefix):
                    continue
                try:
                    d = date.fromisoformat(key[len(prefix):])
                except ValueError:
                    break
                if d < cutoff:
                    db.delete_runtime_setting(key)
                break
    except Exception:  # pragma: no cover - defensive: sweep must not fail persist
        logger.debug("dhw_policy: stale warmup-key sweep failed", exc_info=True)


def _persist_warmup_hour(d: date, hour: int) -> None:
    try:
        db.set_runtime_setting(_warmup_setting_key(d), str(int(hour)))
        _sweep_stale_warmup_keys(d)
    except Exception:  # pragma: no cover - defensive
        logger.debug("dhw_policy: persist warmup hour failed for %s", d, exc_info=True)


def _warmup_shadow_key(d: date) -> str:
    """Runtime-settings key for the OBSERVATIONAL price-aware would-pick row.

    Uses ``_WARMUP_SHADOW_KEY_PREFIX`` — deliberately DISTINCT from the K2 pin
    key (``dhw_warmup_hour_``) so ``_read_warmup_hour``/``_persisted_warmup_hour``
    (which match ``dhw_warmup_hour_`` exactly) never read it. Write-only from the
    resolver; read only by ``read_warmup_shadow``."""
    return f"{_WARMUP_SHADOW_KEY_PREFIX}{d.isoformat()}"


def _persist_warmup_shadow(
    d: date,
    static_hour: int,
    chosen_hour: int,
    delta_pence: float | None,
    resolved_at: datetime | None = None,
) -> None:
    """Persist the observational would-pick row for local date *d* (idempotent
    upsert, no in-process dedup) so late/real-window re-solves refresh it.

    Written UNCONDITIONAL of ``_price_aware_enabled`` — the whole point is to
    accumulate the static→would-pick delta *while the feature is OFF*, toward a
    winter enable-decision. Never read by the K2 warmup-hour pin path (distinct
    key prefix), so it can never move the fired warmup hour or the LP pin."""
    when = (resolved_at or datetime.now(UTC)).astimezone(UTC)
    try:
        db.set_runtime_setting(
            _warmup_shadow_key(d),
            json.dumps({
                "static_hour": int(static_hour),
                "would_pick_hour": int(chosen_hour),
                "delta_pence": (
                    round(float(delta_pence), 3) if delta_pence is not None else None
                ),
                "enabled": _price_aware_enabled(),
                "resolved_at": when.isoformat().replace("+00:00", "Z"),
            }),
        )
    except Exception:  # pragma: no cover - defensive: shadow must not fail resolve
        logger.debug("dhw_policy: persist warmup shadow failed for %s", d, exc_info=True)


def read_warmup_shadow(d: date) -> dict[str, Any] | None:
    """The observational would-pick row for local date *d*, or None. Cheap kv
    read for the /status/feedback self-check surface. Shape:
    ``{static_hour, would_pick_hour, delta_pence, enabled, resolved_at}``."""
    try:
        raw = db.get_runtime_setting(_warmup_shadow_key(d))
    except Exception:  # pragma: no cover - defensive
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# --- Early setback on evening shower drawdown -------------------------------
# When the household's showers drain the tank (a fast ≥N °C drop during the
# evening window), holding the warmup target until the static 22:00 setback
# makes the Onecta firmware reheat the freshly-drawn tank IMMEDIATELY — at
# peak price, from the battery (~1.0-1.6 kWh measured, e.g. 2026-07-10:
# 38→45 °C finishing at 21:53, seven minutes before the setback). The K2 pin
# already models that reheat as DEFERRED to the next day's warmup
# (SHOWER_REHEAT slots are ~0.12 kWh; the warmup transition carries the
# deferred load), so pulling the setback forward to the detected drawdown
# aligns the firmware's behaviour with what the LP already budgets.
#
# Same persist-once discipline as the warmup hour above: the heartbeat
# detector (state_machine._check_dhw_shower_drawdown) persists
# ``dhw_early_setback_<YYYY-MM-DD>`` = fire time (UTC ISO) exactly once per
# local date; every re-plan then regenerates the cycle with the
# warmup→setback boundary moved up to it, and the K2 pin
# (forecast_dhw_load_per_slot) reads the same key — schedule, forecast and
# fired rows can never disagree. Keys are swept with the warmup keys (~7 d).
_EARLY_SETBACK_KEY_PREFIX = "dhw_early_setback_"


def _early_setback_key(d: date) -> str:
    return f"{_EARLY_SETBACK_KEY_PREFIX}{d.isoformat()}"


def read_early_setback(d: date) -> datetime | None:
    """The persisted early-setback fire time (UTC) for local date *d*, or None.

    Pure reader — never persists. Malformed or naive stored values are
    treated as absent (fail-safe: the static setback still fires at
    ``DHW_SETBACK_START_HOUR_LOCAL``)."""
    try:
        raw = db.get_runtime_setting(_early_setback_key(d))
    except Exception:  # pragma: no cover - defensive: reader must not fail
        return None
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if ts.tzinfo is None:
        return None
    return ts.astimezone(UTC)


def persist_early_setback(d: date, fired_at_utc: datetime) -> bool:
    """Persist the early-setback fire time for local date *d* — first write
    wins (re-checks the key so two heartbeat ticks can't both claim the
    fire). Returns True iff THIS call persisted the value."""
    if read_early_setback(d) is not None:
        return False
    try:
        db.set_runtime_setting(_early_setback_key(d), _iso_z(fired_at_utc))
        return True
    except Exception:  # pragma: no cover - defensive
        logger.debug(
            "dhw_policy: persist early setback failed for %s", d, exc_info=True,
        )
        return False


def build_early_setback_row(
    target_date_local: date, start_utc: datetime,
) -> dict[str, Any]:
    """The immediate ``tank_setback`` row the drawdown detector upserts so the
    setback fires on the NEXT heartbeat tick instead of waiting for a re-plan.

    Same shape and end boundary (next day's warmup start, DST-safe) as the row
    ``generate_daily_tank_schedule`` emits for this cycle once the persist-once
    key is set — so a later re-plan upserts onto this very row (identical
    natural key ``(daikin, tank_setback, start_time)``) instead of duplicating
    it. Plain ``DHW_TEMP_SETBACK_C`` target: the detector never fires when a
    negative-price boost overlaps the evening, so the pre-cool variant can't
    apply here.

    KNOWN LIMITATION (accepted): the end boundary freezes D+1's warmup hour
    as read AT FIRE TIME. If the price-aware resolver later persists a
    DIFFERENT hour for D+1 (rates land after ~16:00), this row's end is never
    refreshed (``upsert_action`` won't touch an in-flight/completed row), so
    it can overlap tomorrow's earlier warmup row. Harmless under the default
    ``PREFIRE_STATE_MATCH_ENABLED=true`` — this row goes terminal within a
    tick or two of firing, long before tomorrow's warmup — but if state-match
    is ever disabled, revisit (a live 37 °C row overlapping the warmup would
    thrash against it)."""
    tz = _tz_local()
    setback_c = int(round(float(getattr(config, "DHW_TEMP_SETBACK_C", 37))))
    next_day = target_date_local + timedelta(days=1)
    next_warmup = datetime(
        next_day.year, next_day.month, next_day.day,
        _read_warmup_hour(next_day), 0, tzinfo=tz,
    )
    return _make_action(
        action_type="tank_setback",
        start_utc=start_utc,
        end_utc=next_warmup.astimezone(UTC),
        tank_temp_c=setback_c,
    )


def _read_warmup_hour(d: date) -> int:
    """Read-only warmup hour for local date *d*: persisted-or-static.

    NEVER resolves or persists. When price-aware is disabled this ALWAYS
    returns the static hour (ignoring any stale persisted value), so the
    whole schedule/forecast/nominal stack is byte-identical to legacy.
    """
    if not _price_aware_enabled():
        return _static_warmup_hour()
    persisted = _persisted_warmup_hour(d)
    return persisted if persisted is not None else _static_warmup_hour()


def _price_and_real_maps(
    agile_rates: list[dict[str, Any]] | None,
) -> tuple[dict[datetime, float], set[datetime]]:
    """``agile_rates`` dicts → (``{slot_start_utc: import_pence}``, set of the
    slot starts that came from REAL Agile, not the horizon-extender's priors).

    A row is synthetic iff ``fetched_at == "prior"`` (the sentinel stamped by
    ``optimizer._resolve_plan_window``). Both real and prior prices go into the
    price map (so a shadow pick can still be computed), but only real slots
    enter ``real_slots`` — the persist gate keys off that set.
    """
    price_map: dict[datetime, float] = {}
    real_slots: set[datetime] = set()
    if not agile_rates:
        return price_map, real_slots
    for r in agile_rates:
        ts_raw = r.get("valid_from") or r.get("slot_time_utc")
        if not ts_raw:
            continue
        val = r.get("value_inc_vat")
        if val is None:
            val = r.get("rate_p")
        if val is None:
            continue
        try:
            ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00")).astimezone(UTC)
            price_map[ts] = float(val)
        except (ValueError, TypeError):
            continue
        if str(r.get("fetched_at")) != "prior":
            real_slots.add(ts)
    return price_map, real_slots


def _window_fully_real(target_date_local: date, real_slots: set[datetime]) -> bool:
    """True iff every 30-min slot of the candidate window [START, END) on
    ``target_date_local`` is covered by REAL Agile. This is the persist gate:
    a hour is only picked/frozen when the whole window it was chosen from is
    real — never from synthetic priors (Finding 1) or a truncated range
    (Finding 2)."""
    lo, hi = _warmup_window_bounds()
    if hi <= lo:
        return False
    tz = _tz_local()
    for h in range(lo, hi):
        for minute in (0, 30):
            s = datetime(
                target_date_local.year, target_date_local.month,
                target_date_local.day, h, minute, tzinfo=tz,
            ).astimezone(UTC)
            if s not in real_slots:
                return False
    return True


def _price_aware_pick(
    target_date_local: date,
    price_map: dict[datetime, float],
    static_hour: int,
) -> tuple[int | None, float | None]:
    """Cheapest candidate warmup hour + its saving vs the static hour.

    Returns ``(chosen_hour, delta_p_per_slot)``; ``(None, None)`` when no
    candidate hour has usable price data. ``delta`` = static mean − chosen mean
    (positive = price-aware is cheaper), or None when the static hour itself
    has no price. Tie-break: EARLIEST hour wins (we iterate ascending and only
    replace on a strictly-cheaper mean). The warmer-COP tie-break the issue
    mentions is deliberately NOT wired — dhw_policy exposes no cheap outdoor
    forecast accessor, so earliest-hour is the documented deterministic rule.
    """
    if not price_map:
        return None, None
    lo, hi = _warmup_window_bounds()
    if hi <= lo:
        return None, None
    tz = _tz_local()
    priced: dict[int, float] = {}
    best_hour: int | None = None
    best_price: float | None = None
    for h in range(lo, hi):
        # DST-safe anchor (same pattern as generate_daily_tank_schedule).
        s0 = datetime(
            target_date_local.year, target_date_local.month, target_date_local.day,
            h, 0, tzinfo=tz,
        ).astimezone(UTC)
        s1 = datetime(
            target_date_local.year, target_date_local.month, target_date_local.day,
            h, 30, tzinfo=tz,
        ).astimezone(UTC)
        vals = [price_map[s] for s in (s0, s1) if s in price_map]
        if not vals:
            continue
        mean_p = sum(vals) / len(vals)
        priced[h] = mean_p
        if best_price is None or mean_p < best_price - 1e-9:
            best_price, best_hour = mean_p, h
    if best_hour is None:
        return None, None
    static_p = priced.get(static_hour)
    delta = (static_p - best_price) if static_p is not None else None
    return best_hour, delta


def _shadow_log_warmup(
    target_date_local: date, static_hour: int, chosen: int | None, delta: float | None
) -> None:
    if chosen is None:
        return
    token = (target_date_local.isoformat(), static_hour, chosen)
    if token in _warmup_shadow_logged:
        return
    _warmup_shadow_logged.add(token)
    verb = "ENABLED, applying" if _price_aware_enabled() else "shadow (disabled)"
    delta_str = f"Δ≈{delta:+.2f}p/slot" if delta is not None else "Δ≈n/a"
    logger.info(
        "DHW warmup price-aware %s: date=%s static=%02d:00 price-aware=%02d:00 %s",
        verb, target_date_local.isoformat(), static_hour, chosen, delta_str,
    )


def resolve_warmup_hour_local(
    target_date_local: date,
    agile_rates: list[dict[str, Any]] | None = None,
) -> int:
    """Resolve + (when appropriate) persist the LOCAL warmup START hour for
    ``target_date_local`` (#681). THE WRITER — call only from the LP-solve path.

    * Already persisted for this date → the persisted value verbatim
      (persist-once: stability preserves the restore covenant + K2 pin
      coherence across re-plans at different wall-clock times).
    * The candidate window [START, END) is NOT fully covered by REAL Agile
      (D+1 tail is still priors, or a truncated price range) → static fallback,
      NOT persisted and NOT shadow-logged, so the resolve re-runs (and can then
      freeze from real rates) once the real rates arrive.
    * Window fully real-covered → cheapest candidate hour; shadow-logged; then
      persisted+returned when the feature is ENABLED, or the static hour
      (byte-identical) when disabled.
    """
    static_hour = _static_warmup_hour()

    if _price_aware_enabled():
        persisted = _persisted_warmup_hour(target_date_local)
        if persisted is not None:
            return persisted

    price_map, real_slots = _price_and_real_maps(agile_rates)
    # Persist gate: only trust (and freeze) a pick drawn from a window that is
    # ENTIRELY real Agile. Priors / truncated ranges → static, no persist.
    if not _window_fully_real(target_date_local, real_slots):
        return static_hour

    chosen, delta = _price_aware_pick(target_date_local, price_map, static_hour)
    _shadow_log_warmup(target_date_local, static_hour, chosen, delta)
    # Persist the observational would-pick row — UNCONDITIONAL of the enable
    # flag (still inside the fully-real window gate) so the static→would-pick
    # delta accrues toward a winter enable-decision. Distinct key prefix keeps
    # it invisible to the K2 pin path; idempotent upsert refreshes on re-solve.
    if chosen is not None:
        _persist_warmup_shadow(target_date_local, static_hour, chosen, delta)

    if not _price_aware_enabled() or chosen is None:
        return static_hour
    _persist_warmup_hour(target_date_local, chosen)
    return chosen


def resolve_warmup_hours_for_horizon(
    slot_starts_utc: list[datetime],
    agile_rates: list[dict[str, Any]] | None,
) -> dict[date, int]:
    """Resolve+persist the warmup hour for every LOCAL date spanned by the LP
    horizon (the single-writer entry point called by ``_run_optimizer_lp``).
    Returns ``{local_date: resolved_hour}`` for diagnostics/tests. Idempotent
    via persist-once; a no-op (all static) when the feature is disabled."""
    tz = _tz_local()
    out: dict[date, int] = {}
    seen: set[date] = set()
    for s in slot_starts_utc:
        d = s.astimezone(tz).date()
        if d in seen:
            continue
        seen.add(d)
        out[d] = resolve_warmup_hour_local(d, agile_rates)
    return out


def generate_daily_tank_schedule(
    target_date_local: date,
    *,
    agile_rates: list[dict[str, Any]] | None = None,
    mode: str | None = None,
    allow_past: bool = False,
    boosts_only_as_of: datetime | None = None,
) -> list[dict[str, Any]]:
    """Generate tank action rows for the local calendar day starting at
    ``DHW_WARMUP_START_HOUR_LOCAL`` (default 13:00) on ``target_date_local``
    and ending at the same time the following day.

    The window matches the user's mental model: "a day's tank cycle starts
    at the afternoon warmup and ends at the next afternoon warmup". This
    way one call covers an entire warmup → setback → next-warmup cycle.

    Args:
        target_date_local: anchor day in local TZ
        agile_rates: optional list of {valid_from, value_inc_vat}; used
            to detect negative-price windows for boost overrides
        mode: optimization preset; defaults to ``config.OPTIMIZATION_PRESET``
        boosts_only_as_of: when set, return ONLY this cycle's
            ``tank_negative_boost`` rows, dropping any window that has fully
            ended (``end <= boosts_only_as_of``) and keeping each remaining
            window at its NATURAL start (NOT clipped to the as-of time). The
            stable start keeps the writer idempotent: ``upsert_action`` keys on
            ``(device, action_type, start_time)``, so re-plans refresh the one
            row instead of accumulating a fresh row per advancing clip point.
            Used by the writer to recover the early-morning paid boost of the
            *currently-live* cycle (anchored at yesterday's warmup) that the
            cycle-split otherwise drops on an overnight re-plan — the
            2026-06-07 paid-window incident. Structural warmup/setback rows are
            deliberately NOT re-emitted: they already fired when the cycle
            began, and a fresh setback sharing the boost's start would thrash
            against it (neither row ever reaches a state-matched "completed").

    Returns:
        List of action dicts; empty when ``mode='vacation'``.
    """
    if mode is None:
        mode = (config.OPTIMIZATION_PRESET or "normal").strip().lower()

    if mode == "vacation":
        return []

    # Past-date guard (K1.1 bug #5): generating rows for yesterday is
    # always a no-op for the WRITER — the heartbeat would try to fire them
    # immediately and they'd be wasted churn. Tomorrow is fine (advance
    # scheduling). ``allow_past=True`` lets read-only callers (e.g. the
    # heating-plan timeline endpoint) regenerate a past day's deterministic
    # schedule for display without writing anything. ``boosts_only_as_of``
    # deliberately reaches back into the live (yesterday-anchored) cycle, so it
    # also bypasses this guard — it drops only windows that have fully ended.
    today_local = datetime.now(_tz_local()).date()
    if boosts_only_as_of is None and not allow_past and target_date_local < today_local:
        logger.info(
            "dhw_policy: skipping %s (in the past; today=%s)",
            target_date_local, today_local,
        )
        return []

    tz = _tz_local()
    # Price-aware warmup start (#681) — READER ONLY (persisted-or-static).
    # Persistence happens exclusively in the LP-solve path (single writer,
    # review #683); a dispatch/display caller must never freeze an hour from
    # its own (static-anchored, possibly truncated) rate fetch. next_day is
    # read independently so the setback END aligns with tomorrow's warmup start.
    warmup_hour = _read_warmup_hour(target_date_local)
    setback_hour = int(getattr(config, "DHW_SETBACK_START_HOUR_LOCAL", 22))
    normal_c = int(round(float(config.DHW_TEMP_NORMAL_C)))
    setback_c = int(round(float(getattr(config, "DHW_TEMP_SETBACK_C", 37))))
    # Negative-price boost target → MAX (free money to heat). Clamp to DHW_TEMP_MAX_C.
    boost_c = int(round(min(
        float(getattr(config, "DHW_NEGATIVE_PRICE_BOOST_C", 65)),
        float(config.DHW_TEMP_MAX_C),
    )))

    # DST-safe anchor construction (K1.1 bug #3 fix). Building each
    # boundary explicitly via ``datetime(..., tzinfo=tz)`` lets ZoneInfo
    # pick the correct UTC offset for that wall-clock moment. Avoid
    # ``.replace(hour=...)`` and ``+timedelta(days=1)`` here because
    # ``timedelta`` is offset-blind and ``.replace`` keeps the source
    # tzinfo even when DST has flipped between the two times.
    next_day = target_date_local + timedelta(days=1)
    next_warmup_hour = _read_warmup_hour(next_day)
    warmup_start = datetime(
        target_date_local.year, target_date_local.month, target_date_local.day,
        warmup_hour, 0, tzinfo=tz,
    )
    setback_start = datetime(
        target_date_local.year, target_date_local.month, target_date_local.day,
        setback_hour, 0, tzinfo=tz,
    )
    next_warmup = datetime(
        next_day.year, next_day.month, next_day.day,
        next_warmup_hour, 0, tzinfo=tz,
    )

    # Negative-price boost windows are the sole permitted exception to the fixed
    # schedule. Detect them FIRST so they can genuinely SUPERSEDE the leading
    # warmup (not just sit alongside it): we must never pre-heat to normal_c at a
    # positive price right before a free, paid-to-import boost to boost_c.
    warmup_start_utc = warmup_start.astimezone(UTC)
    setback_start_utc = setback_start.astimezone(UTC)
    next_warmup_utc = next_warmup.astimezone(UTC)

    # Early setback on shower drawdown: once the heartbeat detector persisted a
    # fire time for this cycle, the warmup→setback boundary moves up to it on
    # EVERY regeneration (re-plans included), so the fired early row and the
    # regenerated schedule share the same boundary and upsert key. Clamped
    # strictly inside (warmup_start, setback_start): a bogus/foreign key can
    # only ever SHORTEN the warmup, never invert the cycle or extend it.
    # Guests mode ignores the key entirely (no setback concept — morning
    # showers possible).
    if mode != "guests":
        _early_ts = read_early_setback(target_date_local)
        if _early_ts is not None and warmup_start_utc < _early_ts < setback_start_utc:
            setback_start_utc = _early_ts

    neg_windows = _detect_negative_windows(agile_rates, warmup_start_utc, next_warmup_utc)

    # Boosts-only recovery path: emit just the negative-boost rows of this
    # (already-running) cycle. Drop windows that have fully ended; keep the rest
    # at their NATURAL start (a stable upsert key → no per-re-plan accumulation,
    # see the param docstring). A still-running window keeps its past start; the
    # reconciler fires it on the next tick (start <= now < end) and state-match
    # de-dupes thereafter. No structural rows.
    if boosts_only_as_of is not None:
        boost_rows: list[dict[str, Any]] = []
        for nw_start, nw_end in neg_windows:
            if nw_end <= boosts_only_as_of:
                continue  # window already fully elapsed
            boost_rows.append(_make_action(
                action_type="tank_negative_boost",
                start_utc=nw_start,
                end_utc=nw_end,
                tank_temp_c=boost_c,
                tank_powerful=True,
            ))
        return boost_rows

    # If a boost window opens at/near the warmup start, defer the warmup past it
    # (chaining consecutive boosts that each fall within the lead window of the
    # running start). The boost does the heating during the paid window; the
    # warmup then resumes its hold-at-normal_c role afterwards — the tank simply
    # coasts down from boost_c, so no positive-price heating happens until it
    # falls below normal_c. A boost LATER in the day (beyond the lead window of
    # the warmup start) leaves the warmup intact: the afternoon still needs hot
    # water before that boost arrives.
    #
    # The lead window is LP_PRE_NEGATIVE_PRECOOL_HOURS — deliberately the SAME
    # window the LP's energy forecast (forecast_dhw_load_per_slot) uses to
    # pre-cool: the forecast zeroes warmup energy for slots within precool_hours
    # before a negative window, so deferring the fired warmup over exactly that
    # window keeps the actions and the budgeted DHW import consistent. (The LP
    # suppresses the warmup-start slot iff boost_start <= warmup_start +
    # precool — identical to this defer condition.)
    effective_warmup_start_utc = warmup_start_utc
    defer_lead = timedelta(hours=max(
        0.0, float(getattr(config, "LP_PRE_NEGATIVE_PRECOOL_HOURS", 3.0))
    ))
    if neg_windows and defer_lead > timedelta(0):
        for nw_start, nw_end in sorted(neg_windows):
            if nw_start <= effective_warmup_start_utc + defer_lead:
                effective_warmup_start_utc = max(effective_warmup_start_utc, nw_end)
            else:
                break

    # Tank pre-cool into a negative window: drop the setback target toward the
    # device minimum so the paid boost (cold → boost_c) absorbs the most kWh and
    # no positive-price reheat fires just before it. Guarded by no shower in the
    # setback→first-boost span (the boost reheats to boost_c before any later
    # shower). PHYSICS CAVEAT: standing loss (~0.5 °C/h) caps real cooling — for
    # a window soon after the warmup the tank is still coasting well above the
    # target, so the gain is small; the value is mostly far-from-warmup windows
    # + the guaranteed no-reheat-before-paid.
    effective_setback_c = setback_c
    if (
        getattr(config, "DHW_TANK_PRECOOL_ENABLED", False)
        and mode != "guests"
        and neg_windows
    ):
        first_neg_start = min(nw[0] for nw in neg_windows)
        if setback_start_utc <= first_neg_start and not _shower_in_span(
            setback_start_utc, first_neg_start, tz
        ):
            effective_setback_c = min(
                setback_c, int(getattr(config, "DHW_TANK_PRECOOL_TARGET_C", 30))
            )

    rows: list[dict[str, Any]] = []

    if mode == "guests":
        # Single 24h warmup row — no setback during guest visits because of
        # potential morning showers. Skip entirely if a boost chain has deferred
        # the start past the window end.
        if effective_warmup_start_utc < next_warmup_utc:
            rows.append(_make_action(
                action_type="tank_warmup",
                start_utc=effective_warmup_start_utc,
                end_utc=next_warmup_utc,
                tank_temp_c=normal_c,
            ))
    else:
        # Normal mode: warmup → setback → next-day warmup pattern. The warmup is
        # emitted only when it still has positive duration after any boost defer.
        if effective_warmup_start_utc < setback_start_utc:
            rows.append(_make_action(
                action_type="tank_warmup",
                start_utc=effective_warmup_start_utc,
                end_utc=setback_start_utc,
                tank_temp_c=normal_c,
            ))
        rows.append(_make_action(
            action_type="tank_setback",
            start_utc=setback_start_utc,
            end_utc=next_warmup_utc,
            tank_temp_c=effective_setback_c,
        ))

    for nw_start, nw_end in neg_windows:
        rows.append(_make_action(
            action_type="tank_negative_boost",
            start_utc=nw_start,
            end_utc=nw_end,
            tank_temp_c=boost_c,
            tank_powerful=True,  # grid pays us — load all the kWh
        ))

    return rows


# --- Per-slot electric draws (kWh / 30 min), schedule phases ---------------
# Recalibrated 2026-06 (#534) against measured Onecta 2-hourly splits: almost
# all DHW electric lands in the 12:00-16:00 warmup window (~1.4-1.8 kWh);
# evening shower slots only draw ~0.35-0.45 kWh TOTAL because the firmware's
# hysteresis lets the tank ride through the draws and repays the heat at the
# NEXT day's warmup. The old shape (0.50/slot in shower windows) reserved
# ~2 kWh of battery for a phantom evening load and under-credited the real
# PV-window load.
_WARMUP_TRANSITION_KWH = 0.45   # × 2 slots (13:00 + 13:30) = deferred reheat + lift
_WARMUP_MAINTENANCE_KWH = 0.06
_SHOWER_REHEAT_KWH = 0.12       # per slot during shower window
_SETBACK_MAINTENANCE_KWH = 0.03
_BOOST_KWH = 0.80               # physics-bound (max heating), NOT auto-scaled
_VACATION_KWH = 0.00            # firmware-only; legionella cycle excluded from LP horizon

# Shower windows (local hours, slot-start basis). 20:00→22:00 covers the
# household's "after-dinner shower" pattern; guests adds 07:00→09:00.
_EVENING_SHOWER_START_H = 20
_EVENING_SHOWER_END_H = 22  # exclusive
_GUESTS_MORNING_SHOWER_START_H = 7
_GUESTS_MORNING_SHOWER_END_H = 9  # exclusive

# 6 h TTL cache for the auto-scale factor — one cheap DB read per LP-solve
# burst instead of per call. Keyed by (mode, window_days).
_autoscale_cache: dict[tuple[str, int, str, int], tuple[float, float]] = {}
_AUTOSCALE_TTL_SECONDS = 6 * 3600


def _nominal_daily_total_kwh(mode: str, warmup_hour: int | None = None) -> float:
    """Un-scaled kWh/day the schedule constants imply for ``mode``.

    Walks a generic 48-slot local day through the same phase rules as
    :func:`forecast_dhw_load_per_slot` (no boost, no warm credit) so the
    auto-scale denominator can never drift from the forecast shape.

    ``warmup_hour`` defaults to TODAY's resolved warmup hour (#681) so the
    auto-scale denominator tracks the price-aware start; when price-aware is
    disabled this is the static hour and the total is byte-identical to legacy.
    Moving the warmup earlier lengthens the warmup window (more maintenance
    slots), so the nominal total MUST use the same hour the forecast does.
    """
    if mode == "vacation":
        return 0.0
    if warmup_hour is None:
        warmup_hour = _read_warmup_hour(datetime.now(_tz_local()).date())
    setback_hour = int(getattr(config, "DHW_SETBACK_START_HOUR_LOCAL", 22))
    total = 0.0
    for half_slot in range(48):
        h = half_slot // 2
        if _EVENING_SHOWER_START_H <= h < _EVENING_SHOWER_END_H or (
            mode == "guests"
            and _GUESTS_MORNING_SHOWER_START_H <= h < _GUESTS_MORNING_SHOWER_END_H
        ):
            total += _SHOWER_REHEAT_KWH
        elif mode == "guests":
            total += _WARMUP_MAINTENANCE_KWH
        elif warmup_hour <= h < setback_hour:
            # Both half-slots of the warmup hour are transition slots —
            # mirrors _phase_for_slot.
            total += (
                _WARMUP_TRANSITION_KWH if h == warmup_hour else _WARMUP_MAINTENANCE_KWH
            )
        else:
            total += _SETBACK_MAINTENANCE_KWH
    return total


def _nominal_bucket_shares(mode: str, warmup_hour: int | None = None) -> dict[int, float]:
    """Nominal energy fraction per LOCAL 2h bucket (0-11) for ``mode`` — the
    same 48-slot walk as :func:`_nominal_daily_total_kwh`, aggregated by
    ``hour // 2`` (the ``dhw_error_log`` bucketing). Used by the bucket-bias
    corrector to normalize its factors so applying them preserves the daily
    total: the auto-scale owns the LEVEL and is open-loop w.r.t. the committed
    forecast, so an un-normalized shape corrector would double-correct the
    level, permanently. Returns ``{}`` for vacation (nominal total is 0).

    ``warmup_hour`` defaults to TODAY's resolved warmup hour (#681) — the SAME
    hour the auto-scale denominator uses — so the bias-normalizer and the
    auto-scale never disagree on where the warmup window sits."""
    if mode == "vacation":
        return {}
    if warmup_hour is None:
        warmup_hour = _read_warmup_hour(datetime.now(_tz_local()).date())
    setback_hour = int(getattr(config, "DHW_SETBACK_START_HOUR_LOCAL", 22))
    per_bucket: dict[int, float] = {b: 0.0 for b in range(12)}
    total = 0.0
    for half_slot in range(48):
        h = half_slot // 2
        if _EVENING_SHOWER_START_H <= h < _EVENING_SHOWER_END_H or (
            mode == "guests"
            and _GUESTS_MORNING_SHOWER_START_H <= h < _GUESTS_MORNING_SHOWER_END_H
        ):
            kwh = _SHOWER_REHEAT_KWH
        elif mode == "guests":
            kwh = _WARMUP_MAINTENANCE_KWH
        elif warmup_hour <= h < setback_hour:
            kwh = _WARMUP_TRANSITION_KWH if h == warmup_hour else _WARMUP_MAINTENANCE_KWH
        else:
            kwh = _SETBACK_MAINTENANCE_KWH
        per_bucket[h // 2] += kwh
        total += kwh
    if total <= 0:
        return {}
    return {b: v / total for b, v in per_bucket.items()}


def _dhw_autoscale_factor(mode: str) -> float:
    """Trailing measured-vs-nominal scale for the schedule constants (#534).

    ``clamp(median(daikin_consumption_daily.kwh_dhw over the window) /
    nominal_mode_total)``. Median is robust to negative-boost outlier days
    (e.g. 7 kWh on 2026-06-07) and to Onecta's integer-kWh rounding. Returns
    1.0 when disabled, in vacation mode, or with fewer than
    ``DHW_FORECAST_AUTOSCALE_MIN_DAYS`` measured days.
    """
    if mode == "vacation":
        return 1.0
    if not bool(getattr(config, "DHW_FORECAST_AUTOSCALE_ENABLED", True)):
        return 1.0
    window_days = int(getattr(config, "DHW_FORECAST_AUTOSCALE_WINDOW_DAYS", 14))
    # The denominator (nominal) depends on the resolved warmup hour (#681) —
    # a price-aware move changes the warmup-window slot count. Read TODAY's
    # hour and key the cache on it so a mid-day change re-computes; disabled →
    # static hour → identical key/nominal to legacy.
    # Finding 3 (review #683, accepted): this uses TODAY's hour even for the
    # slice of the horizon that is tomorrow (which may resolve to a different
    # hour). The drift is a scalar level nudge, bounded by the autoscale clamp
    # [0.5, 1.6] — it can never make the LP Infeasible and self-corrects the
    # next day, so it is left as-is rather than made per-date.
    warmup_hour = _read_warmup_hour(datetime.now(_tz_local()).date())
    # DB_PATH in the key: tests swap databases under one process, and prod
    # never changes it — costs nothing, prevents cross-DB cache bleed.
    key = (mode, window_days, str(getattr(config, "DB_PATH", "")), warmup_hour)
    cached = _autoscale_cache.get(key)
    now = _time.time()
    if cached is not None and now - cached[1] < _AUTOSCALE_TTL_SECONDS:
        return cached[0]

    factor = 1.0
    try:
        tz = _tz_local()
        end = (datetime.now(tz) - timedelta(days=1)).date()
        start = end - timedelta(days=window_days - 1)
        rows = db.get_daikin_consumption_daily_range(start.isoformat(), end.isoformat())
        vals = [float(r["kwh_dhw"]) for r in rows if r.get("kwh_dhw") is not None]
        min_days = int(getattr(config, "DHW_FORECAST_AUTOSCALE_MIN_DAYS", 5))
        nominal = _nominal_daily_total_kwh(mode, warmup_hour)
        if len(vals) >= min_days and nominal > 0:
            lo = float(getattr(config, "DHW_FORECAST_AUTOSCALE_MIN", 0.5))
            hi = float(getattr(config, "DHW_FORECAST_AUTOSCALE_MAX", 1.6))
            # #721 — truncation-bias correction. The Onecta daily counter TRUNCATES
            # to whole kWh (a 0.6 kWh reheat reads 0 — see daikin/service.py, #425),
            # so the median under-reads by half the quantisation step on average.
            # Uncorrected, the current window's raw ratio (median 1.0 / nominal 3.0
            # = 0.33) slams into the 0.5 clamp floor — which happens to equal the
            # TRUE ratio ((1.0+0.5)/3.0 = 0.5), so the forecast was right only by
            # coincidence, and any drift in nominal (guests, a price-aware warmup
            # hour changing the slot count) or in usage would silently break it.
            # Correct the numerator; the clamp goes back to being a safety bound.
            trunc = float(getattr(config, "DHW_COUNTER_TRUNCATION_KWH", 1.0)) / 2.0
            factor = max(lo, min(hi, (statistics.median(vals) + trunc) / nominal))
            # The numerator is mode-blind (measured days don't say which mode
            # they ran in) while the denominator is mode-aware. Right after a
            # normal→guests flip the median still reflects normal-mode days
            # and would scale the (higher) guests nominal DOWN — under-pinning
            # DHW exactly when the house is fullest. Guests is comfort-
            # critical: never scale it below the unscaled constants.
            if mode == "guests":
                factor = max(factor, 1.0)
            logger.debug(
                "dhw_policy autoscale: mode=%s median=%.2f nominal=%.2f factor=%.3f (n=%d)",
                mode, statistics.median(vals), nominal, factor, len(vals),
            )
    except Exception as exc:  # pragma: no cover - defensive: forecast must not fail
        logger.debug("dhw_policy autoscale failed, using 1.0: %s", exc)
        factor = 1.0

    _autoscale_cache[key] = (factor, now)
    return factor


def dhw_budget_state(mode: str | None = None) -> dict[str, Any]:
    """Public snapshot of the DHW budget feedback loop (#534) for the API.

    The status endpoints must not import underscore-private internals, so
    this composes them here: nominal mode total × trailing measured/nominal
    auto-scale = the daily electric budget the LP actually pins, plus the
    measured Onecta dhw figures it is being compared against.
    """
    if mode is None:
        mode = (config.OPTIMIZATION_PRESET or "normal").strip().lower()
    nominal = _nominal_daily_total_kwh(mode)
    factor = _dhw_autoscale_factor(mode)

    measured_today: float | None = None
    measured_7d_avg: float | None = None
    try:
        tz = _tz_local()
        today = datetime.now(tz).date()
        start = today - timedelta(days=7)
        rows = db.get_daikin_consumption_daily_range(start.isoformat(), today.isoformat())
        vals = [
            float(r["kwh_dhw"]) for r in rows
            if r.get("kwh_dhw") is not None and str(r["date"]) != today.isoformat()
        ]
        if vals:
            measured_7d_avg = round(sum(vals) / len(vals), 2)
        for r in rows:
            if str(r["date"]) == today.isoformat() and r.get("kwh_dhw") is not None:
                measured_today = float(r["kwh_dhw"])
    except Exception:  # pragma: no cover - defensive: status read must not fail
        logger.debug("dhw_budget_state: measured read failed", exc_info=True)

    bias_factors: dict[str, float] = {}
    bias_in_force: dict[str, float] = {}
    try:
        from .dhw_bias import factors_in_force
        raw = db.get_dhw_bucket_bias()
        bias_factors = {str(b): round(f, 3) for b, f in sorted(raw.items())}
        bias_in_force = {
            str(b): round(f, 3) for b, f in sorted(factors_in_force(mode).items())
        }
    except Exception:  # pragma: no cover - defensive: status read must not fail
        logger.debug("dhw_budget_state: bucket-bias read failed", exc_info=True)

    return {
        "mode": mode,
        "nominal_kwh": round(nominal, 2),
        "autoscale_factor": round(factor, 3),
        "autoscale_enabled": bool(getattr(config, "DHW_FORECAST_AUTOSCALE_ENABLED", True)),
        "effective_budget_kwh": round(nominal * factor, 2),
        "measured_today_kwh": measured_today,
        "measured_7d_avg_kwh": measured_7d_avg,
        "bucket_bias_enabled": bool(getattr(config, "DHW_BUCKET_BIAS_ENABLED", False)),
        "bucket_bias_factors": bias_factors,
        "bucket_bias_in_force": bias_in_force,  # {} unless enabled+normal+fresh
    }


def forecast_dhw_load_per_slot(
    slot_starts_utc: list[datetime],
    *,
    mode: str | None = None,
    target_date_local: date | None = None,
    initial_tank_c: float | None = None,
    price_line: list[float] | None = None,
) -> tuple[list[float], list[float]]:
    """Forecast the electric DHW load + tank temperature **trajectory** the
    fixed-schedule policy implies over the given LP horizon.

    Returns ``(e_dhw_kwh_per_slot, tank_temp_c_per_boundary)``:
        * ``e_dhw_kwh_per_slot[i]`` — predicted heat-pump electric draw
          for DHW during slot ``i`` (kWh per 30-min slot).
        * ``tank_temp_c_per_boundary[k]`` — predicted tank °C at slot
          boundary ``k`` (so ``len = N+1``, matching LP's ``tank[]``).

    The model is intentionally simple — we don't optimize anything here,
    just describe what Daikin firmware will plausibly do under the
    dhw_policy schedule. Used by the LP solver to pin its tank/e_dhw
    decision variables instead of letting it drift from reality.

    Energy model (typical 200 L tank, COP ~3.0; recalibrated 2026-06 #534
    against measured Onecta 2-hourly splits):
        * Warmup transition (both half-slots of the warmup hour): the
          SETBACK→NORMAL lift PLUS the previous evening's deferred shower
          reheat — measured 12:00-16:00 local carries ~1.4-1.8 kWh, the
          dominant load. 2 × 0.45 kWh electric.
        * Steady-state warmup at NORMAL, no draws: ~0.06 kWh/slot.
        * Shower window slots: ~0.12 kWh/slot — firmware hysteresis lets
          the tank ride through the draws (measured 20:00-22:00 total is
          only ~0.35-0.45 kWh); the heat is repaid at the next warmup.
        * Setback at 37 °C: ~0.03 kWh/slot.
        * Negative-price boost slot: ~0.8 kWh electric (max heating).
        * Vacation: 0 kWh (firmware-only; legionella out of horizon).

    Daily total: ~3.0 kWh normal / ~3.4 guests before auto-scale. The
    schedule constants are further multiplied by the trailing
    measured/nominal auto-scale (:func:`_dhw_autoscale_factor`) so the
    pinned forecast tracks seasonal drift (May 2026 measured ~3.0,
    June ~2.0-2.4) instead of going stale.
    """
    if mode is None:
        mode = (config.OPTIMIZATION_PRESET or "normal").strip().lower()

    n = len(slot_starts_utc)
    if n == 0:
        return [], []

    tz = _tz_local()
    setback_hour = int(getattr(config, "DHW_SETBACK_START_HOUR_LOCAL", 22))
    # Price-aware warmup start (#681) — READER ONLY. The K2 pin must agree with
    # the fired warmup row, so it reads exactly the persist-once value the
    # single writer (optimizer._run_optimizer_lp) froze for each local date.
    # The horizon spans ~2 local days that can carry different warmup hours, so
    # look the hour up per slot's LOCAL date. Disabled → static for every date
    # (byte-identical). NO resolve/persist here (a scenario solve must not race
    # a fresh persist from a perturbed price line).
    _warmup_by_date: dict[date, int] = {}
    for _s in slot_starts_utc:
        _d = _s.astimezone(tz).date()
        if _d not in _warmup_by_date:
            _warmup_by_date[_d] = _read_warmup_hour(_d)

    def _warmup_hour_for(slot_local: datetime) -> int:
        return _warmup_by_date.get(slot_local.date(), _static_warmup_hour())

    # Early setback on shower drawdown — READER ONLY, same lockstep rule as the
    # warmup hour above: once the detector persisted a fire time for a local
    # date, every slot of that date at/after it is a SETBACK slot (including
    # the remaining shower-window slots — the showers already happened; that's
    # what the detector detected), so the pin matches the pulled-forward
    # setback row instead of budgeting a phantom evening reheat. Per-date
    # cache: the horizon spans ~2 local dates → ≤2 kv reads per call. Only
    # normal mode — guests keeps its 24 h warmup, vacation forecasts ~0.
    # SAME CLAMP as the generator: a key outside that date's
    # (warmup_start, setback_start) window is ignored — otherwise a bogus/
    # foreign key rejected by K1 (schedule keeps the tank at NORMAL) would
    # still de-budget the whole day here, and the pin would starve the real
    # warmup of battery. K1 and K2 must reject in lockstep too.
    _early_by_date: dict[date, datetime | None] = {}
    if mode == "normal":
        for _s in slot_starts_utc:
            _d = _s.astimezone(tz).date()
            if _d in _early_by_date:
                continue
            _ts = read_early_setback(_d)
            if _ts is not None:
                _w_start = datetime(
                    _d.year, _d.month, _d.day, _warmup_by_date[_d], 0, tzinfo=tz,
                ).astimezone(UTC)
                _s_start = datetime(
                    _d.year, _d.month, _d.day, setback_hour, 0, tzinfo=tz,
                ).astimezone(UTC)
                if not (_w_start < _ts < _s_start):
                    _ts = None
            _early_by_date[_d] = _ts

    def _early_setback_active(slot_utc: datetime, slot_local: datetime) -> bool:
        _ts = _early_by_date.get(slot_local.date())
        return _ts is not None and slot_utc >= _ts

    normal_c = float(config.DHW_TEMP_NORMAL_C)
    setback_c = float(getattr(config, "DHW_TEMP_SETBACK_C", 37.0))
    boost_c = float(getattr(config, "DHW_NEGATIVE_PRICE_BOOST_C", 60.0))

    # Schedule constants × trailing measured/nominal auto-scale (#534). The
    # scaled values feed every phase INCLUDING the pre-cool/boost caps below
    # so the whole trajectory stays internally consistent. BOOST stays
    # physics-bound (max heating) — see _BOOST_KWH.
    _scale = _dhw_autoscale_factor(mode)
    WARMUP_TRANSITION_KWH = _WARMUP_TRANSITION_KWH * _scale
    WARMUP_MAINTENANCE_KWH = _WARMUP_MAINTENANCE_KWH * _scale
    SHOWER_REHEAT_KWH = _SHOWER_REHEAT_KWH * _scale
    SETBACK_MAINTENANCE_KWH = _SETBACK_MAINTENANCE_KWH * _scale
    BOOST_KWH = _BOOST_KWH
    VACATION_KWH = _VACATION_KWH

    EVENING_SHOWER_START_H = _EVENING_SHOWER_START_H
    EVENING_SHOWER_END_H = _EVENING_SHOWER_END_H
    GUESTS_MORNING_SHOWER_START_H = _GUESTS_MORNING_SHOWER_START_H
    GUESTS_MORNING_SHOWER_END_H = _GUESTS_MORNING_SHOWER_END_H

    def _phase_for_slot(slot_utc: datetime) -> str:
        """Return one of: 'vacation', 'warmup_transition', 'shower_reheat',
        'warmup_maintenance', 'setback'."""
        if mode == "vacation":
            return "vacation"
        slot_local = slot_utc.astimezone(tz)
        h = slot_local.hour

        # Early setback beats even the shower window: the drawdown the
        # detector saw IS the evening's showers, so the rest of this local
        # date is setback (mirrors the pulled-forward tank_setback row).
        if _early_setback_active(slot_utc, slot_local):
            return "setback"

        # Shower windows take priority — biggest load contributor.
        if EVENING_SHOWER_START_H <= h < EVENING_SHOWER_END_H:
            return "shower_reheat"
        if (mode == "guests"
                and GUESTS_MORNING_SHOWER_START_H <= h < GUESTS_MORNING_SHOWER_END_H):
            return "shower_reheat"

        if mode == "guests":
            # Guests: tank always at NORMAL outside shower windows →
            # warmup-level maintenance.
            return "warmup_maintenance"

        # Normal mode: warmup window [warmup_hour, setback_hour), setback
        # otherwise. warmup_hour is the price-aware pick for THIS slot's local
        # date (#681) so the pin follows the fired warmup row exactly.
        warmup_hour = _warmup_hour_for(slot_local)
        if warmup_hour <= h < setback_hour:
            # Both half-slots of the warmup hour are transition (#534): the
            # measured lift + deferred-reheat load spans ~1 h, not 30 min.
            if h == warmup_hour:
                return "warmup_transition"
            return "warmup_maintenance"
        return "setback"

    e_dhw: list[float] = []
    phases: list[str] = []
    for slot in slot_starts_utc:
        phase = _phase_for_slot(slot)
        phases.append(phase)
        if phase == "vacation":
            e_dhw.append(VACATION_KWH)
        elif phase == "warmup_transition":
            e_dhw.append(WARMUP_TRANSITION_KWH)
        elif phase == "shower_reheat":
            e_dhw.append(SHOWER_REHEAT_KWH)
        elif phase == "warmup_maintenance":
            e_dhw.append(WARMUP_MAINTENANCE_KWH)
        else:  # setback
            e_dhw.append(SETBACK_MAINTENANCE_KWH)

    # ----- Per-bucket shape correction (dhw_bucket_bias) -------------------
    # Open-loop factors learned nightly from dhw_error_log; normalized so the
    # daily total is untouched (the auto-scale above owns the level — an
    # un-normalized shape factor would double-correct it; see src/dhw_bias.py).
    # factors_in_force gates on enabled + mode == "normal" + table freshness —
    # guests is comfort-critical (a summer-learned factor would shrink the
    # morning-shower budget) and vacation forecasts ~0, so both stay raw.
    # Deliberately BEFORE the _max_hp_kwh clamp below: the LP pins these
    # values as hard equalities, so a boosted bucket must still be capped at
    # the heater's per-slot capacity. The negative-price boost ramp further
    # down OVERWRITES its slots after this point, so boost energy is never
    # scaled. Bucket factors intentionally absorb warmup/decay TIMING error
    # (tank thermal inertia spills the ramp across buckets) — that's the
    # point, not a bug.
    try:
        from .dhw_bias import factors_in_force
        _bias = factors_in_force(mode)
        if _bias:
            e_dhw = [
                v * _bias.get(slot.astimezone(tz).hour // 2, 1.0)
                for v, slot in zip(e_dhw, slot_starts_utc)
            ]
    except Exception as _exc:  # pragma: no cover - forecast must not fail
        logger.debug("dhw_policy: bucket-bias apply failed, using 1.0: %s", _exc)

    # The LP pins ``e_dhw[i] == forecast[i]`` as a hard equality against a
    # variable whose upBound is the heater's per-slot electric capacity
    # (``DAIKIN_MAX_HP_KW × 0.5``). A scaled-up transition slot
    # (0.45 × autoscale-max 1.6 = 0.72 kWh) must never exceed that bound or
    # every solve goes Infeasible on small-heater configs. The boost ramp
    # below clamps itself; the plain schedule values are clamped here.
    _max_hp_kwh = max(0.05, float(getattr(config, "DAIKIN_MAX_HP_KW", 2.0)) * 0.5)
    e_dhw = [min(v, _max_hp_kwh) for v in e_dhw]

    # ----- Initial-tank "warm credit" adjustment ---------------------------
    # If the tank arrives at the LP horizon ABOVE its scheduled target, the
    # heat pump doesn't have to lift it — that stored thermal energy is a
    # gift that offsets the first slots' warmup/reheat load until consumed
    # by standing losses + draws. Without this, the LP forecast would over-
    # estimate e_dhw on transition days (e.g. today after a hot-arrival).
    if initial_tank_c is not None and initial_tank_c > normal_c + 0.5:
        try:
            tank_litres = float(getattr(config, "DHW_TANK_LITRES", 200.0))
            water_cp = float(getattr(config, "DHW_WATER_CP", 4186.0))
            cop_typical = 3.0  # heat-pump average DHW COP; matches lp_optimizer cop_dhw
            excess_thermal_kwh = (
                (initial_tank_c - normal_c) * tank_litres * water_cp / 3.6e6
            )
            excess_electric_kwh = excess_thermal_kwh / cop_typical
            # Spend the credit on the first non-zero slots first (warmup
            # transition + shower reheat are highest-value to offset).
            for i in range(len(e_dhw)):
                if excess_electric_kwh <= 0:
                    break
                if e_dhw[i] <= 0:
                    continue
                reduction = min(e_dhw[i], excess_electric_kwh)
                e_dhw[i] -= reduction
                excess_electric_kwh -= reduction
            logger.debug(
                "dhw_policy: applied %.2f kWh warm-credit (init_tank=%.1f, "
                "normal=%.1f)",
                excess_thermal_kwh / cop_typical, initial_tank_c, normal_c,
            )
        except Exception as _exc:  # pragma: no cover - defensive
            logger.debug("dhw_policy: warm-credit calc failed: %s", _exc)

    # Tank temperature trajectory at slot boundaries. Slot boundary k is
    # the START of slot k (k=0..N-1); boundary N is the END of last slot.
    # Pre-load boundary 0 from the initial state would require it as input;
    # instead we encode the policy's TARGET, not the live state. This is
    # fine for the LP's audit purposes — the actual physical temperature
    # is what dhw_policy commanded via the schedule.
    tank_temps: list[float] = []
    for slot in slot_starts_utc:
        slot_local = slot.astimezone(tz)
        h = slot_local.hour
        if mode == "vacation":
            tank_temps.append(setback_c)  # firmware-owned; setback as proxy
        elif mode == "guests":
            tank_temps.append(normal_c)
        elif _early_setback_active(slot, slot_local):
            tank_temps.append(setback_c)
        elif _warmup_hour_for(slot_local) <= h < setback_hour:
            tank_temps.append(normal_c)
        else:
            tank_temps.append(setback_c)
    # Boundary N: same as last slot's target (assume flat at end)
    tank_temps.append(tank_temps[-1] if tank_temps else normal_c)

    # ----- Negative-price boost (1A) + pre-cool (1C) -----------------------
    # When paid to import, BUDGET the heat-up energy to drive the tank to MAX so
    # the pinned LP plans the extra import; in the short window before, don't
    # re-warm the tank (let it coast to setback) so it has maximum headroom to
    # absorb. Shower-safe + skipped in vacation (firmware owns the tank).
    if price_line is not None and len(price_line) == n and mode != "vacation":
        boost_target = min(
            float(getattr(config, "DHW_NEGATIVE_PRICE_BOOST_C", 65)),
            float(config.DHW_TEMP_MAX_C),
        )
        max_hp_kwh = max(0.05, float(getattr(config, "DAIKIN_MAX_HP_KW", 2.0)) * 0.5)
        tank_litres = float(getattr(config, "DHW_TANK_LITRES", 200.0))
        water_cp = float(getattr(config, "DHW_WATER_CP", 4186.0))
        cop_typical = 3.0  # matches lp_optimizer cop_dhw + warm-credit above
        elec_per_degree = (tank_litres * water_cp / 3.6e6) / cop_typical  # kWh elec / °C
        precool_slots = int(max(0.0, float(getattr(config, "LP_PRE_NEGATIVE_PRECOOL_HOURS", 3.0))) * 2)

        # Contiguous runs of negative-price slots.
        windows: list[tuple[int, int]] = []
        i = 0
        while i < n:
            if price_line[i] < 0:
                j = i
                while j + 1 < n and price_line[j + 1] < 0:
                    j += 1
                windows.append((i, j))
                i = j + 1
            else:
                i += 1

        for ws, we in windows:
            # 1C pre-cool: in the slots just before the window, don't re-warm —
            # coast to setback. Never touch a shower-reheat slot (comfort).
            for k in range(max(0, ws - precool_slots), ws):
                if phases[k] == "shower_reheat":
                    continue
                tank_temps[k] = min(tank_temps[k], setback_c)
                if e_dhw[k] > SETBACK_MAINTENANCE_KWH:
                    e_dhw[k] = SETBACK_MAINTENANCE_KWH
            # 1A boost ramp: heat from the (cooled) entry temp up to boost_target,
            # clamped to the heater's per-slot electric capacity. Short windows
            # only reach what's physically possible.
            entry = float(tank_temps[ws])
            if precool_slots > 0:
                entry = min(entry, setback_c)
                tank_temps[ws] = entry
            cur = entry
            for k in range(ws, we + 1):
                if cur >= boost_target - 1e-6:
                    e_dhw[k] = WARMUP_MAINTENANCE_KWH      # maintain at max
                    tank_temps[k + 1] = boost_target
                    cur = boost_target
                    continue
                lift_deg = (max_hp_kwh / elec_per_degree) if elec_per_degree > 0 else 0.0
                new_temp = min(boost_target, cur + lift_deg)
                e_dhw[k] = min(max_hp_kwh, (new_temp - cur) * elec_per_degree + SETBACK_MAINTENANCE_KWH)
                cur = new_temp
                tank_temps[k + 1] = cur

    # ----- Legionella heat-up budget (#643) ---------------------------------
    # The Onecta firmware runs the weekly thermal-shock cycle on its own
    # (the DHW_LEGIONELLA_STANDOFF_* window); HEM never commands it, but the
    # house DOES draw the energy. 2026-07-05 audit: the forecast budgeted
    # ~0.5 kWh for a cycle that drew ~3-3.5 (37→60 °C on 200 L + hold, at the
    # poor COP of a 60 °C lift), so the LP let the battery discharge into the
    # window and hit the SoC floor mid-cycle; actual import ran ~3 kWh over
    # plan. Budget the measured electric cost evenly across the window slots.
    # ENERGY only — tank_temps stay the schedule's comfort targets (the K2 pin
    # skips tank thermodynamics; the firmware owns the real trajectory).
    # Applied after bias/clamp/boost on purpose: a deterministic firmware
    # event is never scaled by the shape corrector (whose learner also
    # EXCLUDES this window), and where it overlaps a negative-price boost we
    # take max, not sum. Applies in ALL modes incl. vacation — the firmware
    # fires regardless of the preset.
    if bool(getattr(config, "DHW_LEGIONELLA_BUDGET_ENABLED", True)):
        _budget = float(getattr(config, "DHW_LEGIONELLA_BUDGET_KWH", 3.5))
        if _budget > 0 and bool(getattr(config, "DHW_LEGIONELLA_STANDOFF_ENABLED", True)):
            _dow = int(getattr(config, "DHW_LEGIONELLA_STANDOFF_DOW", 6))
            _h0 = int(getattr(config, "DHW_LEGIONELLA_STANDOFF_START_HOUR_UTC", 11))
            _m0 = int(getattr(config, "DHW_LEGIONELLA_STANDOFF_START_MINUTE_UTC", 0))
            _dur = int(getattr(config, "DHW_LEGIONELLA_STANDOFF_DURATION_MINUTES", 120))
            _cap = max(0.05, float(getattr(config, "DAIKIN_MAX_HP_KW", 2.0)) * 0.5)
            _idxs: list[int] = []
            for _i, _slot in enumerate(slot_starts_utc):
                _su = _slot.astimezone(UTC)
                if _su.weekday() != _dow:
                    continue
                _ws = _su.replace(hour=_h0, minute=_m0, second=0, microsecond=0)
                # Window is defined not to cross midnight (config contract),
                # so slot and window share the UTC date.
                if _ws <= _su < _ws + timedelta(minutes=_dur):
                    _idxs.append(_i)
            if _idxs:
                _per_slot = min(_cap, _budget / len(_idxs))
                for _i in _idxs:
                    e_dhw[_i] = min(_cap, max(e_dhw[_i], _per_slot))

    return e_dhw, tank_temps


def write_daily_tank_schedule(
    target_date_local: date | None = None,
    *,
    agile_rates: list[dict[str, Any]] | None = None,
    mode: str | None = None,
    clear_existing: bool = True,
    boosts_only_as_of: datetime | None = None,
    plan_date_override: str | None = None,
) -> int:
    """Write a day's tank schedule into ``action_schedule``.

    The horizon clearing is done by the LP-side ``write_daikin_from_lp_plan``
    when ``DHW_FIXED_SCHEDULE_ENABLED=True``. This function does NOT clear
    by default to allow concurrent LP-side writes (Fox V3 charge actions
    sit in the same horizon but on the ``foxess`` device, not daikin).

    Args:
        target_date_local: defaults to today in local TZ
        agile_rates: optional, for negative-price detection
        mode: optional override; defaults to ``config.OPTIMIZATION_PRESET``
        clear_existing: when True, calls ``db.clear_actions_in_range`` over
            the warmup window before upserting
        plan_date_override: stamp rows with this ``plan_date`` instead of
            ``target_date_local``. CRITICAL for the boosts-only live-cycle
            recovery: the heartbeat reconciler selects rows by
            ``get_actions_for_plan_date(today_local)``, so a live-cycle boost
            anchored at *yesterday* must be filed under TODAY's plan_date or it
            is never fired (2026-06-07: the recovered boost sat pending all
            window because it inherited yesterday's plan_date).

    Returns:
        Number of rows written.
    """
    if target_date_local is None:
        target_date_local = datetime.now(_tz_local()).date()

    rows = generate_daily_tank_schedule(
        target_date_local,
        agile_rates=agile_rates,
        mode=mode,
        boosts_only_as_of=boosts_only_as_of,
    )
    if not rows:
        logger.info("dhw_policy: no rows for %s (mode=%s)", target_date_local, mode)
        return 0

    if clear_existing:
        # Clear daikin actions in the full warmup→next-warmup horizon
        start_iso = rows[0]["start_time"]
        end_iso = rows[-1]["end_time"]
        # tank_negative_boost rows may have earlier start times — use min/max
        start_iso = min(r["start_time"] for r in rows)
        end_iso = max(r["end_time"] for r in rows)
        db.clear_actions_in_range(start_iso, end_iso, device="daikin")

    plan_date_str = plan_date_override or str(target_date_local)
    n_written = 0
    for r in rows:
        try:
            db.upsert_action(
                device=r["device"],
                action_type=r["action_type"],
                start_time=r["start_time"],
                end_time=r["end_time"],
                params=r["params"],
                plan_date=plan_date_str,
                status="pending",
            )
            n_written += 1
        except Exception as e:
            logger.warning(
                "dhw_policy: upsert failed for %s @ %s: %s",
                r["action_type"], r["start_time"], e,
            )
    logger.info(
        "dhw_policy: wrote %d rows for %s (mode=%s, neg_windows=%d)",
        n_written, target_date_local, mode,
        sum(1 for r in rows if r["action_type"] == "tank_negative_boost"),
    )
    return n_written
