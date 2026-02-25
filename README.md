# home-energy-manager

Unified controller for Fox ESS battery + Daikin Altherma heat pump (Onecta).

Built for: 12 Whellock Rd — Daikin Altherma ASHP + Fox ESS battery + solar panels (SEG via British Gas).

## What it does

- **Fox ESS**: Read real-time battery SoC, solar production, grid import/export, inverter stats. Control charge/discharge mode and time-of-use schedules.
- **Daikin Onecta**: Read heat pump status, leaving water temperature, outdoor temperature, DHW tank temperature. Control power, temperature targets, LWT offset, operation mode, and weather regulation.
- **Smart scheduling**: Time-of-use optimisation — charge battery on cheap-rate periods, pre-heat/cool with solar surplus.
- **OverBot integration**: Notifies via WhatsApp when action is needed (e.g. grid export curtailed, battery full, temperature drifting).

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
| `ALERT_WHATSAPP_NUMBER` | No | For WhatsApp alerts via OverBot |

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
```

### Example output

```
┌─ Daikin: Altherma ────────────────────
│ Power       : ON
│ Mode        : heating
│ Outdoor     : 15°C
│ LWT         : 22°C
│ LWT offset  : -5
│ DHW tank    : 44°C (target 45°C)
│ Weather reg : on
└──────────────────────────────────────
```

## Project structure

```
src/
  foxess/
    client.py       # Fox ESS Open API client (signature auth)
    models.py       # Data models for device telemetry
  daikin/
    client.py       # Daikin Onecta API client (OAuth2)
    auth.py         # OAuth2 flow + tunnel-based setup
    models.py       # Data models for devices and status
  cli/
    __main__.py     # CLI entrypoint
  config.py         # Config + .env loader
  notifier.py       # WhatsApp/webhook alerts
tests/
  test_foxess.py    # Fox ESS client unit tests
  test_daikin.py    # Daikin client unit tests (14 tests)
```

## Credentials & security

- All credentials live in `.env` (gitignored — never committed)
- Token files (`*.json`) are gitignored
- SSL certificates (`*.pem`) for the local callback server are gitignored
- The `--setup` tunnel is ephemeral — the URL expires when the SSH session ends

## Known issues & workarounds

| Issue | Workaround |
|---|---|
| Daikin portal "unable to send add registration hook" (403) | Use `--setup` mode to create a public tunnel |
| CloudFront blocks `urllib` requests to Daikin IDP | Token exchange uses `curl` subprocess |
| CloudFront blocks `redirect_uri` containing `localhost` | The tunnel provides a real public HTTPS URL |
| Daikin portal "Refresh Secret" button returns 400 | Delete and recreate the app instead |
| Fox ESS unofficial API returns HTTP 406 | Use the official Open API with `FOXESS_API_KEY` |
| Daikin API reads lag ~2-3s after writes | Normal for cloud-connected devices; not a bug |
