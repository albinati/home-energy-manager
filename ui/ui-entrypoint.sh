#!/bin/sh
# Drop-in entrypoint (nginx's image runs /docker-entrypoint.d/*.sh before
# launching nginx itself). We use this hook for two things:
#
# 1. Generate /usr/share/nginx/html/config.js so the SPA reads its API base
#    (and build SHA) from window.__HEM_CONFIG__ on boot. The bearer field is
#    always null — no token is shipped to the browser (config.js is readable
#    by anyone on the public funnel); admin auth uses a separate runtime token.
#
# 2. Validate that HEM_API_URL is set (the nginx template envsubst step
#    needs it). If not set we exit 1 so the container fails fast rather
#    than serving a broken /api proxy.
set -eu

: "${HEM_API_URL:?HEM_API_URL must be set (e.g. http://hem:8000)}"

CONFIG_PATH=/usr/share/nginx/html/config.js

# The SPA bearer is intentionally ALWAYS null. config.js is world-readable on
# the public Tailscale funnel, so anything shipped here leaks to any visitor.
# The current role model (ApiV1RoleAuth) makes viewer reads open with NO token
# and gates every admin action behind a SEPARATE admin token the user pastes at
# runtime (held in localStorage, never baked here) — see ui/src/lib/api.ts,
# which already ignores this field. So baking HEM_UI_TOKEN into the browser
# bought nothing and was a standing footgun. Ship null.
# (NB: HEM_UI_TOKEN is NOT accepted server-side. The middleware actually mounted
# is ApiV1RoleAuth (src/api/main.py); ApiV1BearerAuth is dead code and is never
# installed. Read routes are viewer-open; writes need HEM_ADMIN_TOKEN.)
BEARER_LITERAL="null"

# Write null (not "unknown") when the env is absent/placeholder so the SPA's
# buildSha() falls through to the Vite-baked __BUILD_SHA__ — a truthy
# "unknown" here used to mask the perfectly good baked value.
if [ -n "${BUILD_SHA:-}" ] && [ "${BUILD_SHA}" != "unknown" ]; then
  SHA_LITERAL="\"$BUILD_SHA\""
else
  SHA_LITERAL="null"
fi

cat > "$CONFIG_PATH" <<EOF
// Generated at container start by ui-entrypoint.sh — DO NOT EDIT.
// Cached:no-store via nginx config.
window.__HEM_CONFIG__ = {
  apiBase: "/api/v1",
  bearer:  $BEARER_LITERAL,
  buildSha: $SHA_LITERAL
};
EOF

# Admin writes use a separate token the user pastes at runtime (stored in the
# browser's localStorage, validated against GET /whoami) — nothing to provision
# here. Viewer reads are open, so the SPA is fully functional read-only on boot.
