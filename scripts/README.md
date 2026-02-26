# Scripts

## GitHub issues agent

Background agent that checks this project’s GitHub issues and can start working on the most important one **with your explicit consent**. Uses the GitHub CLI (`gh`) and `jq`.

### Setup

1. Install [GitHub CLI](https://cli.github.com/) and [jq](https://jqlang.github.io/jq/).
2. Log in once: `gh auth login` (uses your GitHub account; no token in `.env` needed).

### Usage (from project root)

```bash
# List open issues ranked by importance (run manually or from cron)
./scripts/github_issues_agent.sh

# Interactive: show top issue, ask for consent, then create branch (gh issue develop) and open in browser
./scripts/github_issues_agent.sh --work

# Daemon: run in background, refresh state every N minutes (default 60)
./scripts/github_issues_agent.sh --daemon --interval 60
```

Importance is derived from labels (`priority: critical`, `priority: high`, `bug`, `enhancement`, etc.) and comment count. The agent never starts working without you answering `y` to “Start working on this issue?”. **Alternative:** If you prefer not to use `gh`/`jq`, the Python script `scripts/github_issues_agent.py` uses the GitHub REST API and only needs `GITHUB_TOKEN` in `.env` (see script docstring).

---

## Fox ESS API tests

Test the Fox ESS Open API **before** changing code. Use either Python or curl.

## 1. Curl test (recommended first)

The script loads your project `.env` automatically. From project root:

```bash
./scripts/test_foxess_curl.sh
```

It reads `FOXESS_API_KEY` (or `FOX_API_KEY`) and `FOXESS_DEVICE_SN` or `INVERTER_SERIAL_NUMBER` from the `.env` in the project root.

- **errno 0**: Success; `result` shows the raw payload (list of devices with `datas`).
- **errno 40256** (“illegal signature”):
  - Ensure `.env` has no trailing spaces or CRLF line endings on the Fox lines; the script trims values, but re-save the file with LF only if unsure.
  - Confirm the API key is valid: Fox ESS Cloud → User Profile → API Management. If you regenerated the key, update `FOXESS_API_KEY` in `.env`.
  - Use the **inverter** serial (not the datalogger) for `FOXESS_DEVICE_SN` or `INVERTER_SERIAL_NUMBER`.

## 2. Python test (raw response + parsed data)

Uses the same client as the app; needs a venv and project root on `PYTHONPATH`:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
PYTHONPATH=. .venv/bin/python scripts/test_foxess_api.py
```

This prints the raw API `result` and the parsed `RealTimeData` (soc, solar_power, etc.). If the API is unreachable (e.g. SSL timeout), run the curl test instead.
