# HEM SPA container — multi-stage:
#   1. Build the Vite + Preact SPA inside node:20-alpine.
#   2. Pack the static output into nginx:1.27-alpine alongside the legacy/
#      pages that are still hand-written.
#
# Built + published by .github/workflows/ui-publish.yml on every push to main:
#   ghcr.io/albinati/home-energy-manager-ui:<sha>
#   ghcr.io/albinati/home-energy-manager-ui:main
#
# Runtime configuration is via two env vars consumed by ui-entrypoint.sh:
#   HEM_API_URL  — upstream HEM, e.g. http://hem:8000 (compose) or
#                  http://127.0.0.1:8000 (host network).
#   HEM_UI_TOKEN — bearer minted by HEM's lifespan at /srv/hem/data/.hem-ui-token,
#                  read into the container as a Docker secret or env var. The
#                  entrypoint substitutes the token into a runtime config file
#                  the SPA reads on boot (so the token never gets baked into
#                  the image).

# --- Stage 1: build the SPA ------------------------------------------------
FROM node:20-alpine AS build
WORKDIR /app

# Install deps first (cache friendly).
COPY package.json package-lock.json* ./
RUN npm ci --no-audit --no-fund

# Source.
COPY tsconfig.json vite.config.ts vite-env.d.ts index.html ./
COPY src ./src
COPY public ./public

# BUILD_SHA bakes into the bundle so the footer can show it.
ARG BUILD_SHA=unknown
ENV VITE_BUILD_SHA=$BUILD_SHA

RUN npm run build

# --- Stage 2: runtime nginx -------------------------------------------------
FROM nginx:1.27-alpine

# gettext provides `envsubst` for the entrypoint
RUN apk add --no-cache gettext

# Built SPA → nginx html root
COPY --from=build /app/dist/  /usr/share/nginx/html/

# Legacy pages — copied verbatim, served alongside the SPA. The new SPA owns
# /, /cockpit, /forecast, /settings; nginx serves these legacy HTML files at
# /history, /insights, /workbench. See conf/nginx.conf.template for the
# routing rules.
COPY legacy/                  /usr/share/nginx/html/

# nginx config — single template; entrypoint envsubsts HEM_API_URL into it
COPY conf/nginx.conf.template  /etc/nginx/templates/default.conf.template

# Runtime config — entrypoint writes /usr/share/nginx/html/config.js with
# the bearer token + API base so the SPA can read window.__HEM_CONFIG__.
COPY ui-entrypoint.sh /docker-entrypoint.d/40-hem-config.sh
RUN chmod +x /docker-entrypoint.d/40-hem-config.sh

# Health endpoint — nginx serves /healthz with a static response (no HEM round-trip)
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD wget -q -O /dev/null http://127.0.0.1:80/healthz || exit 1

EXPOSE 80
