# Contributing

This is a personal project running 24/7 against one specific UK installation. Public contributions are welcome — but the PR bar is "does this make my installation strictly better, with evidence, without breaking the regression gate?"

By participating, you agree to abide by the [Code of Conduct](CODE_OF_CONDUCT.md).

## Before opening a PR

1. **Open an issue first** for anything beyond a typo or one-line fix. Saves both of us time if the change isn't a fit.
2. **Check the open epics** — most ongoing work has a designated story already. Open the [Issues tab](https://github.com/albinati/home-energy-manager/issues) and filter by the `epic` label.
3. **Run the test suite locally** — `pytest` (~3 min, **1100+ tests** as of v12). All tests must pass. If you find a flake (e.g. time-of-day boundary cases), file an issue rather than disabling.
4. **Run the regression gate** if you touch `src/scheduler/`:
   ```bash
   # Per-PR delta — preferred. Compares this branch against main on the same prod snapshots.
   DB_PATH=/path/to/prod-snapshot.db PYTHONPATH=. .venv/bin/python \
       scripts/check_lp_regression.py --vs-ref=main --days 14 --mode=both

   # Frozen baseline check — fallback when vs-ref subprocess can't propagate PYTHONPATH.
   DB_PATH=/path/to/prod-snapshot.db PYTHONPATH=. .venv/bin/python \
       scripts/check_lp_regression.py --days 14 --mode=both
   ```
   Both `forward` and `honest` modes must pass. If you intentionally regressed the planner (new objective term, tuned weights), refresh the baseline JSON in the same PR with `--refresh-baseline` and explain the trade-off in the PR body.

## Local dev setup

```bash
git clone https://github.com/albinati/home-energy-manager.git
cd home-energy-manager
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
cp .env.example .env
# Set OPENCLAW_READ_ONLY=true — this is the kill switch for any hardware writes.
pytest
```

## Code conventions

- **Python 3.12+**, type-hinted where it adds clarity (don't dogmatically annotate every variable).
- **Ruff** for linting + formatting: `ruff check src tests` and `ruff format src tests`. CI enforces this.
- **No new dependencies** without an issue discussing why. The dependency tree is intentionally small.
- **No emojis in code or commits** (READMEs and PR bodies are fine).
- **Comments are for the *why*, not the *what*.** Well-named identifiers describe what; comments justify hidden constraints, surprising invariants, or workarounds for specific bugs.

## Commit style

Follow the existing log: `<type>(<scope>): <subject>`, e.g. `fix(lp): residual-load profile no longer pollutes itself in passive mode`. Types in active use: `feat`, `fix`, `chore`, `docs`, `refactor`, `test`. Reference the issue number in the body or trailing line so the squash-merge auto-closes it.

## Hardware-touching changes

This codebase controls real hardware: a heat pump that heats a house and a battery worth real money. Be paranoid about:

- **Quota**: Daikin Onecta has a hard **200 requests/day** limit, resets ~midnight UTC. Test changes against `OPENCLAW_READ_ONLY=true` first. The heartbeat no longer hits the Daikin API as of v12 (~10 % of quota recovered), so any new call you add is a measurable budget item.
- **Idempotency**: writes are issued repeatedly by the heartbeat. Anything that depends on "this is the first call this minute" is a bug waiting to happen.
- **Comfort slack**: tank target floors and shower-window floors are LP **soft constraints** since v12 — they breach with a heavy slack penalty rather than failing the solve. Don't tighten them without evidence (a PnL improvement that's not just from making the household colder).
- **Infeasibility surfaces are now snapshotted** (`lp_inputs_snapshot.lp_status='Infeasible'` + `replay_run` extension since v12). If your change introduces a new hard constraint, run a 14-day replay locally and verify the Infeasible count doesn't grow.

## LP design conventions

- **Hard constraints are reserved for physical limits** (fuse_kwh, max_inv_kw, soc bounds at slot 1+, energy balance). Anything else — comfort floors, ceilings, terminal states — should be soft with a configurable penalty.
- **No live API calls inside `solve_lp`**. The solver is pure math; all I/O happens before it (`appliance_dispatch.reconcile`, weather fetch) or after it (Fox upload, Daikin write).
- **Snapshot what the LP saw, not what you think it saw**. The `exogenous_snapshot_json` field is the ground truth for replay-based debugging.

## Reviewing

PRs run `pytest`, `lint-imports`, and (for any LP-touching code) the `--mode=both` regression gate. CI runs are required before merge. Ask for a re-review after pushing fixes — don't squash-and-force-push (it loses review history); push fix-up commits and let the squash-merge collapse them on land.

## Issues

Bug reports → [`Bug report` template](.github/ISSUE_TEMPLATE/bug_report.yml). Include the prod or sim DB row counts, the relevant `optimizer_log` `run_id`, and ideally a `replay_run` output if the LP made a surprising decision.

Feature ideas → [`Feature request` template](.github/ISSUE_TEMPLATE/feature_request.yml). Lead with the goal (`I want X because Y`), not the implementation.
