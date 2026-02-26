# home-energy-manager

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Unified controller for Fox ESS battery + Daikin Altherma heat pump (Onecta).

## What it does

- **Fox ESS**: Read real-time battery SoC, solar production, grid import/export, inverter stats. Control charge/discharge mode and time-of-use schedules.
- **Daikin Onecta**: Read heat pump status, radiator temperature, outdoor temperature, DHW tank temperature. Control power, temperature targets, heating curve offset, and weather regulation.
- **Smart scheduling**: Time-of-use optimisation — charge battery on cheap-rate periods, pre-heat with solar surplus.
- **Energy Dashboard** (coming soon): Track energy costs with Octopus Energy, British Gas, and other providers.
- **OpenClaw integration**: Notifies via configurable channel (webchat, Telegram, etc.) when action is needed. Exposes REST API for AI agent control.

## Quick start

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env — see setup sections below
```

## Setup

### 1. Fox ESS — Open API key

1. Log in to [foxesscloud.com](https://www.foxesscloud.com)
2. Go to **User Profile → API Management** and generate an API key
3. Copy your **Device SN** from Settings → Device
4. Set `FOXESS_API_KEY` and `FOXESS_DEVICE_SN` in `.env`

The client uses the official Open API (`/op/v0/`) with MD5-signature authentication. The old username/password API (`/c/v0/`) is deprecated and returns HTTP 406.

### 2. Daikin Onecta — OAuth2 (the tricky part)

Daikin uses OAuth2 with a registration hook — their portal pings your redirect URI when you create an app. Since you're running locally, that ping will fail unless you expose the server publicly during registration.

#### First-time setup (recommended)

The `--setup` mode handles everything: it creates a public SSH tunnel via [localhost.run](https://localhost.run), starts a local callback server, and walks you through app registration + authentication in one go.

```bash
python -m src.daikin.auth --setup
```

This will:
1. Start a local HTTP server on port 8080
2. Create a free public HTTPS tunnel (e.g. `https://abc123.lhr.life`)
3. Print the redirect URI — paste it into the Daikin developer portal
4. Handle the registration hook ping (responds 200 OK)
5. Prompt for Client ID and Client Secret → saves to `.env`
6. Open a browser for Daikin login → captures the OAuth callback
7. Exchange the auth code for tokens → saves to `.daikin-tokens.json`

#### Why the tunnel hack?

Daikin's developer portal at [developer.cloud.daikineurope.com](https://developer.cloud.daikineurope.com) has two quirks:

1. **Registration hook**: When you create or update an app, the portal makes an HTTP request to your redirect URI to verify it's reachable. If it can't reach it, you get `unable to send add registration hook from HTTP (403)`. Since `localhost` / `127.0.0.1` / `lvh.me` all resolve to loopback and Daikin's servers have SSRF protection, they can't reach your machine.

2. **CloudFront WAF**: Daikin's IDP (`idp.onecta.daikineurope.com`) is behind AWS CloudFront, which blocks requests that contain `localhost` in the `redirect_uri` query parameter. Python's `urllib` also gets blocked (likely user-agent filtering), so token exchange uses `curl` via subprocess as a workaround.

The SSH tunnel solves both problems: it gives you a real public HTTPS URL that Daikin can ping during registration AND that the browser can redirect to during the OAuth flow.

#### Subsequent auth (token expired / re-auth needed)

Once the app is registered, you don't need the tunnel again — just run:

```bash
python -m src.daikin.auth
```

Or exchange a code manually:

```bash
python -m src.daikin.auth --code <PASTE_CODE_HERE>
```

Tokens auto-refresh — you shouldn't need to re-auth unless the refresh token expires (typically months).

### 3. Environment variables

See `.env.example` for all options. Key variables:

| Variable | Required | Description |
|---|---|---|
| `FOXESS_API_KEY` | Yes* | Fox ESS Open API key |
| `FOXESS_DEVICE_SN` | Yes | Inverter serial number |
| `DAIKIN_CLIENT_ID` | Yes | From Daikin developer portal |
| `DAIKIN_CLIENT_SECRET` | Yes | From Daikin developer portal |
| `DAIKIN_REDIRECT_URI` | No | Defaults to `http://localhost:8080/callback` |
| `OCTOPUS_API_KEY` | No | Octopus Energy API key (for tariff tracking) |
| `OCTOPUS_ACCOUNT_NUMBER` | No | Octopus Energy account number |
| `BRITISH_GAS_API_KEY` | No | British Gas API key (if available) |
| `ALERT_OPENCLAW_URL` | No | OpenClaw send endpoint (default `http://127.0.0.1:18789/api/send`) |
| `ALERT_CHANNEL` | No | Channel to send alerts (e.g. `webchat`); leave blank for stdout only |
| `OPENCLAW_READ_ONLY` | No | If `true` (default), OpenClaw cannot execute; only recommend. Apply via dashboard/CLI. |
| `OPENAI_API_KEY` | No | For AI Assistant (recommendations). If unset, rule-based suggestions only |
| `AI_ASSISTANT_PROVIDER` | No | Default `openai` |
| `AI_ASSISTANT_MODEL` | No | Default `gpt-4o-mini` |
| `MANUAL_TARIFF_IMPORT_PENCE` | No | Import rate (p/kWh) for cost-aware suggestions when no provider is connected |
| `MANUAL_TARIFF_EXPORT_PENCE` | No | Export rate (p/kWh) for cost-aware suggestions |

## Web UI & API Server

Start the web server for browser-based control and REST API access:

```bash
python -m src.cli serve                    # Start on default port 8000 (foreground)
python -m src.cli serve --port 3000        # Custom port
```

**Daemon mode** (background server for data updates and OpenClaw API):

```bash
python -m src.cli daemon start             # Start API server in background
python -m src.cli daemon status             # Show PID and URLs
python -m src.cli daemon stop               # Stop the daemon
```

The daemon writes a PID file (`.home-energy-manager.pid`) and log (`daemon.log`) in the project root. Use `daemon start` when you want the dashboard and OpenClaw API available without keeping a terminal open.

**Shell scripts** (from project root):

| Command | Description |
|---------|-------------|
| `./bin/run help` | Show all commands |
| `./bin/start` | Start daemon (same as `daemon start`) |
| `./bin/stop` | Stop daemon |
| `./bin/status` | Daemon status and API URL |
| `./bin/serve` | Run server in foreground |
| `./bin/test-foxess` | Test Fox ESS API (reads `.env`) |

See `bin/README.md` for details. Scripts do not contain any secrets; credentials are read from `.env` only.

- **Web Dashboard**: `http://localhost:8000/` — visual status + control buttons, **AI Assistant** tab for optimization
- **API Docs**: `http://localhost:8000/docs` — interactive Swagger UI
- **OpenClaw API**: `http://localhost:8000/api/v1/openclaw/capabilities`

### AI Assistant

The dashboard includes an **AI Assistant** tab that suggests optimizations for the heat pump and battery based on a **comfort vs cost** balance:

1. Open the **AI Assistant** tab and choose **Comfort**, **Balanced**, or **Cost savings**.
2. Optionally enter a message (e.g. “Lower heating at night”, “Charge on cheap rate”).
3. Click **Get recommendations** — the assistant returns a short explanation and a list of suggested actions (temperature, LWT offset, tank, battery mode, charge periods).
4. Select the actions you want and click **Apply selected**. Actions that require confirmation (e.g. power off, mode change) show a **Confirm** button; use it to complete the change.

Without `OPENAI_API_KEY`, the assistant uses built-in rule-based suggestions. With a key, it uses the configured model for richer, context-aware recommendations. Set `MANUAL_TARIFF_IMPORT_PENCE` (and optionally `MANUAL_TARIFF_EXPORT_PENCE`) in `.env` for cost-aware suggestions when no energy provider is connected.

### API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/v1/daikin/status` | GET | Get Daikin device status |
| `/api/v1/daikin/power` | POST | Turn on/off (requires confirmation) |
| `/api/v1/daikin/temperature` | POST | Set target temperature (15-30°C) |
| `/api/v1/daikin/lwt-offset` | POST | Set LWT offset (-10 to +10) |
| `/api/v1/daikin/mode` | POST | Set operation mode |
| `/api/v1/daikin/tank-temperature` | POST | Set DHW tank target (30-60°C) |
| `/api/v1/foxess/status` | GET | Get battery/solar status |
| `/api/v1/foxess/mode` | POST | Set work mode (requires confirmation) |
| `/api/v1/foxess/charge-period` | POST | Set charge schedule |
| `/api/v1/assistant/recommend` | POST | Get optimization suggestions (preference + optional message) |
| `/api/v1/assistant/apply` | POST | Apply suggested actions (returns confirmation tokens where needed) |
| `/api/v1/energy/providers` | GET | List energy providers and config status |
| `/api/v1/energy/tariff` | GET | Get current tariff info (coming soon) |
| `/api/v1/energy/usage` | GET | Get energy usage summary (coming soon) |
| `/api/v1/openclaw/capabilities` | GET | List all actions for AI agents |
| `/api/v1/openclaw/execute` | POST | Execute action with confirmation flow |
| `/api/v1/scheduler/status` | GET | Agile scheduler: current price, next cheap window, planned LWT adjustment |
| `/api/v1/scheduler/pause` | POST | Pause Agile-based Daikin LWT adjustments |
| `/api/v1/scheduler/resume` | POST | Resume Agile scheduler |
| `/api/v1/health` | GET | Lightweight health check |

### Safeguards

- **Confirmation flow**: Destructive actions (power off, mode changes) require a 2-step confirmation
- **Weather regulation guard**: Room temperature changes are blocked when weather regulation is active (use LWT offset instead)
- **Range validation**: Temperature setpoints validated against safe limits
- **Rate limiting**: 5-second cooldown between commands
- **Audit logging**: All control actions logged with timestamp

### OpenClaw skill

To use the **home-energy-manager** skill from OverBot/OpenClaw chat:

1. **Install the skill**: `cp -r skills/home-energy-manager ~/.openclaw/skills/`
2. **Configure** `~/.openclaw/openclaw.json` with the skill and `HOME_ENERGY_API_URL` (e.g. `http://localhost:8000`). See `AGENTS.md` for the exact JSON snippet.
3. **Health check**: On gateway boot, call `GET {HOME_ENERGY_API_URL}/api/v1/health` and start the API daemon if needed. See `BOOT.md`.

### OpenClaw integration

**No API keys or secrets go in OpenClaw.** Credentials (Fox ESS, Daikin, etc.) stay in `.env` on the machine where the Home Energy Manager API runs. OpenClaw only needs the **base URL** of that API.

1. **Start the API** on the host that has `.env` configured:
   ```bash
   ./bin/start
   # or: python -m src.cli daemon start
   ```

2. **From OpenClaw**, point the skill at that host and port. Copy the skill and set the URL (use your server’s IP or hostname and port; no credentials):
   ```bash
   cp -r skills/home-energy-manager ~/.openclaw/skills/
   ```
   In `~/.openclaw/openclaw.json` (or your OpenClaw config):
   ```json
   {
     "skills": {
       "entries": {
         "home-energy-manager": {
           "enabled": true,
           "env": { "HOME_ENERGY_API_URL": "http://YOUR_SERVER_IP:8000" }
         }
       }
     }
   }
   ```
   Replace `YOUR_SERVER_IP` with the host where the API runs (e.g. `192.168.1.100` or `localhost` if OpenClaw runs on the same machine).

3. **Endpoints** the agent uses (all unauthenticated REST; auth is handled by the server’s `.env`):
   - `GET {HOME_ENERGY_API_URL}/api/v1/openclaw/capabilities` — list actions
   - `POST {HOME_ENERGY_API_URL}/api/v1/openclaw/execute` — run an action (with confirmation flow when required)

4. **Recommendation-only safeguard**: By default `OPENCLAW_READ_ONLY=true`. The agent can read status and capabilities but **cannot execute** changes; `POST .../execute` returns 403. The agent should only recommend; you apply changes via the dashboard or CLI. Set `OPENCLAW_READ_ONLY=false` in `.env` if you want the agent to execute actions (after confirmation where required).

See `skills/home-energy-manager/SKILL.md` for the full instruction set and action list.

### Agile scheduler (Daikin LWT by price)

When on **Octopus Agile**, you can automatically shift heat pump load to cheap slots:

- **Cheap slots** (e.g. &lt; 12p/kWh, often 01:00–06:00): LWT offset is raised slightly to pre-heat (thermal mass stores cheap energy).
- **Peak window** (e.g. 16:00–19:00): LWT offset is lowered to coast on stored heat.
- **Normal**: Hold at 0.

Set `SCHEDULER_ENABLED=true` and `OCTOPUS_TARIFF_CODE` (e.g. `E-1R-AGILE-24-10-01-C`) in `.env`. The API runs a background job every 30 minutes and adjusts Daikin LWT via the first device. Use `GET /api/v1/scheduler/status` to see current price and planned adjustment; `POST /api/v1/scheduler/pause` and `resume` to turn the scheduler off or on without changing `.env`. See `.env.example` for `SCHEDULER_CHEAP_THRESHOLD_PENCE`, `SCHEDULER_PEAK_START`/`SCHEDULER_PEAK_END`, and `SCHEDULER_PREHEAT_LWT_BOOST`.

## CLI usage

```bash
# Full dashboard (Fox ESS + Daikin)
python -m src.cli status

# --- Fox ESS ---
python -m src.cli foxess status
python -m src.cli foxess mode "Self Use"
python -m src.cli foxess charge --from 00:30 --to 05:00 --soc 90

# --- Daikin ---
python -m src.cli daikin status
python -m src.cli daikin on                # Turn climate control on
python -m src.cli daikin off               # Turn climate control off
python -m src.cli daikin temp 21           # Set room temperature target
python -m src.cli daikin lwt-offset -3     # Set leaving water temp offset
python -m src.cli daikin tank-temp 45      # Set DHW tank target (30–60°C)
python -m src.cli daikin mode heating      # heating / cooling / auto

# --- Monitor ---
python -m src.cli monitor                  # Continuous loop with alerts

# --- Options ---
python -m src.cli status --json            # JSON output for OpenClaw
python -m src.cli daikin status --api      # Route through API server
```

### Example output

```
┌─ Daikin: Altherma ────────────────────
│ Power       : ON
│ Mode        : heating
│ Outdoor     : 15°C
│ Radiator    : 22°C
│ Curve adj.  : -5
│ DHW tank    : 44°C (target 45°C)
│ Weather reg : on
└──────────────────────────────────────
```

## Project structure

```
src/
  api/
    main.py         # FastAPI app + REST endpoints
    models.py       # Pydantic request/response schemas
    safeguards.py   # Confirmation tokens, rate limiting, audit
    templates/      # Jinja2 web UI templates (tabbed dashboard)
  foxess/
    client.py       # Fox ESS Open API client (signature auth)
    models.py       # Data models for device telemetry
  daikin/
    client.py       # Daikin Onecta API client (OAuth2)
    auth.py         # OAuth2 flow + tunnel-based setup
    models.py       # Data models for devices and status
  energy/
    models.py       # Energy provider data models
    provider.py     # Abstract base class for provider clients
  cli/
    __main__.py     # CLI entrypoint
  config.py         # Config + .env loader
  notifier.py       # WhatsApp/webhook alerts (via OpenClaw)
skills/
  home-energy-manager/
    SKILL.md        # OpenClaw / AgentSkills skill definition
tests/
  test_foxess.py    # Fox ESS client unit tests
  test_daikin.py    # Daikin client unit tests (14 tests)
```

## Credentials & security

- All credentials live in `.env` (gitignored — never committed)
- Token files (`*.json`) are gitignored
- SSL certificates (`*.pem`) for the local callback server are gitignored
- The `--setup` tunnel is ephemeral — the URL expires when the SSH session ends

## Energy Dashboard (Coming Soon)

The Energy tab in the web dashboard is a placeholder for upcoming energy provider integrations. When complete, it will support:

- **Octopus Energy**: Agile, Go, Tracker, and fixed tariffs with half-hourly pricing data
- **British Gas**: Fixed and variable tariffs, SEG export payments
- **Manual entry**: Enter your own rates for cost tracking

Features planned:
- Real-time tariff display
- Daily/weekly/monthly cost breakdown
- Export earnings tracking
- Time-of-use rate visualization
- Cost optimization suggestions

The API endpoints (`/api/v1/energy/*`) are stubbed and ready for implementation.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Known issues & workarounds

| Issue | Workaround |
|---|---|
| Daikin portal "unable to send add registration hook" (403) | Use `--setup` mode to create a public tunnel |
| CloudFront blocks `urllib` requests to Daikin IDP | Token exchange uses `curl` subprocess |
| CloudFront blocks `redirect_uri` containing `localhost` | The tunnel provides a real public HTTPS URL |
| Daikin portal "Refresh Secret" button returns 400 | Delete and recreate the app instead |
| Fox ESS unofficial API returns HTTP 406 | Use the official Open API with `FOXESS_API_KEY` |
| Daikin API reads lag ~2-3s after writes | Normal for cloud-connected devices; not a bug |
