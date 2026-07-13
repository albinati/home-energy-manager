# UI ↔ API inventory (Epic 13b / Story B2)

> **SUPERSEDED — ARCHIVED 2026-07-13. Describes a UI that no longer exists.**
>
> This was the migration-planning artifact for Epic 13b. **The migration is
> done.** Story B5 removed **all** HTML from the API: there is no Jinja2, no
> `src/api/templates/`, no `static/`, no `_layout_context()`, no `/legacy` route
> — the API serves JSON only. The six Jinja-served pages catalogued below are
> gone.
>
> The live UI is the **Preact + TypeScript + Vite SPA** in `ui/`, served by the
> `hem-ui` nginx container, with **four** routes: `/`, `/insights`, `/report`,
> `/settings` (`ui/src/routes/`). For the current API surface, use the OpenAPI
> docs at `:8000/docs`. Kept only as a record of what the pre-SPA UI consumed.

Catalogues every `/api/v1/*` endpoint each web UI page consumes, plus every
server-side Jinja2 template variable the page depends on. Output of
Story B2 (#357) — feeds Story B3 (SPA container scaffold) and Story B4
(page-by-page migration) so the team knows up front whether each page
can be migrated as-is or needs new JSON endpoints first.

**Headline finding:** the six live pages (cockpit / history / forecast /
insights / workbench / settings) are *already* JSON-driven. They use the
Jinja2 layer only for shared chrome (page-title, the mode-switcher
partial, `static_v()` cache-busting). The 7th template,
`dashboard_legacy.html` (the v9 fallback), is the only Jinja-rendered
data page — and it's slated for removal in Story B5 (decommission).

**Implication:** B3 (SPA scaffold) does **not** need new endpoints
before the migration starts. Drop the legacy page in B5 alongside the
Jinja2 dependency, port the six live pages with their existing JSON
calls, and the migration is purely a frontend lift.

---

## Shared Jinja context (used by every page via `_layout.html`)

All six live pages render through `_layout.html` which receives these
three variables from `_layout_context()` in `src/api/main.py`:

| Variable | Source | SPA equivalent |
|---|---|---|
| `active_page` | route handler kwarg | route name in the SPA router |
| `daikin_control_mode` | `config.DAIKIN_CONTROL_MODE` | `/api/v1/settings` already exposes it |
| `require_simulation_id` | `config.REQUIRE_SIMULATION_ID` | same — `/api/v1/settings` |

Plus the `static_v()` helper for cache-busting query strings — replaced
by vite/esbuild fingerprinting (or simply nginx's `etag` since the SPA
container serves static via nginx).

---

## Per-page inventory

### `cockpit.html` — Served at `/`

| Field | Detail |
|---|---|
| Route handler | `src/api/main.py:378` |
| JS API calls | `GET /api/v1/cockpit/now`<br>`GET /api/v1/agile/today`<br>`GET /api/v1/optimization/plan`<br>`GET /api/v1/load/breakdown`<br>`POST /api/v1/optimization/refresh`<br>`GET /api/v1/foxess/status?refresh=true`<br>`GET /api/v1/daikin/status?refresh=true`<br>`GET /api/v1/recent-triggers?limit=6` |
| Jinja vars (beyond shared) | none |
| **Gap** | **none** — fully JSON-driven |

### `history.html` — Served at `/history`

| Field | Detail |
|---|---|
| Route handler | `src/api/main.py:386` |
| JS API calls | `GET /api/v1/cockpit/at?when={iso}`<br>`GET /api/v1/attribution/day?date={date}` |
| Jinja vars (beyond shared) | none |
| **Gap** | **none** |

### `forecast.html` — Served at `/forecast`

| Field | Detail |
|---|---|
| Route handler | `src/api/main.py:398` |
| JS API calls | `GET /api/v1/optimization/inputs` |
| Jinja vars (beyond shared) | none |
| **Gap** | **none** — config snapshot + slot tables built client-side |

### `insights.html` — Served at `/insights`

| Field | Detail |
|---|---|
| Route handler | `src/api/main.py:410` |
| JS API calls | `GET /api/v1/energy/period?{period_params}`<br>`GET /api/v1/agile/day?date={date}`<br>`GET /api/v1/execution/today?date={date}`<br>`GET /api/v1/optimization/plan`<br>`GET /api/v1/patterns/hourly?{qs}`<br>`GET /api/v1/patterns/dow?{qs}`<br>`GET /api/v1/patterns/price-distribution?{qs}`<br>`GET /api/v1/patterns/pv-calibration?{qs}`<br>`POST /api/v1/tariffs/compare` (body `{months_back: 3}`) |
| Jinja vars (beyond shared) | none |
| **Gap** | **none** — period nav + tariff comparison + pattern aggregates all client-side |

### `workbench.html` — Served at `/workbench`

| Field | Detail |
|---|---|
| Route handler | `src/api/main.py:424` |
| JS API calls | `POST /api/v1/workbench/simulate` (body `{overrides}`)<br>`GET /api/v1/optimization/plan`<br>`GET /api/v1/workbench/schema`<br>`GET /api/v1/workbench/profiles`<br>`GET /api/v1/workbench/profiles/{name}`<br>`POST /api/v1/workbench/profiles/{name}`<br>`DELETE /api/v1/workbench/profiles/{name}` |
| Jinja vars (beyond shared) | none |
| **Gap** | **none** — editor form built client-side from `/workbench/schema` |

### `settings.html` — Served at `/settings`

| Field | Detail |
|---|---|
| Route handler | `src/api/main.py:431` |
| JS API calls | `GET /api/v1/settings`<br>`PUT /api/v1/settings/{key}` (+ simulate variant via wrapAction handler) |
| Jinja vars (beyond shared) | none |
| **Gap** | **none** — all comfort/strategy/schedule values managed through `/api/v1/settings/*` |

### `dashboard_legacy.html` — Served at `/legacy`

| Field | Detail |
|---|---|
| Route handler | `src/api/main.py:438` |
| JS API calls | none (fully server-side rendered) |
| Jinja vars (beyond shared) | `daikin.*` dict (device_id, device_name, is_on, mode, room_temp, target_temp, outdoor_temp, lwt, lwt_offset, tank_temp, tank_target, weather_regulation, weather_regulation_settable, lwt_offset_range, room_temp_range, tank_temp_range, cache_stale)<br>`foxess.*` dict (soc, solar_power, grid_power, battery_power, load_power, work_mode, updated_at, refresh_count_24h, refresh_limit_24h)<br>`daikin_error`, `foxess_error` (error strings or None) |
| **Gap** | the only Jinja-only page in the codebase. The data IS already available as JSON at `GET /api/v1/daikin/status` + `GET /api/v1/foxess/status` — the legacy template just doesn't call them. **Action:** drop this template in Story B5 (decommission) instead of porting it; v9 fallback is no longer needed. |

---

## Decision: no follow-up endpoints required for B3/B4

Only `dashboard_legacy.html` carries Jinja-dependent data, and the
equivalent JSON endpoints exist already. **B3 can scaffold + B4 can
migrate the six live pages as a pure frontend lift**, without holding
the line on a parallel API-endpoint workstream. B5 deletes
`dashboard_legacy.html` and the Jinja2 dependency together.

## What B4's migration touches per page

For each live page the porting work is:

1. Lift the `.html` body out of `_layout.html` inheritance into a
   standalone HTML page (the SPA container already provides chrome).
2. Lift the page's JS from `src/api/static/js/` into the SPA's `src/`.
3. Replace the `static_v()` cache-busting query string with the SPA
   build's hashed filenames.
4. Wire the new `Authorization: Bearer ${HEM_UI_TOKEN}` header into
   every `fetch(...)` call (nginx in the SPA container envsubsts the
   token at boot, see Story B6).
5. Verify all `/api/v1/*` paths still resolve (`fetch` works the same
   against the proxied origin).

No backend code changes, no new endpoints. The bearer auth gate
(`HEM_UI_AUTH_REQUIRED`, currently `false`) flips to `true` on B6
cutover after the SPA container is the only consumer.
