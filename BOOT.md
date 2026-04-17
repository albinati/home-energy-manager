# Boot / health-check (OpenClaw gateway)

To ensure the Home Energy Manager API is running when the OpenClaw gateway starts (so the `home-energy-manager` skill works), add a daemon health-check step.

## Health check

- **Endpoint**: `GET http://localhost:8000/api/v1/health` (or whatever host/port the API uses)
- **Expected**: HTTP 200 and `{"status": "ok"}`

If the request fails (connection refused or non-2xx), start the API daemon from the project root:

```bash
cd /path/to/home-energy-manager
source venv/bin/activate
python -m src.cli daemon start
```

Or run the server in foreground (e.g. under systemd or a process manager):

```bash
python -m src.api.main
# Uses API_HOST / API_PORT from .env (default 0.0.0.0:8000)
```

## Summary

1. On gateway boot, call `GET {HOME_ENERGY_API_URL}/api/v1/health`.
2. If the check fails, start the home-energy-manager API (daemon or foreground) so the skill can reach it.
3. The skill uses `HOME_ENERGY_API_URL` from OpenClaw config; point it at the same host/port you health-check.
