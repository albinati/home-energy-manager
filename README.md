# home-energy-manager

[![Tests](https://github.com/albinati/home-energy-manager/actions/workflows/tests.yml/badge.svg)](https://github.com/albinati/home-energy-manager/actions/workflows/tests.yml)
[![Latest release](https://img.shields.io/github/v/release/albinati/home-energy-manager)](https://github.com/albinati/home-energy-manager/releases)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![Container image](https://img.shields.io/badge/ghcr.io-home--energy--manager-blue?logo=docker)](https://github.com/albinati/home-energy-manager/pkgs/container/home-energy-manager)

> A single planning brain for a UK home running **Octopus Agile** + **Fox ESS battery** + **Daikin Altherma heat pump**. Solves a 24–48 h MILP every few minutes, uploads a Fox ESS Scheduler V3, drives Daikin via Onecta, and exposes a 57-tool MCP surface for Claude / OpenClaw.

## Why it exists

Most home-battery controllers run hand-coded rules per appliance. Multi-vendor optimisation against half-hourly Agile prices, weather-dependent heat-pump demand, and DHW pre-heat windows is the sort of thing rules get wrong. So this is a real solver — `PuLP` over a 96-slot horizon — that minimises grid cost while respecting:

- Battery RT efficiency, SoC bounds, ramp limits, peak-export robustness
- Heat-pump COP curve vs outdoor temperature, weather-compensation curve, DHW tank thermal mass
- Octopus Agile import + Outgoing Agile export prices, SVT shadow cost for accountability
- Negative-price plunges, peak windows, scenario perturbations for cold-night safety

Every solve is snapshotted to SQLite so any past day's plan can be re-run under today's code (closed-loop replay) — that's the regression gate that lets you tune the model without breaking last week's planner.

```
                ┌─────────────────────────────┐
  Octopus Agile ──→  half-hourly tariff       │
  Open-Meteo    ──→  temp + cloud + radiation │
  Fox ESS       ──→  load, PV, SoC, schedule  │
  Daikin Onecta ──→  tank, indoor, outdoor    │     PuLP MILP
  SmartThings   ──→  appliance state          │ ──→ 24–48h plan ──→  Fox V3 schedule
                │                             │                      Daikin action_schedule
  Past data     ──→  PV calibration table     │                      OpenClaw consent hooks
                ──→  load profile             │
                ──→  Daikin physics priors    │
                └─────────────────────────────┘
                            ↑                                 ↓
                            │                            ┌────────┐
                            └─── snapshots ────────────  │ replay │  ← regression gate
                                 (closed-loop)           └────────┘
```

## Status

This is a working personal project running 24/7 on one site (UK, London). It is **public so the architecture and accuracy work are open**, not because it is a turn-key product. Hardware specs, integrations, and tariff defaults are tuned to one installation:

- 4.5 kW PV array, Fox H1-5.0-E-G2 inverter, EP11 battery
- Daikin Altherma 3 H HT (passive mode in summer, active LWT modulation in heating season)
- G98 single-phase export limit 3.68 kW
- Octopus Agile import + Outgoing Agile export
- Heat pump runs on Daikin's own weather curve; LP shifts demand, doesn't override the curve

Everything is parameterised via `.env`. Adapting it to a different setup is feasible but undocumented — open a discussion if you want to try.

## What it does

- **Octopus Agile fetch** every 30 min. Stores import + Outgoing Agile rates in SQLite for the next ~36 h.
- **Open-Meteo forecast** every LP solve — temperature, shortwave radiation, cloud cover. Snapshotted per fetch for replay.
- **PV forecast calibration** — three-tier resolver: cloud-aware `(hour, cloud bucket)` table → per-hour-of-day table → flat factor. Today-aware OCF Quartz-style adjuster on top.
- **Load forecast accuracy evaluator** — per-slot MAE/RMSE/bias broken down by local hour, plus a daily Daikin physics check against Onecta-measured kWh. Captures the biases the LP hasn't yet learned.
- **MILP solver (PuLP)** — 96-slot horizon, soft penalties for cycling/comfort/inverter stress, scenario LP for peak-export robustness, twice-daily and tier-boundary MPC.
- **Fox ESS Scheduler V3** — single daily upload of the optimised charge/discharge windows. Heuristic fallback if the LP fails.
- **Daikin Onecta** — `action_schedule` rows that the heartbeat applies; LWT offset, tank target, weather regulation toggles. OAuth2 with auto-refresh.
- **SmartThings appliance dispatch** — washer/dryer/dishwasher start times picked by the LP given a deadline; physical Smart Control button is the consent gate.
- **Notifications via OpenClaw hook** — twice-daily digest, plan-revision pings, negative-price alerts. No direct chat APIs from this repo.
- **57-tool MCP surface** — Fox, Daikin, Octopus, optimization, replay, dispatch decisions. Bearer-guarded HTTP transport.
- **Closed-loop replay + regression gate** — every LP solve is a frozen, replayable snapshot; `scripts/check_lp_regression.py --mode=both` blocks merges that make the planner worse.

## How it works

The full architecture is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). The short version:

1. A scheduler tick fetches the latest Agile rates + weather forecast.
2. Past-data layers (load profile, residual-load profile minus physics-Daikin, PV calibration, dispatch decisions) feed into `lp_inputs_snapshot`.
3. `solve_lp()` runs PuLP/CBC over the 96-slot horizon with soft objective penalties for cycling, comfort slack, and inverter stress.
4. A scenario LP (optimistic / nominal / pessimistic perturbations) filters `peak_export` slots that would lose money under cold-night conditions.
5. The plan is persisted (`lp_solution_snapshot`), uploaded to Fox V3, and emitted as `action_schedule` rows for Daikin.
6. The 2-minute heartbeat reconciles live hardware with the schedule and writes `execution_log`.
7. MPC re-solves trigger on tier boundaries, forecast revisions, and Octopus fetches.

For decisions about peak-export robustness, see [docs/DISPATCH_DECISIONS.md](docs/DISPATCH_DECISIONS.md). For the OpenClaw boundary contract, [docs/OPENCLAW_BOUNDARY.md](docs/OPENCLAW_BOUNDARY.md). For the live-ops runbook, [docs/RUNBOOK.md](docs/RUNBOOK.md).

## Quick start (local sim box)

The local checkout is for development and replay against a copy of prod state — **it must never touch live hardware**.

```bash
git clone https://github.com/albinati/home-energy-manager.git
cd home-energy-manager
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

cp .env.example .env
# Edit .env — at minimum set OPENCLAW_READ_ONLY=true so nothing dials out.

pytest                                    # 837+ tests, ~3 min on a laptop
python -m src.cli serve                   # FastAPI on :8000, MCP at /mcp
```

The web UI is at `http://localhost:8000/`, OpenAPI docs at `/docs`, MCP transport at `/mcp` (bearer-guarded; token at `data/.openclaw-token`).

## Production deployment

The production target is an **immutable container** pulled from GHCR — the application code is never editable on the host after cutover. State and secrets are bind-mounted; everything else is replaced by a new image pull.

```bash
docker pull ghcr.io/albinati/home-energy-manager:latest
# See deploy/README.md for the systemd unit + compose.yaml + cutover runbook.
```

| Concern | Path |
|---|---|
| Image | `ghcr.io/albinati/home-energy-manager:<sha>` (linux/arm64) |
| State volume | `/srv/hem/data/` (SQLite + tokens + snapshots) |
| Config | `/srv/hem/.env` (mounted ro) |
| Service | `hem.service` (wraps `docker compose up`) |
| API | `http://127.0.0.1:8000` (loopback + Tailscale) |

OAuth bootstrap (Daikin + SmartThings) uses one-shot containers documented in [`deploy/README.md`](deploy/README.md).

## Hardware & integrations

| Vendor | What we use | Auth |
|---|---|---|
| Octopus Energy | Agile import + Outgoing Agile export rates, optional consumption backfill | API key (read-only) |
| Open-Meteo | Hourly forecast (temp, radiation, cloud cover) | None — public |
| Fox ESS | SoC, PV, load, grid, Scheduler V3 upload | Open API key + signature |
| Daikin Altherma | Status read + LWT/tank writes via Onecta | OAuth2 (auto-refresh) |
| Samsung SmartThings | Washer/dryer/dishwasher schedule | OAuth2 (auto-refresh) |
| OpenClaw / Claude | Reads state, requests plan changes, gets notifications | Bearer-guarded HTTP MCP |

## Roadmap

Active epic: **[#193 V11 — Accuracy via past-data integration & uncertainty modelling](https://github.com/albinati/home-energy-manager/issues/193)**.

| | Story | Status |
|---|---|---|
| ✅ | V11-A — Cloud-cover & full-input snapshot capture | shipped (#240) |
| ✅ | V11-E — Adaptive PV calibration trigger | shipped (#198) |
| 🟡 | V11-B — Quantile-based scenario perturbations | pending |
| 🟡 | V11-C — DHW draw learning (rolling 14-day prior) | pending |
| 🟡 | V11-D — Occupancy & variable-load inference | pending |

Other open work: [Daikin physics calibration via 2-hourly Onecta consumption](https://github.com/albinati/home-energy-manager/issues/238), [Travel-period aware load profile](https://github.com/albinati/home-energy-manager/issues/161), [Daikin tank reheat anomaly detection](https://github.com/albinati/home-energy-manager/issues/184).

The full backlog is on the [issues board](https://github.com/albinati/home-energy-manager/issues).

## Contributing

Adding a feature, reproducing a bug, or porting to a different installation? See [CONTRIBUTING.md](CONTRIBUTING.md). Security issues — see [SECURITY.md](SECURITY.md).

The PR template requires the LP regression gate (`scripts/check_lp_regression.py --mode=both`) to pass for any change under `src/scheduler/` — that's how we keep "the LP must outperform every earlier version always" honest.

## License

[Apache 2.0](LICENSE). Attribution requirements live in [NOTICE](NOTICE).
