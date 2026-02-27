---
name: home-energy-manager
description: Control Daikin Altherma heat pump and Fox ESS battery via the Home Energy Manager REST API. Manage heating temperature, DHW tank, inverter modes, and charge schedules with built-in safety confirmations.
metadata: {"openclaw": {"requires": {"env": ["HOME_ENERGY_API_URL"]}, "primaryEnv": "HOME_ENERGY_API_URL", "emoji": "🏠"}}
---

# Home Energy Manager

You can control a home energy system (Daikin Altherma heat pump + Fox ESS battery) through a REST API.

**Base URL**: Use the environment variable `HOME_ENERGY_API_URL` (e.g. `http://192.168.1.100:8000`).

## How to discover available actions

Before doing anything, fetch the capabilities list to see what's available and what constraints apply:

```
GET {HOME_ENERGY_API_URL}/api/v1/openclaw/capabilities
```

This returns every action you can take, its parameters, validation ranges, and whether it requires confirmation.

## How to read status

**Daikin heat pump status:**
```
GET {HOME_ENERGY_API_URL}/api/v1/daikin/status
```

Returns: `is_on`, `mode`, `room_temp`, `target_temp`, `outdoor_temp`, `lwt`, `lwt_offset`, `tank_temp`, `tank_target`, `weather_regulation`.

**Fox ESS battery status:**
```
GET {HOME_ENERGY_API_URL}/api/v1/foxess/status
```

Returns: `soc` (battery %), `solar_power`, `grid_power`, `battery_power`, `load_power`, `work_mode`.

## Data report (energy, cost, charts — for OpenClaw)

All insight data is provided by the API as a **data report**. Use the report endpoint for a single response that includes every metric and a spoken summary.

**Full data report (recommended for OpenClaw):**
```
GET {HOME_ENERGY_API_URL}/api/v1/energy/report
GET {HOME_ENERGY_API_URL}/api/v1/energy/report?period=month&month=YYYY-MM
GET {HOME_ENERGY_API_URL}/api/v1/energy/report?period=year&year=YYYY
GET {HOME_ENERGY_API_URL}/api/v1/energy/report?period=day&date=YYYY-MM-DD
GET {HOME_ENERGY_API_URL}/api/v1/energy/report?period=week&date=YYYY-MM-DD
```

- **No query params**: current month’s report (same as `period=month&month=YYYY-MM` for this month).
- **period** = `day` | `week` | `month` | `year`. For **day/week** use `date=YYYY-MM-DD`. For **month** use `month=YYYY-MM`. For **year** use `year=YYYY`.

**Response (full data report):**

| Field | Description |
|-------|-------------|
| `period` | `"day"` \| `"week"` \| `"month"` \| `"year"` |
| `period_label` | Human label, e.g. `"Feb 2026"`, `"4–10 Feb 2026"` |
| `energy` | `import_kwh`, `export_kwh`, `solar_kwh`, `load_kwh`, `charge_kwh`, `discharge_kwh` |
| `cost` | `net_cost_pounds`, `import_cost_pounds`, `export_earnings_pounds`, `net_cost_pence`, etc. |
| `heating_estimate_kwh` | Estimated heating consumption (when available) |
| `equivalent_gas_cost_pounds` | What the same period would cost on gas |
| `gas_comparison_ahead_pounds` | Positive = ahead with solar + heat pump; negative = gas would be cheaper |
| `chart_data` | Array of `{ date, import_kwh, export_kwh, solar_kwh, load_kwh, charge_kwh, discharge_kwh }` for charts |
| `heating_analytics` | When available: `heating_percent_of_cost`, `heating_percent_of_consumption`, `degree_days`, `temp_bands`, etc. |
| `summary` | Short narrative for TTS/chat: cost, balance, gas comparison. Use this to speak the report. |

Use `summary` for voice answers; use the structured fields for exact numbers, charts, or follow-up questions. Returns 503 if Fox ESS is not configured; 400 for invalid params; 502 on Fox ESS errors.

**Legacy endpoints (still supported):**

- **Monthly only (no chart_data, no day/week/year):**  
  `GET {HOME_ENERGY_API_URL}/api/v1/energy/monthly?month=YYYY-MM`  
  Returns: same `energy`, `cost`, heating/gas fields as above.

- **Narrative only (no structured data):**  
  `GET {HOME_ENERGY_API_URL}/api/v1/energy/insights`  
  Returns: `{ "summary": "..." }` for current month. Prefer `/energy/report` to get data + summary in one call.

## How to execute actions

Use the unified execute endpoint:

```
POST {HOME_ENERGY_API_URL}/api/v1/openclaw/execute
Content-Type: application/json

{"action": "<action_name>", "parameters": {<params>}}
```

### Actions that do NOT require confirmation

These execute immediately:

| Action | Parameters | Notes |
|--------|-----------|-------|
| `daikin.temperature` | `{"temperature": 21}` | Range: 15-30°C. **BLOCKED when weather regulation is active** — use `daikin.lwt_offset` instead. |
| `daikin.lwt_offset` | `{"offset": -3}` | Range: -10 to +10. Works in all modes including weather regulation. |
| `daikin.mode` | `{"mode": "heating"}` | Options: `heating`, `cooling`, `auto`, `fan_only`, `dry` |
| `daikin.tank_temperature` | `{"temperature": 45}` | Range: 30-60°C |
| `foxess.charge_period` | `{"start_time": "00:30", "end_time": "05:00", "target_soc": 90}` | Optional: `period_index` (0 or 1) |

### Actions that REQUIRE confirmation (2-step flow)

These are destructive or mode-changing operations. The API enforces a confirmation step:

| Action | Parameters |
|--------|-----------|
| `daikin.power` | `{"on": true}` or `{"on": false}` |
| `daikin.tank_power` | `{"on": true}` or `{"on": false}` |
| `foxess.mode` | `{"mode": "Self Use"}` — options: `Self Use`, `Feed-in Priority`, `Back Up`, `Force charge`, `Force discharge` |

**Step 1** — Send the action. You'll get back a `confirmation_token`:
```json
{
  "requires_confirmation": true,
  "action": {"action_id": "abc123...", "description": "Turn Daikin OFF", "status": "pending"},
  "message": "Confirmation required: Turn Daikin OFF. Re-send with confirmation_token='abc123...' to execute."
}
```

**Step 2** — Confirm by re-sending with the token:
```json
POST {HOME_ENERGY_API_URL}/api/v1/openclaw/execute
{"action": "daikin.power", "parameters": {"on": false}, "confirmation_token": "abc123..."}
```

Confirmation tokens expire after 5 minutes.

## Critical rules

1. **Always check status before making changes.** Read the current state to understand what mode the system is in.
2. **Weather regulation**: When `weather_regulation` is `true` in the Daikin status, you CANNOT set room temperature. Use `daikin.lwt_offset` to adjust heating intensity instead.
3. **Confirmation flow**: Never skip the 2-step confirmation for power and mode changes. Always tell the user what you're about to do and confirm the result.
4. **Rate limiting (internal)**: The API enforces a 5-second cooldown between commands of the same type. If you get a 429 response from the local API, wait 5 seconds and retry.
5. **Rate limiting (Daikin cloud)**: The Daikin Onecta Cloud API has a **200 requests/day** limit. This is a hard daily quota. Avoid polling status too frequently — 10-15 minute intervals are recommended for automated refreshes. A 429 from the Daikin cloud means you've hit the daily limit and must wait until the next day.
6. **Temperature ranges**: Room temp 15-30°C, tank temp 30-60°C, LWT offset -10 to +10. The API will reject out-of-range values.
7. **Fox ESS may be unavailable**: If Fox ESS returns a 503, it means credentials are not yet configured. Only Daikin operations will work.

## Recommendation-only mode (403)

If the API returns **403** on `POST /api/v1/openclaw/execute` with a message like "recommendation-only mode", the server is configured so OpenClaw must **not** execute changes. In that case:

- **Only recommend** actions to the user (e.g. "I suggest setting the temperature to 21°C" or "Consider switching to Feed-in Priority").
- Tell the user to apply changes themselves via the **dashboard** (web UI) or **CLI**.
- Do **not** retry execute or attempt to bypass; respect the safeguard.

## Error handling

- `400` — Invalid parameters (bad mode, out-of-range value)
- `403` — Recommendation-only mode: do not execute; suggest actions and tell the user to apply via dashboard/CLI.
- `404` — No devices found
- `409` — Action blocked (e.g. setting temperature during weather regulation)
- `410` — Confirmation token expired
- `429` — Rate limited. Check the error message: if it mentions "5 seconds", wait and retry; if it mentions "API rate limit exceeded", you've hit the Daikin daily limit.
- `502` — Upstream device API error
- `503` — Service not configured
