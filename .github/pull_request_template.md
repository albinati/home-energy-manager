## Summary

<!-- What changed and why. -->

## Testing

<!-- e.g. `pytest tests/...` or manual checks. -->

## Dispatch / LP changes

<!--
If this PR touches src/scheduler/ (LP solver, dispatch, scenarios, peak-export
logic), BOTH of these gates must pass before merging.

### Gate 1 — LP regression (general)

The LP must be no worse than the pinned baseline on the last 14 days of
historical runs. Run against your locally-mounted prod DB snapshot:

    DB_PATH=/path/to/prod-snapshot.db .venv/bin/python scripts/check_lp_regression.py

  → NEW total replayed cost: +X.XX £
  → BASELINE total replayed cost: +Y.YY £   (threshold +0.50 £)
  → Δ vs baseline: +Z.ZZ £
  → VERDICT: PASS / FAIL

If VERDICT=FAIL, the LP got worse on past days — investigate before merging.
If the regression is INTENTIONAL (new objective term, tuned weights), accept
it as the new baseline by re-running with `--refresh-baseline` and committing
`tests/fixtures/lp_regression_baseline.json` in the same PR.

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
