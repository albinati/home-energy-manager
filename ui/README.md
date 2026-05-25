# HEM SPA container

Standalone nginx container serving the Home Energy Manager web UI.
Decouples the frontend from the Python API container so each can iterate
independently. Originally shipped as Epic 13b; rebuilt as a Vite + Preact +
TypeScript SPA in the 2026-05-25 cutover.

## Layout

```
ui/
├── Dockerfile                  multi-stage: node:20-alpine build → nginx:1.27-alpine runtime
├── README.md                   this file
├── package.json                Preact + ECharts + Vite + TS
├── vite.config.ts              build config + dev proxy
├── tsconfig.json               strict TS
├── index.html                  Vite entry (loads /config.js then the bundle)
├── public/                     unprocessed assets (favicon, og image)
├── src/
│   ├── main.tsx                mounts <App/>
│   ├── app.tsx                 wouter router + shell
│   ├── routes/                 one file per SPA route
│   │   ├── landing.tsx         /         — savings story, "selling" page
│   │   ├── cockpit.tsx         /cockpit  — live power flow, SoC, dispatch
│   │   ├── forecast.tsx        /forecast — forecast vs actuals
│   │   └── settings.tsx        /settings — runtime settings editor
│   ├── components/             shared + per-page UI
│   ├── lib/                    api, types, polling, charts, formatting, toast
│   └── styles/                 design tokens + base/shell CSS
├── legacy/                     static HTML kept from the previous UI
│   ├── history.html            /history
│   ├── insights.html           /insights
│   ├── workbench.html          /workbench
│   ├── css/                    legacy stylesheets
│   └── js/                     legacy vanilla JS
├── conf/
│   └── nginx.conf.template     server config; ${HEM_API_URL} substituted at boot
└── ui-entrypoint.sh            generates /usr/share/nginx/html/config.js at boot
```

The SPA owns `/`, `/cockpit`, `/forecast`, `/settings`. The legacy HTML pages
remain at `/history`, `/insights`, `/workbench` and are full-page-reloaded (not
SPA-routed) from the top nav. A follow-up PR will rebuild those routes too.

## Stack

| | |
|---|---|
| Framework | Preact 10 (~5 KB) |
| Router | wouter-preact (~3 KB) |
| Charts | ECharts 5 (lazy-loadable chunk) |
| Build | Vite 5 + TypeScript 5 (strict) |
| State | `@preact/signals` for global toast queue; local `useState` elsewhere |
| Styling | CSS custom properties + plain CSS files per component |

No Tailwind, no Redux, no PWA, no service worker.

## Local development

```bash
cd ui
npm ci
# Drop a local config.js so the SPA has a bearer + apiBase:
cat > public/config.js <<EOF
window.__HEM_CONFIG__ = {
  apiBase: "/api/v1",
  bearer:  "<sim-box token>",
  buildSha: "dev"
};
EOF
# Point Vite's /api proxy at your HEM instance:
VITE_DEV_API_TARGET="http://sim-box-host:8000" npm run dev
```

Then open http://localhost:5173.

## Build + publish

CI handles it — every push to main that touches `ui/**` triggers
`.github/workflows/ui-publish.yml`, which pushes
`ghcr.io/albinati/home-energy-manager-ui:<sha>` (and the `main` tag).

To build locally:

```bash
docker build --build-arg BUILD_SHA=$(git rev-parse HEAD) -t hem-ui ./ui
```

To typecheck without building:

```bash
npm run typecheck
```

To produce the static bundle:

```bash
npm run build   # → ui/dist/
```

## Run

Required env vars at container start:

| Var | Purpose |
|---|---|
| `HEM_API_URL` | Upstream HEM, e.g. `http://hem:8000`. Substituted into the nginx `proxy_pass`. |
| `HEM_UI_TOKEN` *or* `HEM_UI_TOKEN_FILE` | Bearer token. The entrypoint writes it into `/usr/share/nginx/html/config.js` at boot — never baked into the image. `HEM_UI_TOKEN_FILE` points at a Docker secret mount (e.g. `/run/secrets/hem-ui-token`). |

```bash
docker run --rm -p 8080:80 \
  -e HEM_API_URL=http://hem:8000 \
  -e HEM_UI_TOKEN=$(cat /srv/hem/data/.hem-ui-token) \
  ghcr.io/albinati/home-energy-manager-ui:main
```

## Why coexist with the legacy pages?

The rebuild cut the four highest-impact pages (landing, cockpit, forecast,
settings) in one PR. Rebuilding the operator pages (history, insights,
workbench) needs more API plumbing — they keep working as vanilla HTML in
the meantime. nginx's `try_files $uri $uri.html` resolves them as static
HTML; only paths that miss every file fall back to the SPA's `index.html`.
