# Appliance dispatch (SmartThings)

Moved verbatim from CLAUDE.md on 2026-10-31; the CLAUDE.md section is the summary.

### Appliance dispatch

LP solver hooks `appliance_dispatch.reconcile()` at the start of each
solve. Reads each registered appliance's `remoteControlEnabled` via
SmartThings; when true, picks the cheapest contiguous window before the
deadline, includes the planned kWh in the LP residual-load profile, and
registers a one-shot APScheduler `DateTrigger` cron at the chosen time.
Cron fires `setMachineState run` after pre-fire safety re-check.
Cancelling on the unit before fire time → next LP solve drops the cron
and re-plans without the load. Physical Smart Control button on the
appliance IS the consent gate — no MCP confirm.

`device_type` accepts `washer | dryer | dishwasher` — same SmartThings
`setMachineState run` command for all three. Register multiple devices
via the discover/register MCP tools or REST endpoints.

**Earliest start within a cost tolerance (#853).** `find_battery_aware_window`
(and the marginal-cost cheapest picker) choose the EARLIEST candidate whose
TOTAL cycle cost is within `APPLIANCE_EARLY_START_TOLERANCE_PENCE` (default 10;
0 = legacy strict-cheapest) of the cheapest, instead of delaying a wash 12 h to
save 2 p. `APPLIANCE_MAX_DELAY_HOURS` (default 0 = off) never waits past now+N h
when a window inside it is within 3x tolerance. Battery-aware effective pricing
(refill / round-trip) is unchanged. `action_log` `appliance_window_choice`
(`chosen_start/total_p`, `cheapest_start/total_p`, `tolerance_p`) and the arm
notification ("lavo às 09:30 — esperar até 22:00 economizaria 2p"). Env-only knobs.

**Units of the picker's "price" (#853 review).** `build_marginal_cost_per_slot`
returns PENCE PER 30-MIN SLOT of the appliance's own energy (`typical_kw x 0.5`
kWh x p/kWh), NOT p/kWh: a 0.327 kW cycle (0.164 kWh/slot) shows 2.04 at the 12.49p
cheap band, 4.15 at 25.45p, 6.25 at 38.17p (PV-covered slots: forgone export).
`avg_price_pence` / "p/kWh" in logs and the arm ping inherit that scale (a mislabel,
not a ranking bug: every candidate shares it). Cycle total = value x slots.

**Learned `typical_kw` (#222).** The cycle-energy estimate prefers the rolling
mean of recent completed runs' measured `actual_kwh` (SmartThings energy
counter, #235) over the static registration `typical_kw`, once
`APPLIANCE_LEARNED_KW_MIN_SAMPLES` (3) runs exist — `db.appliance_learned_typical_kw`
over the last `APPLIANCE_LEARNED_KW_LOOKBACK` (10). Registration default 0.5 kW
over-estimated eco cycles ~3× (real ~0.2–0.4 kW), so the LP used to route around
the wash more than necessary. `GET /api/v1/appliances` surfaces
`learned_typical_kw` / `learned_samples` / `effective_typical_kw`.

---

