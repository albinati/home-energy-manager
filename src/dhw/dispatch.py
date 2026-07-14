"""Turn the LP's tank-temperature trajectory into a few Daikin setpoint rows.

The LP plans a temperature per 30-min boundary. The Daikin can only be told a tank
SETPOINT, and it can only be told it a few times a day — the Onecta quota is 200
calls, and each row we write is a potential call. So this module compresses the
trajectory into a handful of ``action_schedule`` rows, and it does so PURELY (a
function of the plan, no DB, no clock) so the compression is testable in isolation.

Three things it must get right, each mapped to a mechanism:

* **Few rows.** The plan's per-slot targets are quantised to a small ladder of
  setpoints and then run-length-encoded: contiguous slots at the same rung become
  ONE row. A minimum dwell stops a one-slot blip from earning its own row. This is
  the same idea as the LP's slice cap, seen from the other end — the LP bounds the
  number of heating runs, and this bounds the number of setpoint changes.

* **Never below comfort, whatever the LP believed.** The row translation follows the
  LP, but the LP trusts a calibration that could be wrong. So a BACKSTOP row is laid
  over the shower window unconditionally, at the declared comfort temperature, from a
  constant — never from anything learned. If the tank really did coast to comfort the
  firmware sees a target it already meets and does nothing; if the model was wrong the
  firmware repairs it. This is what makes an optimistic-calibration bug a non-event
  instead of a cold shower.

* **One owner.** These rows carry an ``lp_owned`` marker and use the same
  ``action_type`` vocabulary (``tank_warmup`` / ``tank_setback`` / ``tank_negative_boost``)
  as the fixed schedule, so the heartbeat, the pre-fire reconciler and the user-override
  machinery treat them identically — and the dispatch layer writes them in ONE batch
  keyed to the same clear range, so the LP and the fixed schedule can never both own the
  tank in the same window.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

#: The setpoint ladder the Daikin is actually told. The tank sensor and setpoint are
#: whole degrees (Onecta stepValue = 1); a small ladder keeps re-plans from nudging the
#: setpoint by fractions of a degree and spending a write on nothing. The rungs are the
#: meaningful states: overnight setback, normal comfort, guest comfort, boost.
DEFAULT_RUNGS: tuple[int, ...] = (37, 45, 48, 60)


@dataclass(frozen=True)
class TankRow:
    """One Daikin setpoint instruction, ready for ``db.upsert_action``."""

    action_type: str
    start_utc: datetime
    end_utc: datetime
    tank_temp_c: int
    tank_powerful: bool

    def to_params(self) -> dict:
        return {
            "tank_power": True,
            "tank_temp": int(self.tank_temp_c),
            "tank_powerful": self.tank_powerful,
            "lp_owned": True,  # marker: this row came from the LP-owned regime
        }


def _quantise(target_c: float, rungs: tuple[int, ...], *, heating: bool) -> int:
    """Snap a planned temperature to the ladder.

    Direction matters. When the LP is HEATING towards a target, round UP to the next
    rung so the delivered water is at least as hot as planned — a shower must never be
    cooler than the plan promised. When the tank is COASTING, round DOWN: telling the
    Daikin a setpoint at or below the current temperature is what lets it sit idle and
    the tank drift, which is the whole point of letting it cool through the peak.
    """
    ladder = sorted(rungs)
    if heating:
        for r in ladder:
            if r >= target_c - 1e-6:
                return r
        return ladder[-1]
    for r in reversed(ladder):
        if r <= target_c + 1e-6:
            return r
    return ladder[0]


def tank_rows_from_plan(
    slot_starts_utc: list[datetime],
    tank_temp_c: list[float],
    dhw_electric_kwh: list[float],
    price_pence: list[float],
    *,
    rungs: tuple[int, ...] = DEFAULT_RUNGS,
    min_dwell_slots: int = 2,
    heating_kwh_threshold: float = 0.05,
    slot_minutes: int = 30,
) -> list[TankRow]:
    """Compress an LP tank plan into a few setpoint rows. PURE — plan in, rows out.

    ``tank_temp_c`` has one more entry than the slot list (boundaries). ``dhw_electric_kwh``
    tells heating slots (the LP is adding heat) from coasting ones, which sets the
    rounding direction. A negative price makes a boost row (``tank_powerful``).
    """
    n = len(slot_starts_utc)
    if n == 0:
        return []
    slot_dt = timedelta(minutes=slot_minutes)

    # Per-slot quantised target + whether it is a boost slot.
    per_slot: list[tuple[int, bool]] = []
    for i in range(n):
        heating = dhw_electric_kwh[i] > heating_kwh_threshold
        # The target the LP wants the tank to REACH by the end of the slot.
        target = tank_temp_c[i + 1]
        # A boost is a negative-price slot the LP actually HEATS in — that is the paid
        # import worth commanding to 60 °C. A negative-price slot the LP leaves idle is
        # just coasting that happens to be cheap; it is not a boost.
        boost = price_pence[i] < 0 and heating
        rung = _quantise(target, rungs, heating=heating)
        per_slot.append((rung, boost))

    # Run-length encode into contiguous same-(rung,boost) blocks.
    blocks: list[tuple[int, int, int, bool]] = []  # (start_i, end_i_excl, rung, boost)
    s = 0
    for i in range(1, n + 1):
        if i == n or per_slot[i] != per_slot[s]:
            blocks.append((s, i, per_slot[s][0], per_slot[s][1]))
            s = i

    # Absorb a too-short block into a neighbour, fail-cheap: prefer merging DOWN to the
    # cooler rung (never invent heat the plan did not ask for). A short block at the
    # horizon edge merges into its only neighbour.
    merged = _absorb_short_blocks(blocks, min_dwell_slots)

    rows: list[TankRow] = []
    prev_rung: int | None = None
    for start_i, end_i, rung, boost in merged:
        start = slot_starts_utc[start_i]
        end = slot_starts_utc[end_i - 1] + slot_dt
        # warmup = the setpoint went UP (or the first block already sits above the
        # setback floor); setback = it held or dropped. The distinction is what the
        # heartbeat/reconciler (#386) key on, so it must reflect the actual move.
        if boost:
            atype = "tank_negative_boost"
        elif (prev_rung is None and rung > rungs[0]) or (prev_rung is not None and rung > prev_rung):
            atype = "tank_warmup"
        else:
            atype = "tank_setback"
        rows.append(TankRow(
            action_type=atype, start_utc=start, end_utc=end,
            tank_temp_c=rung, tank_powerful=boost,
        ))
        prev_rung = rung
    return rows


def _absorb_short_blocks(
    blocks: list[tuple[int, int, int, bool]], min_dwell: int
) -> list[tuple[int, int, int, bool]]:
    """Merge any block shorter than ``min_dwell`` slots into an adjacent block, then
    coalesce neighbours that ended up on the same rung. Boost blocks are never
    absorbed (a negative-price boost is always worth its own row)."""
    if len(blocks) <= 1:
        return blocks
    work = list(blocks)
    changed = True
    while changed and len(work) > 1:
        changed = False
        for idx, (s, e, rung, boost) in enumerate(work):
            if boost or (e - s) >= min_dwell:
                continue
            # Merge into the cooler neighbour (fail-cheap), else the only neighbour.
            left = work[idx - 1] if idx > 0 else None
            right = work[idx + 1] if idx + 1 < len(work) else None
            pick = _cooler_non_boost(left, right)
            if pick is None:
                continue  # both neighbours are boosts; leave it
            tgt_rung = pick[2]
            new = (s, e, tgt_rung, False)
            work[idx] = new
            changed = True
            break
        # Coalesce adjacent same-(rung,boost) blocks.
        work = _coalesce(work)
    return work


def _cooler_non_boost(left, right):
    cands = [b for b in (left, right) if b is not None and not b[3]]
    if not cands:
        return None
    return min(cands, key=lambda b: b[2])


def _coalesce(blocks):
    out = [blocks[0]]
    for s, e, rung, boost in blocks[1:]:
        ps, pe, pr, pb = out[-1]
        if rung == pr and boost == pb:
            out[-1] = (ps, e, pr, pb)
        else:
            out.append((s, e, rung, boost))
    return out


def apply_comfort_backstop(
    rows: list[TankRow],
    slot_starts_utc: list[datetime],
    tz,
    *,
    backstop_c: float,
    window_start_hour: float,
    window_end_hour: float,
) -> list[TankRow]:
    """Guarantee a comfort-temperature setpoint over the shower window, whatever the
    LP planned. Reads NOTHING learned — ``backstop_c`` is the declared comfort constant.

    Any existing row over the window that already meets the backstop is kept; a gap or a
    too-cool row is raised to it. If the LP's own plan already delivered comfort this is
    a no-op the firmware never notices. If an optimistic calibration bug left the tank
    cold, this is what stops that becoming a cold shower — and a backstop that actually
    HAS to raise a cold row is the regime's health alarm (log it upstream).
    """
    if not slot_starts_utc:
        return rows
    slot_dt = slot_starts_utc[1] - slot_starts_utc[0] if len(slot_starts_utc) > 1 else timedelta(minutes=30)

    def _in_window(st: datetime) -> bool:
        local = st.astimezone(tz)
        h = local.hour + local.minute / 60.0
        return window_start_hour <= h < window_end_hour

    win_slots = [st for st in slot_starts_utc if _in_window(st)]
    if not win_slots:
        return rows
    win_start, win_end = win_slots[0], win_slots[-1] + slot_dt

    backstop = int(round(backstop_c))
    covered = any(
        r.start_utc <= win_start and r.end_utc >= win_end and r.tank_temp_c >= backstop
        for r in rows
    )
    if covered:
        return rows

    # Drop/trim any row inside the window and lay the backstop over it.
    kept = [r for r in rows if r.end_utc <= win_start or r.start_utc >= win_end]
    kept.append(TankRow(
        action_type="tank_warmup", start_utc=win_start, end_utc=win_end,
        tank_temp_c=backstop, tank_powerful=False,
    ))
    kept.sort(key=lambda r: r.start_utc)
    return kept
