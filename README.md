# home-energy-manager

Unified controller for Fox ESS battery + Daikin Altherma heat pump (Onecta).

Built for: 12 Whellock Rd — Daikin Altherma ASHP + Fox ESS battery + solar panels (SEG via British Gas).

## What it does

- **Fox ESS**: Read real-time battery SoC, solar production, grid import/export, inverter stats. Control charge/discharge mode and time-of-use schedules.
- **Daikin Onecta**: Read and set heat pump temperature, mode (heating/cooling/auto), on/off state, weather compensation.
- **Smart scheduling**: Time-of-use optimisation — charge battery on cheap-rate periods, pre-heat/cool with solar surplus.
- **OverBot integration**: Notifies via WhatsApp when action is needed (e.g. grid export curtailed, battery full, temperature drifting).

## Setup

### 1. Fox ESS API Key

1. Log in to [foxesscloud.com](https://www.foxesscloud.com)
2. Go to **My Account → API Management**
3. Generate an API key
4. Copy your **Device SN** (Settings → Device)

```bash
cp .env.example .env
# Edit .env and fill in FOXESS_API_KEY and FOXESS_DEVICE_SN
```

### 2. Daikin Onecta OAuth2

1. Register at [developer.cloud.daikineurope.com](https://developer.cloud.daikineurope.com) — use the **same email** as your Onecta app login
2. Create an application — set redirect URI to `http://localhost:8080/callback`
3. Copy Client ID and Client Secret into `.env`
4. Run the auth flow once:

```bash
python -m src.daikin.auth
# Opens browser → log in with Daikin account → tokens saved to .daikin-tokens.json
```

### 3. Install & run

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# Status dashboard
python -m src.cli status

# Set heat pump to 21°C
python -m src.cli daikin set-temp 21

# Set Fox ESS to force-charge (e.g. overnight cheap rate)
python -m src.cli foxess charge --from 00:30 --to 05:00 --target-soc 90

# Run continuous monitor (logs + WhatsApp alerts)
python -m src.cli monitor
```

## Project structure

```
src/
  foxess/
    client.py       # Fox ESS Cloud API client
    models.py       # Pydantic models for device data
  daikin/
    client.py       # Daikin Onecta API client (OAuth2)
    auth.py         # OAuth2 flow helper
    models.py       # Pydantic models
  cli/
    __main__.py     # CLI entrypoint (Click)
    status.py       # Status dashboard command
    monitor.py      # Continuous monitor loop
  config.py         # Config + env loader
  notifier.py       # WhatsApp/webhook alerts
tests/
  test_foxess.py
  test_daikin.py
```

## Credentials

All credentials live in `.env` (never committed). See `.env.example`.
Token files (`*.json`) are gitignored.
