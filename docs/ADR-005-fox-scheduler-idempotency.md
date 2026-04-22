# ADR-005 — Fox ESS Scheduler V3 idempotency guard

**Date:** 2026-04-22 (PR #61, closes #38).
**Status:** Implemented.
**Extends:** [ADR-001 — API Quota & Cache Layer](ADR-001-api-quota-cache-layer.md).

## Context

The Fox Open API v3 `POST /device/scheduler/enable` call uploads a full day
schedule (one API call per optimizer run). Empirically:

- Re-uploading an **identical** groups payload leaves the previous groups
  **disabled on the inverter** rather than cleanly replacing them. Over days
  this accumulated disabled rows and polluted the device state.
- Our MPC re-solves fire 3+ times a day. When the plan is unchanged (common in
  stable weather + stable price days), we were burning 3 Fox writes a day for
  no behavioural effect, and silently corrupting the device's scheduler state.

The existing `warn_if_scheduler_v3_mismatch` (#23) read back group counts after
a write but didn't gate the write itself — it only flagged the corruption after
the fact.

## Decision

**Read-before-write with fingerprint equality, fail-open on low budget.**

### Fingerprint

`SchedulerGroup.fingerprint()` returns a stable hashable tuple built from
`to_api_dict()` — i.e. exactly the fields we write. `extraParam` dict items are
sorted into a tuple so dict-order can't flip equality. We deliberately did **not**
override `__eq__` on the dataclass — keeps it lean and avoids accidental
hashability changes elsewhere in the codebase.

### Guard

`FoxESSClient.set_scheduler_v3(groups, is_default=False, *, skip_if_equal=True)`:

1. If `skip_if_equal` and `quota_remaining("fox") >= 2`: `get_scheduler_v3()`,
   compare fingerprint lists, short-circuit on match with an INFO log
   (`"Fox scheduler unchanged (N groups) — skipping upload"`).
2. If the pre-read GET raises, fall through to the POST anyway — we never want a
   flaky network read to block the write.
3. If budget has <2 calls left: skip the pre-read **and** upload unconditionally.
   Safety invariant _"push the latest plan"_ beats saving one redundant call
   when the quota is already tight.

Callers (`src/scheduler/lp_dispatch.py:336`, `src/scheduler/optimizer.py:760`)
get the new behaviour via default kwargs — zero migration effort.

## Consequences

### Good
- ~2 Fox writes/day saved in steady-state (MPC cadence × stable days). Marginal
  on the 1440/day budget, meaningful for device-state hygiene.
- Inverter no longer accumulates disabled groups — what we upload is what the
  device runs.
- The skip path is logged loudly enough to see in `journalctl -u home-energy-manager -n 200`
  so an operator can confirm the guard is engaging.

### Watch points
- **Fingerprint equality ≠ semantic equality.** Two groups with
  `fdPwr=3000` vs `fdPwr=2950` rounded differently would count as different even
  if the inverter treats them identically. We consider this acceptable — the LP
  is deterministic, so identical inputs produce identical fingerprints, and the
  only downside of a false-negative (unnecessary write) is one API call.
- **Pre-read GET costs one Fox call.** The `>= 2` budget gate is what prevents
  turning a cheap no-op into two calls when budget is tight. If you shrink the
  daily Fox budget very low, revisit this threshold.
- **Not currently applied elsewhere.** Force-discharge / peak-export-on-demand
  endpoints don't use this pattern; they're one-shot actions driven by the
  optimizer and always intentional.

## Related files

- `src/foxess/models.py::SchedulerGroup.fingerprint`.
- `src/foxess/client.py::set_scheduler_v3` (guarded), `get_scheduler_v3`,
  `warn_if_scheduler_v3_mismatch` (complementary, post-write).
- `src/api_quota.py::quota_remaining` — the headroom check.
- `tests/test_foxess_scheduler_readback.py` — skip-when-equal, upload-when-different,
  low-budget fail-open, pre-read GET failure, fingerprint stability / inequality.
