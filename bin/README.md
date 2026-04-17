# Shell scripts

Run the API server and tests from the project root. **No secrets** — credentials are read from `.env` only.

| Script | Description |
|--------|-------------|
| `./bin/run` | Main entrypoint. Use `./bin/run help` for commands. |
| `./bin/start` | Start API server as daemon (background). OpenClaw and dashboard available. |
| `./bin/stop` | Stop the daemon. |
| `./bin/status` | Show daemon status and API URL. |
| `./bin/serve` | Start API server in foreground (for debugging). |
| `./bin/mcp` | Start MCP server (`python -m src.mcp_server`) with the same Python selection as `serve`. |
| `./bin/test-foxess` | Test Fox ESS Open API (curl). Verifies `.env` credentials. |

**Python selection** (`bin/lib.sh`): On the host, a `.venv` is preferred when present. In Docker (`/.dockerenv`) or when `HEM_IN_CONTAINER=1`, **system `python3.11` then `python3`** is used so a bind-mounted host venv (e.g. Python 3.12) does not break with glibc mismatches. Override with `HEM_PYTHON=/path/to/python` if needed.

**Requirements:** From project root, with `.env` configured (see main README). A `.venv` is optional on the host if you use system Python 3.11+ with dependencies installed.  
**OpenClaw:** Point agents at `http://<this-host>:8000`; credentials stay in `.env` on the server.
