# Security

## Reporting a vulnerability

This repo has **GitHub Private Vulnerability Reporting** enabled. Please use that channel — don't file a public issue or post to discussions.

To report:

1. Go to the [Security tab](https://github.com/albinati/home-energy-manager/security) of the repo.
2. Click **"Report a vulnerability"**.
3. Describe the issue, attach proof-of-concept or logs as needed.

GitHub creates a private draft advisory only the maintainer can see. I'll acknowledge within 5 working days and aim to ship a fix within 30 days for anything actionable. If you want disclosure credit in the published advisory + release notes, say so in the report.

## Threat model — what this project guards against

This is a self-hosted controller running on private infrastructure (Tailscale / loopback only). It does not expose anything to the public internet by design. The interesting attack surface is:

| Asset | Threat | Mitigation |
|---|---|---|
| Daikin / Fox ESS / SmartThings tokens | Theft via filesystem read | Tokens stored under `data/` mounted into the container with restrictive perms. Container runs as uid 1001 with read-only rootfs. |
| OpenClaw MCP transport | Unauthenticated access | Bearer token at `data/.openclaw-token`, generated on first boot. `BearerAuthMiddleware` rejects all requests without it. |
| Hardware-write actions | Misuse via MCP | `OPENCLAW_READ_ONLY=true` kill switch in `.env`. Plan-approval flow with consent gates for high-impact changes. |
| Database | Tampering with state | SQLite at `data/energy_state.db` mounted with restrictive perms. Snapshot-based replay can detect plan tampering after the fact. |
| Octopus / OAuth credentials | Leak via logs | Secrets are read from `.env` only; redacted in logs by convention (audit `src/notifier.py` etc. if you suspect a leak). |

## Out of scope

- Denial-of-service (the service is single-tenant and rate-limited internally).
- Issues in upstream dependencies (Open-Meteo, Octopus, Fox ESS, Daikin, SmartThings) unless we're handling their responses unsafely.
- Local-host attacks where the attacker already has root on the host running the container.

## Dependencies

The dependency tree is intentionally small (see `requirements.txt`). If you spot a CVE in something we pull in, file via the Private Vulnerability Reporting channel above; I'll either bump the dep or remove it.
