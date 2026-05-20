# HEM SPA container — nginx serving the static SPA + reverse-proxying /api → HEM.
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
FROM nginx:1.27-alpine

# gettext provides `envsubst` for the entrypoint
RUN apk add --no-cache gettext

# Static assets — html/, src/js/, src/css/ map to /usr/share/nginx/html/
COPY html/        /usr/share/nginx/html/
COPY src/js/      /usr/share/nginx/html/js/
COPY src/css/     /usr/share/nginx/html/css/

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
