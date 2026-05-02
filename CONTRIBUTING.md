# Contributing

This is a personal project running 24/7 against one specific UK installation. Public contributions are welcome — but the PR bar is "does this make my installation strictly better, with evidence, without breaking the regression gate?"

## Before opening a PR

1. **Open an issue first** for anything beyond a typo or one-line fix. Saves both of us time if the change isn't a fit.
2. **Check the [V11 roadmap epic (#193)](https://github.com/albinati/home-energy-manager/issues/193)** — most accuracy work has a designated story already.
3. **Run the test suite locally** — `pytest` (~3 min, 837+ tests). All tests must pass.
4. **Run the regression gate** if you touch `src/scheduler/`:
   ```bash
   DB_PATH=/path/to/prod-snapshot.db .venv/bin/python scripts/check_lp_regression.py --mode=both
   ```
   Both `forward` and `honest` modes must pass. If you intentionally regressed the planner (new objective term, tuned weights), refresh the baseline JSON in the same PR.

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

- **Quota**: Daikin Onecta has a hard 200/day limit. Test changes against `OPENCLAW_READ_ONLY=true` first.
- **Idempotency**: writes are issued repeatedly by the heartbeat. Anything that depends on "this is the first call this minute" is a bug waiting to happen.
- **Comfort slack**: tank target floors and indoor temperature setpoints are LP soft constraints, not hard ones. Don't tighten them without evidence (a PnL improvement that's not from making the household colder).

## Reviewing

PRs run `pytest`, `lint-imports`, and (for any LP-touching code) the `--mode=both` regression gate. CI runs are required before merge. Ask for a re-review after pushing fixes — don't squash-and-force-push (it loses review history); push fix-up commits and let the squash-merge collapse them on land.

## Issues

Bug reports → [`Bug report` template](.github/ISSUE_TEMPLATE/bug_report.yml). Include the prod or sim DB row counts, the relevant `optimizer_log` `run_id`, and ideally a `replay_run` output if the LP made a surprising decision.

Feature ideas → [`Feature request` template](.github/ISSUE_TEMPLATE/feature_request.yml). Lead with the goal (`I want X because Y`), not the implementation.
