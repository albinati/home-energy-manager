#!/bin/sh
# Drop-in entrypoint (nginx's image runs /docker-entrypoint.d/*.sh before
# launching nginx itself). We use this hook for two things:
#
# 1. Generate /usr/share/nginx/html/config.js so the SPA reads its bearer
#    token + API base from window.__HEM_CONFIG__ on boot. The token is
#    NEVER baked into the image — it comes in via the runtime env at
#    container start, lifted from /srv/hem/data/.hem-ui-token by the
#    compose deploy.
#
# 2. Validate that HEM_API_URL is set (the nginx template envsubst step
#    needs it). If not set we exit 1 so the container fails fast rather
#    than serving a broken /api proxy.
set -eu

: "${HEM_API_URL:?HEM_API_URL must be set (e.g. http://hem:8000)}"

CONFIG_PATH=/usr/share/nginx/html/config.js
TOKEN="${HEM_UI_TOKEN:-}"

# Token-file fallback — prod compose mounts /srv/hem/data/.hem-ui-token at
# /run/secrets/hem-ui-token (read-only) so the SPA container reads what
# HEM's lifespan minted. Env still wins (handy for local dev `docker run`).
if [ -z "$TOKEN" ] && [ -n "${HEM_UI_TOKEN_FILE:-}" ] && [ -r "$HEM_UI_TOKEN_FILE" ]; then
  TOKEN="$(tr -d '\n\r ' < "$HEM_UI_TOKEN_FILE")"
fi

# Render bearer as a JSON literal — either "<token>" (string) or null. The
# previous ``${TOKEN:+\"$TOKEN\"}${TOKEN:-null}`` trick was broken in POSIX
# sh: the :- expansion fires whenever TOKEN is empty OR unset, so when TOKEN
# was set the output was `"TOKEN"TOKEN` (string + bare token). That produced
# a JS syntax error and the SPA fell back to ``window.__HEM_CONFIG__``
# undefined, killing every authenticated /api/v1 call once the gate flag
# flipped.
if [ -n "$TOKEN" ]; then
  BEARER_LITERAL="\"$TOKEN\""
else
  BEARER_LITERAL="null"
fi

cat > "$CONFIG_PATH" <<EOF
// Generated at container start by ui-entrypoint.sh — DO NOT EDIT.
// Cached:no-store via nginx config.
window.__HEM_CONFIG__ = {
  apiBase: "/api/v1",
  bearer:  $BEARER_LITERAL,
  buildSha: "${BUILD_SHA:-unknown}"
};
EOF

# Without HEM_UI_TOKEN the SPA still loads but write actions fail with 401
# (once HEM_UI_AUTH_REQUIRED=true on the API side). Surface the absence in
# the container log so it's obvious in journalctl.
if [ -z "$TOKEN" ]; then
  echo "ui-entrypoint: WARNING — HEM_UI_TOKEN not set; SPA will only succeed on read paths until token is provided" >&2
fi
