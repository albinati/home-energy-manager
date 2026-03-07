# AGENTS.md — Home Energy Manager

Production system controlling real hardware. Read before making changes.

## Hardware
- **Fox ESS inverter**: S/N 609H5020541M055 | Battery: EP11 ~10kWh | Logger: 609WWE1F541A727
- **Solar**: 4.5kWp (near-zero export — battery absorbs it all)
- **ASHP**: Daikin Altherma (Onecta cloud API) | ClientID: Ye0E9y4DyWMTk8_LebF8kiz2
- **Location**: London W4, UK

## Critical Rules
- `OPENCLAW_READ_ONLY=true` by default — recommendations only, no writes
- Fox ESS API: **200 req/day max** — no polling loops
- Daikin: check `weather_regulation_active` before changing LWT; use `lwt_offset`, not room temp
- Never remove safety guards or auth checks
- No credentials in code — use env vars from `src/config.py`

## Stack
- Python 3.11+, FastAPI, APScheduler
- AI assistant: Anthropic Claude Haiku (default), OpenAI as fallback
- REST API on port 8000 (default)

## Code Style
- Type hints required
- Follow existing patterns in `src/`
- Conventional commits: `feat/fix/refactor/docs/chore`
- Tests in `tests/` for new features
- Never force push `main`

## Who Runs This
OverBot (OpenClaw AI assistant) triggers Cursor Agent for coding tasks.
Git commits are handled by OverBot after Cursor edits — don't commit from here.
