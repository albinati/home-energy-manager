## Summary

<!-- What changed and why. -->

## Testing

<!-- e.g. `pytest tests/...` or manual checks. -->

## Dispatch / LP changes

<!--
If this PR touches src/scheduler/ (LP solver, dispatch, scenarios, peak-export
logic), run the realised-data regression validator against a recent prod DB
snapshot and paste the verdict line:

    DB_PATH=/path/to/prod-snapshot.db .venv/bin/python scripts/validate_scenario_filter.py

  → AGGREGATE: +X.XX £   (threshold: -5.00 £)
  → VERDICT: PASS / FAIL

If VERDICT=FAIL, the filter would have lost money historically — investigate
and tune `LP_PEAK_EXPORT_PESSIMISTIC_FLOOR_KWH` or the perturbation deltas
before merging. See docs/DISPATCH_DECISIONS.md.

Skip this section for PRs that don't touch the dispatch path.
-->

## Issues

<!-- Link issues so GitHub can auto-close on merge. Use one of:
  - `Closes #123` — merge to default branch closes #123 (see docs/phase2-epic-tasks.md).
  - `Related to #123` — partial work; maintainers close manually after verification.
-->
