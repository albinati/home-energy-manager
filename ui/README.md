# HEM SPA container

Standalone nginx container serving the Home Energy Manager web UI.
Decouples the frontend from the Python API container so each can iterate
independently. Shipped by Epic 13b.

## Layout

```
ui/
├── Dockerfile                  nginx:alpine + envsubst for config
├── README.md                   this file
├── conf/
│   └── nginx.conf.template     server config; ${HEM_API_URL} substituted at boot
├── html/                       static HTML pages (one per route)
│   └── cockpit.html            placeholder until B4 lifts the real cockpit
├── src/
│   ├── css/                    CSS, served from /css/
│   └── js/
│       └── _api.js             shared fetch() wrapper (bearer + base URL)
└── ui-entrypoint.sh            generates /usr/share/nginx/html/config.js at boot
```

## Build + publish

CI handles it — every push to main that touches `ui/**` triggers
`.github/workflows/ui-publish.yml`, which pushes
`ghcr.io/albinati/home-energy-manager-ui:<sha>` (and the `main` tag).

To build locally:

```bash
docker build -t hem-ui ./ui
```

## Run

Two required env vars:

| Var | Purpose |
|---|---|
| `HEM_API_URL` | Upstream HEM (e.g. `http://hem:8000` in compose, `http://127.0.0.1:8000` on host network). The nginx config envsubsts this into the `/api/` reverse proxy. |
| `HEM_UI_TOKEN` | Bearer for `/api/v1/*`. Minted by HEM's lifespan at `/srv/hem/data/.hem-ui-token` (Epic 13b/B1). Written into the runtime `/config.js` by the entrypoint so the SPA can read it on boot. |

```bash
docker run --rm -p 8080:80 \
  -e HEM_API_URL=http://host.docker.internal:8000 \
  -e HEM_UI_TOKEN="$(cat data/.hem-ui-token)" \
  hem-ui
```

Open <http://localhost:8080/>.

## Story progression

- **B3** (this PR) — scaffold only. Placeholder cockpit page proves
  routing + bearer injection are wired.
- **B4** — lift the actual pages from `src/api/templates/` + JS from
  `src/api/static/js/`. Mechanical copy, per the inventory at
  [`docs/UI_API_INVENTORY.md`](../docs/UI_API_INVENTORY.md).
- **B5** — drop the inline templates / Jinja2 from the HEM container.
- **B6** — wire the `hem-ui` service into `/srv/hem/compose.yaml` and
  flip `HEM_UI_AUTH_REQUIRED=true` so the new bearer gate enforces.

Until B6 cuts over, the inline UI inside HEM keeps serving traffic —
this SPA container can be built + pulled in parallel without affecting
prod.
