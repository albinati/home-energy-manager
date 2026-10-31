#!/usr/bin/env bash
# hem-funnel-watchdog: outside-in probe of the Tailscale Funnel public ingress.
# Restarts ONLY tailscaled (with a cooldown) and alerts via Telegram.
# Always exits 0 on handled paths so the systemd timer never goes "failed".
set -euo pipefail

# Optional overrides (also supplied via the unit's EnvironmentFile).
if [ -r /etc/default/hem-funnel-watchdog ]; then
  # shellcheck disable=SC1091
  . /etc/default/hem-funnel-watchdog
fi

FUNNEL_HOST="${FUNNEL_HOST:-openclaw-overbot.tail0dbf20.ts.net}"
FUNNEL_PORT="${FUNNEL_PORT:-8443}"
FUNNEL_PATH="${FUNNEL_PATH:-/api/v1/health}"
FUNNEL_RESOLVERS="${FUNNEL_RESOLVERS:-1.1.1.1 9.9.9.9}"
FUNNEL_TIMEOUT_S="${FUNNEL_TIMEOUT_S:-20}"
FUNNEL_FAIL_THRESHOLD="${FUNNEL_FAIL_THRESHOLD:-2}"
FUNNEL_RESTART_COOLDOWN_S="${FUNNEL_RESTART_COOLDOWN_S:-1800}"
FUNNEL_STATE_DIR="${FUNNEL_STATE_DIR:-/var/lib/hem-funnel-watchdog}"
HEM_ENV_FILE="${HEM_ENV_FILE:-/srv/hem/.env}"
FUNNEL_DRY_RUN="${FUNNEL_DRY_RUN:-0}"

log() { echo "hem-funnel-watchdog: $*" >&2; }

mkdir -p "$FUNNEL_STATE_DIR"
exec 9>"$FUNNEL_STATE_DIR/lock"
if ! flock -n 9; then
  log "another run in progress; skipping"
  exit 0
fi

read_state() { # name default
  if [ -s "$FUNNEL_STATE_DIR/$1" ]; then cat "$FUNNEL_STATE_DIR/$1"; else echo "$2"; fi
}
write_state() { printf '%s\n' "$2" >"$FUNNEL_STATE_DIR/$1"; }

env_value() { # KEY -> value without surrounding quotes (file is never sourced)
  [ -r "$HEM_ENV_FILE" ] || return 0
  { grep -E "^$1=" "$HEM_ENV_FILE" || true; } | tail -n1 | cut -d= -f2- |
    sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\\(.*\\)'\$/\\1/"
}

telegram() { # text
  local text="HEM host: $1"
  if [ "$FUNNEL_DRY_RUN" = "1" ]; then
    log "DRY-RUN telegram: $text"
    return 0
  fi
  local token chat
  token="$(env_value TELEGRAM_BOT_TOKEN)"
  chat="$(env_value TELEGRAM_CHAT_ID)"
  if [ -z "$token" ] || [ -z "$chat" ]; then
    log "telegram credentials missing; not sending: $text"
    return 0
  fi
  curl -s -m 10 -o /dev/null -X POST "https://api.telegram.org/bot${token}/sendMessage" \
    --data-urlencode "chat_id=${chat}" \
    --data-urlencode "text=${text}" \
    --data-urlencode "disable_notification=false" ||
    log "telegram send failed"
  return 0
}

# Resolve via PUBLIC resolvers only (MagicDNS would hit the tailnet, proving nothing).
resolve_ips() {
  local r out
  for r in $FUNNEL_RESOLVERS; do
    out="$(dig +short +time=5 +tries=1 "@$r" "$FUNNEL_HOST" A 2>/dev/null |
      grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' | head -n 2 || true)"
    if [ -n "$out" ]; then
      echo "$out"
      return 0
    fi
  done
  return 1
}

probe_ip() { # ip -> 0 when TLS+HTTP completed with any status
  local code
  code="$(curl -s -m "$FUNNEL_TIMEOUT_S" \
    --resolve "${FUNNEL_HOST}:${FUNNEL_PORT}:$1" -o /dev/null -w '%{http_code}' \
    "https://${FUNNEL_HOST}:${FUNNEL_PORT}${FUNNEL_PATH}" 2>/dev/null)" || return 1
  [ -n "$code" ] && [ "$code" != "000" ]
}

if ! ips="$(resolve_ips)"; then
  log "all public resolvers failed; not counting a failure"
  exit 0
fi

alive=0
for ip in $ips; do
  if probe_ip "$ip"; then
    alive=1
    break
  fi
  log "ingress $ip did not answer"
done

now="$(date +%s)"
fails="$(read_state fails 0)"

if [ "$alive" = "1" ]; then
  if [ -n "$(read_state alerted "")" ]; then
    telegram "Funnel voltou: ingresso público respondendo de novo."
  fi
  if [ "$fails" -gt 0 ]; then
    log "funnel recovered after $fails failure(s)"
  fi
  write_state fails 0
  rm -f "$FUNNEL_STATE_DIR/alerted" "$FUNNEL_STATE_DIR/first_fail_epoch"
  exit 0
fi

fails=$((fails + 1))
write_state fails "$fails"
if [ ! -s "$FUNNEL_STATE_DIR/first_fail_epoch" ]; then
  write_state first_fail_epoch "$now"
fi
log "public funnel probe failed ($fails/$FUNNEL_FAIL_THRESHOLD)"

if [ "$fails" -lt "$FUNNEL_FAIL_THRESHOLD" ]; then
  exit 0
fi

# Last tailscaled (re)start: ours, or the unit's own ActiveEnterTimestamp.
last_restart="$(read_state last_restart_epoch 0)"
unit_ts="$(systemctl show -p ActiveEnterTimestamp --value tailscaled 2>/dev/null || true)"
if [ -n "$unit_ts" ] && unit_epoch="$(date -d "$unit_ts" +%s 2>/dev/null)"; then
  if [ "$unit_epoch" -gt "$last_restart" ]; then
    last_restart="$unit_epoch"
  fi
fi

first_fail="$(read_state first_fail_epoch "$now")"
minutes=$(((now - first_fail) / 60))

if [ $((now - last_restart)) -lt "$FUNNEL_RESTART_COOLDOWN_S" ]; then
  log "tailscaled restarted recently; cooldown active, not restarting"
  if [ "$(read_state alerted "")" != "unresolved" ]; then
    telegram "Funnel público morto há ${minutes} min; reinício do tailscaled não resolveu / aguardando cooldown."
    write_state alerted unresolved
  fi
  exit 0
fi

if [ "$FUNNEL_DRY_RUN" = "1" ]; then
  log "DRY-RUN: would run systemctl restart tailscaled"
else
  systemctl restart tailscaled || log "systemctl restart tailscaled failed"
  write_state last_restart_epoch "$now"
fi
write_state alerted restart
telegram "Funnel público morto (${fails} falhas); tailscaled reiniciado."
exit 0
