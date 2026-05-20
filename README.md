<div align="center">

# 🏠⚡ home-energy-manager

**A solver-driven planning brain for a UK home on Octopus Agile + Fox ESS battery + Daikin Altherma heat pump.**

Solves a 24–48 h MILP every few minutes, uploads a Fox ESS Scheduler V3, drives Daikin via Onecta, and exposes a 75-tool MCP surface for Claude.

[![Tests](https://github.com/albinati/home-energy-manager/actions/workflows/tests.yml/badge.svg)](https://github.com/albinati/home-energy-manager/actions/workflows/tests.yml)
[![Latest release](https://img.shields.io/github/v/release/albinati/home-energy-manager)](https://github.com/albinati/home-energy-manager/releases)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![Container image](https://img.shields.io/badge/ghcr.io-home--energy--manager-2496ED?logo=docker&logoColor=white)](https://github.com/albinati/home-energy-manager/pkgs/container/home-energy-manager)

[![Octopus Agile](https://img.shields.io/badge/Octopus-Agile-EE2E7B?logo=octopusdeploy&logoColor=white)](https://octopus.energy/smart/agile/)
[![Fox ESS](https://img.shields.io/badge/Fox_ESS-Scheduler_V3-1F6FEB)](https://www.fox-ess.com/)
[![Daikin Altherma](https://img.shields.io/badge/Daikin-Onecta-0093D0?logo=data:image/svg%2bxml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHZpZXdCb3g9IjAgMCAyNCAyNCIgZmlsbD0iI2ZmZiI+PHBhdGggZD0iTTEyIDJDNi40OCAyIDIgNi40OCAyIDEyczQuNDggMTAgMTAgMTAgMTAtNC40OCAxMC0xMFMxNy41MiAyIDEyIDJ6Ii8+PC9zdmc+)](https://daikin-cdc.com/)
[![SmartThings](https://img.shields.io/badge/Samsung-SmartThings-1428A0?logo=samsung&logoColor=white)](https://www.smartthings.com/)
[![PuLP MILP](https://img.shields.io/badge/MILP-PuLP%20%2F%20CBC-FFB000)](https://coin-or.github.io/pulp/)
[![Model Context Protocol](https://img.shields.io/badge/MCP-75_tools-D97757)](https://modelcontextprotocol.io/)
[![Topic](https://img.shields.io/badge/topic-smart--home-3DDC84)](#)
[![Topic](https://img.shields.io/badge/topic-energy--optimization-22C55E)](#)
[![Topic](https://img.shields.io/badge/topic-solar--PV-FACC15)](#)

</div>

> Most home-battery controllers run hand-coded rules per appliance. Multi-vendor optimisation against half-hourly Agile prices, weather-dependent heat-pump demand, and DHW pre-heat windows is the sort of thing rules get wrong. So this is a real solver — `PuLP` over a 96-slot horizon — that minimises grid cost while respecting battery efficiency, heat-pump COP curves, scenario-robust peak export, and negative-price plunges. Every solve is snapshotted to SQLite so any past day's plan can be re-run under today's code.

---

## ✨ Highlights

- 🧮 **Real solver, not rules.** PuLP MILP over 96 half-hour slots with soft penalties for cycling, comfort slack, and inverter stress. CBC by default.
- 🌤️ **Per-hour PV calibration.** Three-tier resolver — `(hour, cloud-bucket)` table → per-hour-of-day → flat factor. Quartz nowcast preferred; Open-Meteo fallback. OCF-style today-aware adjuster on top.
- 🔋 **Scenario-robust peak export.** Pessimistic forecast must also export ≥ floor before a `peak_export` slot is committed to Fox V3 — kills the cold-night export trap.
- 🏃 **Event-driven MPC.** Re-solves fire on tier boundaries, forecast revisions, Octopus fetches, SoC drift, and import-overshoot detection. No fixed-time belt-and-braces.
- 🧺 **Appliance dispatch.** Washer / dryer / dishwasher start times picked by the LP given a deadline; Smart Control button on the unit IS the consent gate. **v12**: drops the appliance and re-solves once if its load makes the LP Infeasible.
- 🚿 **Soft shower-window tank floor (v12).** `tank ≥ 45 °C` on shower-window slots is a soft constraint with a heavy 50 p/K-slot penalty — heats as fast as physics allows, surfaces the unavoidable deficit as a quantified slack instead of returning Infeasible. Closes the residual-class Infeasibility surface that the 60-day audit identified.
- 🔁 **Replayable Infeasibles (v12).** When the LP can't solve, the inputs are still snapshotted (`lp_inputs_snapshot.lp_status='Infeasible'`); `lp_replay.replay_run()` can reload + reproduce any past Infeasible offline against any code version.
- 📋 **Closed-loop regression gate.** Every LP solve is a frozen replayable snapshot. `scripts/check_lp_regression.py --vs-ref=<ref> --mode=both` gives clean per-PR cost deltas; `--refresh-baseline` re-pins the frozen JSON when an accepted strategy shift improves the optimum.
- 🔌 **75-tool MCP surface.** Bearer-guarded HTTP transport. Claude / OpenClaw read state, request plan changes, replay past days, and explain dispatch decisions.
- 📲 **Direct Telegram notifications.** Optional bypass of the OpenClaw `/hooks/agent` LLM-shaping path; HEM POSTs straight to `api.telegram.org` when configured. Keeps free pings out of LLM loops.

---

## 🏗️ How it works

```
                ┌─────────────────────────────┐
  Octopus Agile ──→  half-hourly tariff       │
  Quartz / Open-Meteo ─→ PV nowcast + weather │
  Fox ESS       ──→  load, PV, SoC, schedule  │
  Daikin Onecta ──→  tank, indoor, outdoor    │     PuLP MILP
  SmartThings   ──→  appliance state          │ ──→ 24–48h plan ──→  Fox V3 schedule
                │                             │                      Daikin action_schedule
  Past data     ──→  PV calibration table     │                      Telegram / OpenClaw notify
                ──→  load profile             │
                ──→  Daikin physics priors    │
                └─────────────────────────────┘
                            ↑                                 ↓
                            │                            ┌────────┐
                            └─── snapshots ────────────  │ replay │  ← regression gate
                                 (closed-loop)           └────────┘
```

The full architecture is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). The short version:

1. A scheduler tick fetches the latest Agile rates + weather forecast.
2. Past-data layers (load profile, residual-load profile minus physics-Daikin, PV calibration, dispatch decisions) feed into `lp_inputs_snapshot`.
3. `solve_lp()` runs PuLP/CBC over the 96-slot horizon with soft objective penalties for cycling, comfort slack, and inverter stress.
4. A scenario LP (optimistic / nominal / pessimistic perturbations) filters `peak_export` slots that would lose money under cold-night conditions.
5. The plan is persisted (`lp_solution_snapshot`), uploaded to Fox V3, and emitted as `action_schedule` rows for Daikin.
6. The 2-minute heartbeat reconciles live hardware with the schedule and writes `execution_log`.
7. MPC re-solves trigger on tier boundaries, forecast revisions, Octopus fetches, SoC drift, and import overshoot.

For decisions about peak-export robustness, see [docs/DISPATCH_DECISIONS.md](docs/DISPATCH_DECISIONS.md). For the OpenClaw boundary contract, [docs/OPENCLAW_BOUNDARY.md](docs/OPENCLAW_BOUNDARY.md). For the live-ops runbook, [docs/RUNBOOK.md](docs/RUNBOOK.md).

---

## 📦 What it does

- **Octopus Agile fetch** every 30 min. Stores import + Outgoing Agile rates in SQLite for the next ~36 h.
- **Quartz forecast source** — preferred PV nowcast for the UK site. Auth via env, direct PV is snapshotted per fetch, and Open-Meteo remains the weather fallback/context source.
- **Open-Meteo forecast** — temperature, cloud cover, irradiance. Snapshotted per fetch for replay and used as fallback when Quartz is unavailable.
- **PV forecast calibration** — three-tier resolver: cloud-aware `(hour, cloud bucket)` table → per-hour-of-day table → flat factor. Quartz direct PV and irradiance-based PV both pass through the same site calibration chain. Today-aware OCF Quartz-style adjuster on top.
- **Load forecast accuracy evaluator** — per-slot MAE/RMSE/bias broken down by local hour, plus a daily Daikin physics check against Onecta-measured kWh. Captures the biases the LP hasn't yet learned.
- **MILP solver (PuLP)** — 96-slot horizon, soft penalties for cycling/comfort/inverter stress, scenario LP for peak-export robustness, twice-daily and tier-boundary MPC.
- **Fox ESS Scheduler V3** — single daily upload of the optimised charge/discharge windows. Heuristic fallback if the LP fails.
- **Daikin Onecta** — `action_schedule` rows that the heartbeat applies; LWT offset, tank target, weather regulation toggles. OAuth2 with auto-refresh.
- **SmartThings appliance dispatch** — washer/dryer/dishwasher start times picked by the LP given a deadline; physical Smart Control button is the consent gate.
- **Notifications** — direct Telegram Bot API (preferred when `TELEGRAM_BOT_TOKEN` is set), with the OpenClaw `/hooks/agent` LLM-shaping path as fallback. Twice-daily digest, plan-revision pings, negative-price alerts, appliance lifecycle.
- **75-tool MCP surface** — Fox, Daikin, Octopus, optimization, replay, dispatch decisions. Bearer-guarded HTTP transport.
- **Closed-loop replay + regression gate** — every LP solve is a frozen, replayable snapshot; `scripts/check_lp_regression.py --mode=both` blocks merges when aggregate cost is worse on comparable baseline dates.

---

## 🚀 Quick start (local sim box)

The local checkout is for development and replay against a copy of prod state — **it must never touch live hardware**.

```bash
git clone https://github.com/albinati/home-energy-manager.git
cd home-energy-manager
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

cp .env.example .env
# Edit .env — at minimum set OPENCLAW_READ_ONLY=true so nothing dials out.
# To switch PV nowcasting, set FORECAST_SOURCE=quartz and fill the Quartz auth vars.

pytest                                    # 1100+ tests, ~3 min on a laptop
python -m src.cli serve                   # FastAPI on :8000, MCP at /mcp
```

Web UI at `http://localhost:8000/`, OpenAPI docs at `/docs`, MCP transport at `/mcp` (bearer-guarded; token at `data/.openclaw-token`).

---

## 🐳 Production deployment

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

---

## 🔌 Hardware & integrations

| Vendor | What we use | Auth |
|---|---|---|
| **Octopus Energy** | Agile import + Outgoing Agile export rates, optional consumption backfill | API key (read-only) |
| **Quartz Solar** | PV nowcast source for the LP; direct site PV when configured | Auth0 bearer token via env |
| **Open-Meteo** | Hourly weather forecast and fallback PV context (temp, radiation, cloud cover) | None — public |
| **Fox ESS** | SoC, PV, load, grid, Scheduler V3 upload | Open API key + signature |
| **Daikin Altherma** | Status read + LWT/tank writes via Onecta | OAuth2 (auto-refresh) |
| **Samsung SmartThings** | Washer/dryer/dishwasher schedule | OAuth2 (auto-refresh) |
| **Telegram Bot API** | Direct user notifications (preferred path) | Bot token + chat id |
| **OpenClaw / Claude** | Reads state, requests plan changes, runs MCP tools | Bearer-guarded HTTP MCP |

---

## 🛠️ This installation

This is a working personal project running 24/7 on one UK site. It is **public so the architecture and accuracy work are open**, not because it is a turn-key product. Hardware specs, integrations, and tariff defaults are tuned to one installation:

- 4.5 kW PV array, Fox H1-5.0-E-G2 inverter, EP11 battery
- Daikin Altherma 3 H HT (passive mode in summer, active LWT modulation in heating season)
- G98 single-phase export limit 3.68 kW
- Octopus Agile import + Outgoing Agile export
- Heat pump runs on Daikin's own weather curve; LP shifts demand, doesn't override the curve

Everything is parameterised via `.env`. Adapting it to a different setup is feasible but undocumented — open a discussion if you want to try.

---

## 🗺️ Roadmap

**v12.0.0** (2026-05-20) shipped the LP residual-class Infeasibility fix stack — see [CHANGELOG.md](CHANGELOG.md) for the eight PRs and the −£1.09/14d honest-mode regression delta.

V11 epic (accuracy via past-data integration) — open stories:

| | Story | Status |
|---|---|---|
| ✅ | V11-A — Cloud-cover & full-input snapshot capture | shipped (#240) |
| ✅ | V11-E — Adaptive PV calibration trigger | shipped (#198) |
| 🟡 | V11-B — Quantile-based scenario perturbations | pending |
| 🟡 | V11-C — DHW draw learning (rolling 14-day prior) | pending |
| 🟡 | V11-D — Occupancy & variable-load inference | pending |

Other open work: [Daikin observation strategy](https://github.com/albinati/home-energy-manager/issues/267), [Daikin physics calibration via 2-hourly Onecta consumption](https://github.com/albinati/home-energy-manager/issues/238), [Travel-period aware load profile](https://github.com/albinati/home-energy-manager/issues/161), [Daikin tank reheat anomaly detection](https://github.com/albinati/home-energy-manager/issues/184).

The full backlog is on the [issues board](https://github.com/albinati/home-energy-manager/issues).

---

## 🤝 Contributing

Adding a feature, reproducing a bug, or porting to a different installation? See [CONTRIBUTING.md](CONTRIBUTING.md). Project conduct — [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md). Security issues — [SECURITY.md](SECURITY.md). Release history — [CHANGELOG.md](CHANGELOG.md).

The PR bar for LP-touching changes: `scripts/check_lp_regression.py --vs-ref=main --mode=both` must show non-regressive aggregate cost on the comparable baseline window. Individual moments can be worse; aggregate must be ≤ baseline + the configured threshold.

---

## 📜 License

[Apache 2.0](LICENSE). Attribution requirements live in [NOTICE](NOTICE).
