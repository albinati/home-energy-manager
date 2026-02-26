# Shell scripts

Run the API server and tests from the project root. **No secrets** — credentials are read from `.env` only.

| Script | Description |
|--------|-------------|
| `./bin/run` | Main entrypoint. Use `./bin/run help` for commands. |
| `./bin/start` | Start API server as daemon (background). OpenClaw and dashboard available. |
| `./bin/stop` | Stop the daemon. |
| `./bin/status` | Show daemon status and API URL. |
| `./bin/serve` | Start API server in foreground (for debugging). |
| `./bin/test-foxess` | Test Fox ESS Open API (curl). Verifies `.env` credentials. |

**Requirements:** From project root, with a venv at `.venv` and `.env` configured (see main README).  
**OpenClaw:** Point agents at `http://<this-host>:8000`; credentials stay in `.env` on the server.
