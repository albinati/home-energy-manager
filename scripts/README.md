# Scripts

## Fox ESS API tests

Test the Fox ESS Open API **before** changing code. Use either Python or curl.

## 1. Curl test (recommended first)

The script loads your project `.env` automatically. From project root:

```bash
./scripts/test_foxess_curl.sh
```

It reads `FOXESS_API_KEY` (or `FOX_API_KEY`) and `FOXESS_DEVICE_SN` or `INVERTER_SERIAL_NUMBER` from the `.env` in the project root.

- **errno 0**: Success; `result` shows the raw payload (list of devices with `datas`).
- **errno 40256** ("illegal signature"):
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

## Weekly energy report (CLI)

Plain-text report for the last 7 days (uses the same logic as the API insights):

```bash
PYTHONPATH=. .venv/bin/python scripts/weekly_report.py
PYTHONPATH=. .venv/bin/python scripts/weekly_report.py --date 2026-03-07
```

Requires Fox ESS configured in `.env` (same as the app).
