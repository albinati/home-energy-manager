# Home Energy Manager - Bulletproof AI Upgrade Spec

You are stepping into a deterministic, highly resilient energy arbitrage system managing a FoxESS solar/battery inverter, a Daikin Altherma Heat Pump, and Octopus Agile dynamic tariffs.

The system is currently operational but lacks thermodynamic awareness, environmental forecasting, and push-based reporting. Your task is to plan the implementation of three critical upgrades.

## Upgrade 1: The Physics Module (Thermodynamic Decay)
The Python orchestrator must calculate thermal inertia natively, avoiding blind LLM reliance.
- **Requirement:** Implement a thermal decay constant (e.g., `HEAT_LOSS_C_PER_HOUR = 0.3`).
- **Logic:** For targeted DHW (Domestic Hot Water) events, such as the "09:30 AM 45°C Shower Target", the code must scan the cheapest Agile slots in the early morning (e.g., 04:00 AM). It must calculate the time delta between the end of heating and the usage time (e.g., 5.5 hours).
- **Execution:** The script dynamically calculates the optimal Daikin setpoint (`45°C + (5.5h * 0.3°C) = 46.6°C`) and writes this explicit target to the SQLite `action_schedule`.

## Upgrade 2: Environmental Context (Solar & Temp Predictor)
The script currently assumes static conditions. It needs external context to override price signals with survival tactics.
- **Requirement:** Integrate with the free `Open-Meteo` API.
- **Logic:** During the daily 16:05 Octopus fetch, also fetch tomorrow's Solar Irradiance (W/m²) and External Temperature forecast.
- **Execution:** If tomorrow's solar generation forecast is near zero (heavy rain/cloud) AND no deep plunge pricing (< 5p) exists in the daytime, the optimizer must trigger a defensive `Force Charge` during the cheapest night slot (even if it costs 12p-15p). This ensures the battery survives the 16:00-19:00 peak (34p).

## Upgrade 3: Reverse Webhooks (Push Reporting)
Polling the system externally via LLM crons is fragile. The Python daemon must push critical state changes.
- **Requirement:** Implement lightweight webhook dispatches pointing to the OpenClaw Gateway endpoint.
- **Logic & Events:**
  - *Start of Cheap Window:* "Cheap window active. Forcing FoxESS charge, heating DHW."
  - *Start of Peak Window (The Death Zone):* "Peak window active. House shielded. SoC is X%. Daikin heating suspended."
  - *D+1 Financial Report (Hedge Fund format):* Push a JSON payload with PnL, VWAP, and Slippage metrics at 08:00 AM for the LLM to format and deliver to the user.

## Objective for Cursor Agent
Operate in `--mode=plan` using the `opus-4.6-thinking` model.
Read the existing `/opt/projects/home-energy-manager` codebase (if accessible) or assume a standard Python/SQLite architecture based on the current context.
Produce a comprehensive technical design document (`ARCHITECTURE_V2_PLAN.md`) detailing exactly how these modules will be structured, the new DB schema additions, and the required Python functions. Do not write the final code yet; generate the blueprint for review.