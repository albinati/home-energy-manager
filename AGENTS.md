# Available skills (OverBot / OpenClaw)

When the OpenClaw gateway is running, the following skills can be enabled and used from chat.

## home-energy-manager

**Skill ID**: `home-energy-manager`  
**Description**: Control Daikin Altherma heat pump and Fox ESS battery via the Home Energy Manager REST API.

**Requires**: `HOME_ENERGY_API_URL` (e.g. `http://localhost:8000`) pointing at a running instance of this project's API server.

**Install** (from this repo):

```bash
cp -r skills/home-energy-manager ~/.openclaw/skills/
```

**Enable** in `~/.openclaw/openclaw.json`:

```json
{
  "skills": {
    "entries": {
      "home-energy-manager": {
        "enabled": true,
        "env": { "HOME_ENERGY_API_URL": "http://localhost:8000" }
      }
    }
  }
}
```

**Capabilities**: Control heating temperature, DHW tank, LWT offset, inverter modes, charge schedules; get AI-powered optimization suggestions. All without exposing Daikin/Fox ESS credentials to OpenClaw (the API handles auth).
