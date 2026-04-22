# Architecture Decision Records — Index

Each ADR captures one decision, its context, and its trade-offs. ADRs are
**append-only**: a later ADR supersedes or extends an earlier one rather than
editing the original. Read in number order for historical arc, or jump to
whichever subsystem you're touching.

| # | Title | Status | Date |
|---|---|---|---|
| [001](ADR-001-api-quota-cache-layer.md) | API quota & cache layer (Daikin / Fox ESS) | Implemented, extended by 002 + 004 | 2026-04-19 |
| [002](ADR-002-daikin-quota-integrity-hardening.md) | Daikin quota-integrity hardening (transport-layer accounting + user-override acceptance) | Implemented | 2026-04-21 |
| [003](ADR-003-mcp-boundary-enforcement.md) | MCP boundary enforcement (audit + simulate_plan + singleton lock) | Implemented | 2026-04-22 |
| [004](ADR-004-daikin-physics-estimator.md) | Daikin physics state estimator as 429 fallback | Implemented | 2026-04-22 |
| [005](ADR-005-fox-scheduler-idempotency.md) | Fox ESS Scheduler V3 idempotency guard | Implemented | 2026-04-22 |
| [006](ADR-006-runtime-tunable-settings.md) | Runtime-tunable settings (no-restart tuning) | Implemented | 2026-04-22 |

## Authoring a new ADR

1. Copy the structure from a recent one (ADR-006 is the shortest current example).
2. Next free number, descriptive slug in the filename.
3. Add the row to this index.
4. If the ADR supersedes or extends an existing decision, cross-link both ways —
   the older ADR gets a pointer in its "Supersedes / extended by" section.
5. Keep it short — 60-150 lines is the sweet spot. Long context goes in the
   relevant subsystem doc; the ADR is the decision and the trade-offs.
