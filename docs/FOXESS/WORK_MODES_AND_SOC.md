# Fox ESS — work modes, SoC keys, scheduler groups

Reference for the Fox Open API quirks we actually hit in this codebase.
Keep this living — update when a new shape shows up.

## TL;DR landmines

1. **Two different string conventions for `workMode`** depending on endpoint:
   - Global inverter setting (`/device/setting/set` with `key=workMode`): **spaced** strings → `"Self Use"`, `"Feed-in Priority"`, `"Back Up"`, `"Force charge"`, `"Force discharge"`.
   - Scheduler V3 group's `workMode` field (`/op/v3/device/scheduler/enable`): **camelCase, no spaces** → `"SelfUse"`, `"ForceCharge"`, `"ForceDischarge"`, `"Feedin"`, `"Backup"`.
   - Getting this wrong returns `errno 40257 "Parameters do not meet expectations"`.
2. **Two different SoC keys**:
   - `minSocOnGrid` — the on-grid battery reserve floor. Accepted in **scheduler group `extraParam`** (V3) AND we currently also use it as a global setting via `set_device_setting("minSocOnGrid", …)`. The latter is device-dependent — some models 40257 on this.
   - `minSoc` — the inverter's **true global** min-SoC setting on some firmwares. Docstring in `client.py:401` mentions it as an example. We do NOT currently call this directly; `set_min_soc` only writes `minSocOnGrid`.
3. **`get_device_list` is POST, not GET** — a GET returns 40257 (see comment at `client.py:271`).

## Work modes — full table

| Global setting (spaces) | Scheduler V3 (no space) | Behavior | When the LP picks this |
|---|---|---|---|
| `Self Use` | `SelfUse` | Default. Use PV first, charge battery from surplus, import from grid to cover load shortfall. Never force-exports. **Firmware caveat:** a group `minSocOnGrid=100` does NOT reliably freeze discharge — observed 2026-06-28 and 2026-07-04 (battery discharged into a heavy load with SoC 20-27 % under SelfUse(100,100)). Do not use a SelfUse floor as a hold primitive; use ForceCharge. | Standard slots + `solar_charge` (PV-only charging; since 2026-07-04 negative-price slots outrank solar_charge — `LP_NEGATIVE_BEATS_SOLAR_CHARGE`). |
| `Feed-in Priority` | `Feedin` | Send PV **directly to grid** at full capacity; battery only covers load shortfall. | Not currently used by the LP dispatcher (would need Outgoing Agile + surplus). |
| `Back Up` | `Backup` | Reserve the battery for outage/EPS: does **not** discharge to household loads, and charges from grid toward full (observed live 2026-07-04: manual Backup → grid import with min/max SoC pinned at 100 %). | **Default for `negative_hold` since 2026-07-04** (`LP_NEGATIVE_HOLD_FOX_MODE=backup`; `forcecharge` = the #607/#630 interim, kept as fallback). NB the 2026-06-28 "Backup discharges into load" finding was a **misdiagnosis** — fox_schedule_state archaeology (2026-07-04) showed the discharge windows were covered by SelfUse groups (zero-charge `negative` slots fell through to the SelfUse mapping); no Backup group was ever active during an observed discharge. |
| `Force charge` | `ForceCharge` | **Charge battery from the grid** at the specified `fdPwr` until `fdSoc` is reached. Respects `minSocOnGrid` as a lower bound but `fdSoc` is the target ceiling for this window. | Negative-price slots + cheap-price slots ahead of a forecasted peak. |
| `Force discharge` | `ForceDischarge` | **Discharge battery to grid** (peak-export) until `fdSoc` is reached or battery hits `minSocOnGrid`. | `peak_export` / `pre_negative_export` slot kinds (LP plans discharge AND export exceeds PV-alone), filtered for robustness by the scenario LP. (`ENERGY_STRATEGY_MODE` + `EXPORT_DISCHARGE_MIN_SOC_PERCENT` were removed — mode collapse #392-394.) |

### Empirical mode truth table (OUR H1, 35 days of prod telemetry, 2026-07-04)

15,232 3-min samples from `pv_realtime_history` joined against the V3 group
active at each instant (`fox_schedule_state`). Regenerate with
`fox_mode_truth_table.py` (scp to `/srv/hem/data/`, `docker exec hem python`).
This table — not vendor prose — is the authority for dispatch decisions:

| Regime (as uploaded) | n | % samples discharging >0.1 kW | % charging | Verdict |
|---|---|---|---|---|
| `Backup` (minSoc=10, maxSoc=10 or None) | 413 | **0.0 %** | 28-36 % (PV trickle; grid ~1.2 kW avg when maxSoc unset) | TRUE hold: never discharges to loads; tops up toward full when maxSoc allows. |
| `ForceCharge`, SoC ≥ fdSoc | 354 | **0.0 %** | ~5 % | TRUE hold after target — equivalent to Backup, plus target/power control. |
| `ForceCharge`, SoC < fdSoc | 749 | 0.6-1.2 % (slot-boundary noise) | 96-97 % @ ~2.5-3.9 kW | Charges from grid at fdPwr as documented. |
| `ForceDischarge` | 177 | 88 % @ 3.5 kW | — | As documented. |
| `SelfUse(minSocOnGrid=100, maxSoc=100)` (the `solar_charge` shape) | 4,011 | **31.5 % @ 0.69 kW avg** | 53 % | **The floor is NOT honoured as a discharge freeze.** Never use a SelfUse floor as a hold primitive (2026-06-28 + 07-04 incidents). |
| No group covering the instant (global work mode = Self Use) | 9,497 | 66 % | 8 % | Plain self-use outside scheduled windows — desired at positive prices. |

Consequences for the dispatcher:
- The two proven zero-discharge hold primitives are **Backup** and
  **ForceCharge with fdSoc ≤ current SoC**. Since 2026-07-04 (owner decision)
  the dispatcher holds negative windows via **Backup** — the semantically
  native reserve mode; with maxSoc unpinned the firmware also tops the
  battery up from the PAID grid inside the window (~1.2 kW avg observed),
  which is exactly the household policy (maximize grid usage during
  negatives). Backup holds also sit structurally outside the ForceCharge
  merge, so paid fills stay anchored to the deepest-priced slots.
  `LP_NEGATIVE_HOLD_FOX_MODE=forcecharge` restores the #607/#630 interim
  (FC at fdPwr ≈ LP import, fdSoc = target — equally discharge-proof).
  Backup is only used INSIDE negative windows: anywhere else its
  unconditional top-up would buy energy the plan didn't ask for.
- Negative windows must never contain SelfUse groups — enforced at the
  labeller since #630 (`LP_NEGATIVE_BEATS_SOLAR_CHARGE`).
- Known residual transition artifact: when a re-plan shifts the horizon past
  the in-flight slot, past windows are reconstructed from the PREVIOUS plan's
  labels (only in-flight ForceCharge is re-asserted). Self-heals one slot
  after any labelling change.

### Group `extraParam` fields (V3)

`SchedulerGroup.to_api_dict()` (`models.py:53-73`) packs these into `extraParam`:

- `minSocOnGrid` (always present, default `10`) — discharge floor during this window.
- `fdSoc` — **target SoC** for `ForceCharge` / `ForceDischarge`. For ForceCharge: "charge until battery reaches this %". For ForceDischarge: "discharge until battery reaches this % or minSocOnGrid, whichever is higher".
- `fdPwr` — **power in watts**. For ForceCharge: charge rate. For ForceDischarge: discharge rate. Bounded by `FOX_FORCE_CHARGE_MAX_PWR` (default 6000 = the inverter's AC cap on our 6 kW hybrid).
- `maxSoc` — rarely used, ceiling for SelfUse charging.
- `importLimit` / `exportLimit` — in watts. Peak-shaving knobs; not wired into the LP dispatcher today.

## Known issue: `set_min_soc(10)` failing with 40257 on shutdown

The `apply_safe_defaults` shutdown path (`state_machine.py:85`) calls:
1. `set_scheduler_flag(False)` — OK
2. `set_work_mode("Self Use")` — OK
3. `set_min_soc(10)` → `set_device_setting("minSocOnGrid", 10)` — **40257**

Observed on every prod restart. The inter-write pacing (PR #134) didn't fix it because it's not a timing issue — the key/value combo is rejected.

### Hypotheses (to verify)

1. **Wrong key name** — this device might require `minSoc` instead of `minSocOnGrid` for a global setting. `minSocOnGrid` may only be valid inside a scheduler group's `extraParam`, not as a top-level device setting.
2. **Value format** — Fox might expect a string, a nested object, or a different numeric range.
3. **Already at 10%** — device might reject a no-op write.

### Verification plan (next time we touch Fox)

Try, in isolation, against a dev account:
```python
# Hypothesis 1
fox.set_device_setting("minSoc", 10)
# Hypothesis 2
fox.set_device_setting("minSocOnGrid", {"value": 10})
# Hypothesis 3 — omit the write; just rely on the scheduler group's minSocOnGrid
```

The scheduler group already sets `minSocOnGrid` per window via `extraParam` — the shutdown-path call is probably redundant with that.

### Workaround (non-urgent)

The shutdown error is cosmetic: logged at WARN, doesn't affect the subsequent startup. Filing as `TODO(fox-minsoc-40257)`. If anyone touches `apply_safe_defaults` next, try removing the `set_min_soc(10)` call and see if anything notices.

## Realtime `workMode` parsing

`get_realtime()` may return `workMode` as:
- a **numeric code** (`"0"` / `"1"` / …) — mapped via `WORK_MODE_BY_CODE` at `client.py:32`.
- a **spaced string** (`"Self Use"`).
- an **unknown string** — left as-is.

Always treat `RealTimeData.work_mode` as a display string; don't match it exactly to the `set_work_mode` input list.

## References

- `src/foxess/client.py` — HTTP layer, all writers
- `src/foxess/models.py` — `SchedulerGroup`, `RealTimeData`
- TonyM1958/FoxESS-Cloud — reference Python client (inspired our 2s inter-write pacing, see PR #134)
- Fox Open API docs: https://www.foxesscloud.com/public/i18n/en/OpenApiDocument.html
