# Indoor temperature sensor ingestion

Moved verbatim from CLAUDE.md on 2026-10-31; the CLAUDE.md section is the summary.

## Indoor temperature sensor ingestion (#540 W1)

The Altherma has no room stat, so the house's indoor temperature was never
measured — the winter thermal model (#540) needs it. An ESPHome room sensor
pushes readings to **`POST /api/v1/sensors/indoor`** (`src/api/routers/sensors.py`):
batch (1–2000 readings), idempotent on `(captured_at, room)`, stored in
`room_temperature_history`. Downstream: LP initial state + dispatch comfort
guard read the freshest reading (`INDOOR_SENSOR_STALE_MINUTES=30`); the W2
thermal learner reads the history. Read back via `GET /api/v1/sensors/indoor`
and `GET /api/v1/sensors/thermal-calibration`.

**Full per-device logging (#540 W1c).** The endpoint has TWO sinks:
`room_temperature_history` keeps ONLY the in-band `temp_c` (what the LP/thermal
model read), while `device_reading_log` is the **lossless audit of everything a
device sends** — typed columns for the known metrics (temp/humidity/pressure/
mac/device_id) + a `payload_json` blob so any extra field (2nd temperature,
RSSI, battery…) survives with no migration. `IndoorReading` is `extra="allow"`;
`temp_c` is optional (a humidity-only device still logs) and an out-of-band temp
(85 °C fault) is logged but NOT routed to thermal history. Dedup on
`(device_key, captured_at)`, `device_key = mac|device_id|source|room`. Read
back: `GET /api/v1/sensors/devices` (one row per device + latest metrics) and
`GET /api/v1/sensors/device-log?device=&hours=` (raw rows w/ full `payload`),
both viewer. POST returns `{received, written (temp rows), logged (device-log)}`.

**Network path (sensor at home → HEM on Hetzner).** The sensor is on the house
LAN; the HEM is a cloud box behind Tailscale — ESPHome can't join the tailnet.
It reuses the **existing `hem-ui` Tailscale funnel (`:8443`)**, which already
publishes `/api/` with valid TLS (and does NOT expose `/mcp`) — no new proxy,
port, or funnel. The sensor POSTs to
`https://<host>.ts.net:8443/api/v1/sensors/indoor` carrying a **scoped**
`HEM_SENSOR_INGEST_TOKEN` — NOT admin. (The funnel is a dumb TLS tunnel to one
local port with no path ACL, so route-level containment is done entirely by the
scoped token in the middleware, below — not by the funnel.)

**Scoped token (`ApiV1RoleAuth.ingest_tokens`).** `middleware.py:_ingest_allowed`
lets this token satisfy ONLY a *write* to `/api/v1/sensors/indoor` — never an
admin read (Settings/Journal) or any other write. So a firmware/network leak can
only post fake temperatures to that one endpoint; rotate the token to revoke a
device. Empty `HEM_SENSOR_INGEST_TOKEN` → feature off (admin-only, as before).

Full deploy (token mint + smoke test) + ESPHome YAML skeleton: `deploy/README.md` §12.

---

