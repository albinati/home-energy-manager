# LP regression workflow

How we keep the LP solver from quietly getting worse, version-over-version,
in a way that's **fair, observable, and traceable**.

## TL;DR

For any PR that touches LP solver code, the LP closure, calibration,
dispatch translation, or anything else that can change the LP's planned
dispatch:

```bash
DB_PATH=/path/to/prod.db .venv/bin/python scripts/check_lp_regression.py \
    --vs-ref=main \
    --mode=forward \
    --json=/tmp/lp-regression-vs-main.json
```

Paste the resulting per-day table (and the verdict) into the PR
description. The PR is mergeable iff the verdict says PASS, OR you've
explained in writing why the regression is acceptable.

## Why this workflow

The original gate compared against a frozen JSON pinned in
`tests/fixtures/lp_regression_baseline.json`. It worked at the time, but:

1. **Drift bug** — Between baseline refreshes, every LP-touching PR
   accumulated against a stale reference. By the time #331 ran, the
   gate had been silently failing for ~4 PRs (#321, #324, #325, #329)
   without anyone noticing because nobody had actually run the gate
   per-PR. The verdict was a false signal: pre-existing drift, not the
   current PR.
2. **Origin opacity** — Per-day deltas were vs the frozen JSON, not vs
   the immediate parent. You couldn't read the output and tell which
   commit caused which day's regression.
3. **Outlier sensitivity** — A single noisy historical day in the
   baseline window stayed pinned forever. Refreshing the baseline
   accidentally absorbed whatever non-PR-related changes had landed in
   the meantime, papering over real regressions.

**The fix (per user 2026-05-15):** the calibration must come from the
*distribution* of forecast-vs-actual deviations, not from any single
"frozen baseline" snapshot. Same principle applies here: the regression
gate should measure THIS PR vs IMMEDIATE PARENT (or any chosen ref) on
the SAME prod DB, not against a frozen JSON.

## The new `--vs-ref` mode

`--vs-ref=<git-ref>` runs the replay twice:

1. **Inside a temporary `git worktree`** at the named ref (default
   target: `main`), via a subprocess that imports from that ref's
   `src/`. Both refs see the same `DB_PATH` (env-inherited) so the
   prod DB is identical.
2. **On the current branch**, in-process.

Then it produces a per-day comparison table:

```
  date         recalcs  ref £     current £   Δ £    class
  -------------------------------------------------------
  2026-05-10       35    +1.69      +1.94    +0.25  loss
  2026-05-11       31    +1.88      +1.88    +0.00  flat
  2026-05-13       41    +1.31      +1.85    +0.54  loss
  ...
  TOTAL                 +16.05     +16.84    +0.79
  Pattern: wins=0 losses=2 flat=8
  VERDICT: PASS — current branch ≤ main + threshold
```

**Reading the output:**

- `ref £` — total replayed cost on the named ref for that day
- `current £` — total replayed cost on current branch for that day
- `Δ £` — current minus ref. Negative = improvement. Positive = regression
- `class` — `win` (Δ < -1 p), `loss` (Δ > +1 p), `flat` (within 1 p)
- `Pattern:` — count by class for at-a-glance shape
- `VERDICT:` — PASS iff aggregate Δ ≤ `--fail-above-pence` (default 0)

Each row tells you which day this PR moved the needle. Cross-reference
against the run_id range and the `dispatch_decisions` table to trace
exactly what the LP decided differently on that day.

## Workflow conventions

### Per PR

Required for any PR matching the LP-touching criteria below:

```bash
# 1. Pull a recent prod DB snapshot
scp root@openclaw-overbot.tail0dbf20.ts.net:/srv/hem/data/energy_state.db /tmp/prod.db

# 2. Run the gate on your branch vs main
DB_PATH=/tmp/prod.db .venv/bin/python scripts/check_lp_regression.py \
    --vs-ref=main --json=/tmp/lp-reg.json

# 3. Paste output table + JSON path into PR description
```

PR-touching criteria (when to run):

- Any change under `src/scheduler/lp_*` or `src/scheduler/optimizer.py`
- Any change to `src/weather.py` PV calibration / forecast scaling
- Any change to `src/physics.py` Daikin curves
- Any change to the dispatch translation in `src/scheduler/lp_dispatch.py`
- Any change to `pv_calibration_hourly*` cron timing or window
- Any change to config defaults that the LP reads (`LP_*` env vars,
  `OPTIMIZATION_PRESET`, `ENERGY_STRATEGY_MODE`)

### When the verdict is FAIL

Don't refresh the baseline JSON to make it pass. The baseline JSON is
for documented strategy shifts only (changes accepted with full
awareness of the cost). For a FAIL on `--vs-ref=main`:

1. Look at the per-day rows sorted by `|Δ|` — which days regressed?
2. Use `--inspect-day=YYYY-MM-DD` on the worst regression day to dump
   the LP solution: which slots changed import/export/charge/discharge.
3. Cross-reference `dispatch_decisions` and `lp_inputs_snapshot` for
   that day's run_ids to see what the LP saw vs decided differently.
4. Either:
   - **Fix the regression** — modify the PR so the gate passes
   - **Document and accept** — explain in the PR description WHY the
     regression is acceptable (e.g. you accept a small cost increase
     for a comfort/safety/observability gain), and merge anyway. The
     follow-up PR after merge can update the frozen baseline if needed.

### When to use the frozen baseline JSON

Two cases:

1. **CI gate** — the GitHub Actions check runs without `--vs-ref` for
   stability (so it has a deterministic comparison even if `main` is in
   motion). The frozen JSON gives that stability.
2. **Strategy shift acceptance** — after a deliberate strategy change
   (e.g. switching from `savings_first` to `strict_savings` as the
   default), run `--refresh-baseline` to capture the new known-good
   state. Commit the updated JSON in a clearly-titled `chore(lp):
   refresh regression baseline` PR (precedent: #332).

## Traceability — patterns over time

The `--json` output is the durable record. Recommended convention:

- Per-PR run: archive `/tmp/lp-reg-PR<num>.json` (NOT in git — too churny)
- Per-merged-PR: in the PR description, paste the aggregate Δ + 1-2
  "loss" rows that explain the largest contributions
- Commit message convention for LP-touching PRs: include a one-line
  trailer
  ```
  LP-regression: vs main +£0.79 over 14 days (2/10 days regress, max +£0.54 on 2026-05-13)
  ```
  so `git log --grep=LP-regression` reconstructs the history of how
  cost-of-LP-changes accumulated over time.

## Comparing modes

| Mode | When | What it measures |
|---|---|---|
| `--mode=forward` | Default | New code on snapshot weather. Catches behaviour-change regressions. |
| `--mode=honest` | After solver-internal refactors | Snapshot weather + snapshot config. Catches solver/framework regressions independent of config drift. |
| `--mode=both` | Belt-and-braces | Run both, require both to pass. Use for any LP-touching PR before merge. |

All three modes can stack with `--vs-ref`.

## CI integration (current state)

CI runs the legacy frozen-baseline path automatically on every PR via
the `pytest` job. **It does NOT run `--vs-ref=main`** — that requires
a prod DB snapshot which CI doesn't have. So:

- CI's pass/fail is informational, NOT authoritative
- The `--vs-ref=main` run on your local machine + the pasted result in
  the PR description IS the authoritative check
- After merge, the next PR's `--vs-ref=main` will include your PR's
  impact in its "ref" side — chained traceability

Future work: a GHA cron that runs `--vs-ref=HEAD^` daily against a
DB snapshot stored in S3 (or similar), publishing the JSON to a
prometheus/grafana metric so the regression pattern is queryable across
months.

## Refs

- The frozen-baseline drift bug that prompted this: PR #331 discussion
  / #332 baseline-refresh PR
- Patch 2 (this workflow): PR #334
- Calibration imputation fix (related principle: trust the
  distribution, not yesterday): PR #333
