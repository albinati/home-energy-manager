# ARCHITECTURE V2 PLAN: Home Energy Manager (Bulletproof Edition)

## 1. Database Schema Updates (`energy_state.db`)

We need to store environmental data and enhance the logging table to support the Hedge Fund reporting.

```sql
-- New table for Open-Meteo forecasts
CREATE TABLE meteo_forecast (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    forecast_date TEXT NOT NULL,
    slot_time TEXT NOT NULL,
    temp_c REAL,
    solar_w_m2 REAL,
    UNIQUE(slot_time)
);

-- Execution log for PnL Hedge Fund reporting
CREATE TABLE pnl_execution_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slot_time TEXT NOT NULL,
    kwh_consumed REAL,
    agile_price_pence REAL,
    svt_price_pence REAL,
    delta_pence REAL
);
```

## 2. The Physics Module (`src/physics.py`)

This module handles thermodynamic calculations natively, preventing LLM hallucination and ensuring deterministic Daikin setpoints.

```python
import math
from datetime import datetime

HEAT_LOSS_C_PER_HOUR = 0.3  # Daikin Altherma typical standing loss
MARGIN_OF_SAFETY_C = 0.5    # Pipe loss margin

def calculate_dhw_setpoint(target_temp_c: float, target_time_iso: str, heat_end_time_iso: str) -> float:
    """
    Calculates the exact Daikin tank setpoint required to hit a target temp at a future time.
    e.g., target 45C at 09:30, heating ends at 05:30 -> 4 hours of decay.
    """
    end_dt = datetime.fromisoformat(heat_end_time_iso.replace('Z', '+00:00'))
    target_dt = datetime.fromisoformat(target_time_iso.replace('Z', '+00:00'))
    
    hours_diff = (target_dt - end_dt).total_seconds() / 3600.0
    if hours_diff <= 0:
        return target_temp_c # Immediate usage
        
    expected_loss = hours_diff * HEAT_LOSS_C_PER_HOUR
    dynamic_setpoint = target_temp_c + expected_loss + MARGIN_OF_SAFETY_C
    
    # Cap at absolute safe max (e.g., 65C) to prevent boiling/stress
    return min(round(dynamic_setpoint, 1), 65.0)
```

## 3. Environmental Context (`src/meteo_fetcher.py`)

Runs daily at 16:05 immediately after the Octopus API fetch.

```python
import requests

def fetch_tomorrow_forecast(lat=51.49, lon=-0.25): # West London (Chiswick/Acton)
    url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&hourly=temperature_2m,shortwave_radiation"
    res = requests.get(url, timeout=10).json()
    # Logic to parse arrays and insert into `meteo_forecast`
    return res

def evaluate_survival_fallback(db_conn, date_str):
    """
    If solar radiation is near zero (heavy clouds/rain) AND agile prices don't plunge below 5p,
    force a night charge (even at 12p-15p) to survive the 16:00-19:00 peak (34p).
    """
    pass # Implementation details
```

## 4. Push Webhooks (`src/notifier.py`)

Replaces the fragile LLM cron polling. The Python daemon pushes state actively to OpenClaw.

```python
import requests

# Pointing to a local OpenClaw webhook intake or Telegram bot API directly
WEBHOOK_URL = "http://127.0.0.1:18789/api/webhook/energy_alert" 

def push_alert(event_type: str, payload: dict):
    """
    Types: 
    - 'CHEAP_WINDOW_START': "Bateria sugando, Daikin aquecendo"
    - 'PEAK_WINDOW_START': "Escudo ativado, SoC: 95%"
    - 'DAILY_PNL': Hedge fund report metrics for D-1
    """
    try:
        requests.post(WEBHOOK_URL, json={"type": event_type, "data": payload}, timeout=5)
    except Exception as e:
        print(f"Webhook push failed: {e}. Failsafe: logging locally.")
```

## 5. Optimizer Integration (`src/optimizer.py`)

The daily solver will now:
1. Call `fetch_agile_rates()`.
2. Call `fetch_tomorrow_forecast()`.
3. Check `evaluate_survival_fallback()` to decide on defensive night charging.
4. Use `calculate_dhw_setpoint(45.0, '09:30', '05:30')` to schedule the morning shower precisely.
5. Write the final deterministic plan to `action_schedule`.