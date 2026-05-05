## Summary

<!-- What changed and why. -->

## Testing

<!-- e.g. `pytest tests/...` or manual checks. -->

## Dispatch / LP changes

<!--
If this PR touches src/scheduler/ (LP solver, dispatch, scenarios, peak-export
logic), BOTH of these gates must pass before merging.

### Gate 1 — LP regression (general) — BOTH modes must pass

V11-A (#194). The LP must be no worse than the pinned baseline on the same
historical replay dates in BOTH modes. Individual moments may be worse, but
aggregate cost must be better than or equal to the comparable baseline window:

  - **forward** (snapshot weather + current config) — catches behaviour change.
  - **honest**  (snapshot weather + snapshot config) — catches solver / framework
    regressions, including PV-side regressions like cloud-aware calibration drift.

    DB_PATH=/path/to/prod-snapshot.db .venv/bin/python scripts/check_lp_regression.py --mode=both

  → MODE = FORWARD: VERDICT PASS / FAIL
  → MODE = HONEST:  VERDICT PASS / FAIL
  → BOTH-MODE VERDICT: PASS / FAIL

If VERDICT=FAIL on either, aggregate cost got worse or the baseline dates could
not all be replayed — investigate before merging. Only refresh the baseline
after confirming the new strategy is better or equal on an agreed replay set,
then commit both `tests/fixtures/lp_regression_baseline.forward.json` and
`tests/fixtures/lp_regression_baseline.honest.json` in the same PR.

### Gate 2 — Scenario filter regression (peak_export specifically)

    DB_PATH=/path/to/prod-snapshot.db .venv/bin/python scripts/validate_scenario_filter.py

  → AGGREGATE: +X.XX £   (threshold: -5.00 £)
  → VERDICT: PASS / FAIL

If VERDICT=FAIL, the filter would have lost money historically — investigate
and tune `LP_PEAK_EXPORT_PESSIMISTIC_FLOOR_KWH` or the perturbation deltas
before merging. See docs/DISPATCH_DECISIONS.md.

Skip both for PRs that don't touch the dispatch path.
-->

## Issues

<!-- Link issues so GitHub can auto-close on merge. Use one of:
  - `Closes #123` — merge to default branch closes #123 (see docs/phase2-epic-tasks.md).
  - `Related to #123` — partial work; maintainers close manually after verification.
-->
