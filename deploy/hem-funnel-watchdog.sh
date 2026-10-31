#!/usr/bin/env bash
# hem-funnel-watchdog: outside-in probe of the Tailscale Funnel public ingress.
# Restarts ONLY tailscaled (with a growing cooldown) and alerts via Telegram.
# Always exits 0 on handled paths so the systemd timer never goes "failed".
#
# Config comes from the environment only. systemd loads
# /etc/default/hem-funnel-watchdog via EnvironmentFile; for manual runs use
#   set -a; . /etc/default/hem-funnel-watchdog; set +a
# (this script deliberately does NOT source it, so the caller's env wins).
set -euo pipefail

FUNNEL_HOST="${FUNNEL_HOST:-openclaw-overbot.tail0dbf20.ts.net}"
FUNNEL_PORT="${FUNNEL_PORT:-8443}"
FUNNEL_PATH="${FUNNEL_PATH:-/api/v1/health}"
FUNNEL_RESOLVERS="${FUNNEL_RESOLVERS:-1.1.1.1 9.9.9.9}"
FUNNEL_TIMEOUT_S="${FUNNEL_TIMEOUT_S:-20}"
FUNNEL_FAIL_THRESHOLD="${FUNNEL_FAIL_THRESHOLD:-2}"
FUNNEL_RESTART_COOLDOWN_S="${FUNNEL_RESTART_COOLDOWN_S:-1800}"
FUNNEL_RESTART_COOLDOWN_MAX_S="${FUNNEL_RESTART_COOLDOWN_MAX_S:-14400}"
HEM_ENV_FILE="${HEM_ENV_FILE:-/srv/hem/.env}"
FUNNEL_DRY_RUN="${FUNNEL_DRY_RUN:-0}"

log() { echo "hem-funnel-watchdog: $*" >&2; }

# Validate numeric knobs; fall back to the default with a warning.
num_or_default() { # name default
  local v="${!1}"
  if ! [[ $v =~ ^[0-9]+$ ]]; then
    log "invalid $1='$v'; using default $2"
    printf -v "$1" '%s' "$2"
  fi
}
num_or_default FUNNEL_TIMEOUT_S 20
num_or_default FUNNEL_FAIL_THRESHOLD 2
num_or_default FUNNEL_RESTART_COOLDOWN_S 1800
num_or_default FUNNEL_RESTART_COOLDOWN_MAX_S 14400

# Dry-run must never touch the real state: throwaway dir unless the caller
# explicitly chose one.
if [ "$FUNNEL_DRY_RUN" = "1" ] && [ -z "${FUNNEL_STATE_DIR+x}" ]; then
  FUNNEL_STATE_DIR="$(mktemp -d)"
  log "DRY-RUN: using throwaway state dir $FUNNEL_STATE_DIR"
fi
FUNNEL_STATE_DIR="${FUNNEL_STATE_DIR:-/var/lib/hem-funnel-watchdog}"

mkdir -p "$FUNNEL_STATE_DIR"
exec 9>"$FUNNEL_STATE_DIR/lock"
if ! flock -n 9; then
  log "another run in progress; skipping"
  exit 0
fi

read_state() { # name default (plain text, e.g. alerted)
  if [ -s "$FUNNEL_STATE_DIR/$1" ]; then cat "$FUNNEL_STATE_DIR/$1"; else echo "$2"; fi
}
read_num() { # name default (corrupt/non-numeric content -> default)
  local v
  v="$(read_state "$1" "$2")"
  if [[ $v =~ ^[0-9]+$ ]]; then echo "$v"; else echo "$2"; fi
}
write_state() { printf '%s\n' "$2" >"$FUNNEL_STATE_DIR/$1"; }

# KEY -> value. Handles optional `export `, "..." / '...' quoting and a
# trailing ` # comment` on unquoted values. The file is never sourced.
env_value() {
  [ -r "$HEM_ENV_FILE" ] || return 0
  local line v
  line="$({ grep -E "^(export[[:space:]]+)?$1=" "$HEM_ENV_FILE" || true; } | tail -n1)"
  [ -n "$line" ] || return 0
  v="${line#*=}"
  v="${v#"${v%%[![:space:]]*}"}"
  if [[ $v =~ ^\"([^\"]*)\" ]]; then
    v="${BASH_REMATCH[1]}"
  elif [[ $v =~ ^\'([^\']*)\' ]]; then
    v="${BASH_REMATCH[1]}"
  else
    v="${v%%[[:space:]]#*}"
    v="${v%"${v##*[![:space:]]}"}"
  fi
  printf '%s' "$v"
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
  curl -sS -f -m 10 -o /dev/null -X POST "https://api.telegram.org/bot${token}/sendMessage" \
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
  local code rc=0 errf
  errf="$(mktemp)"
  code="$(curl -sS -m "$FUNNEL_TIMEOUT_S" \
    --resolve "${FUNNEL_HOST}:${FUNNEL_PORT}:$1" -o /dev/null -w '%{http_code}' \
    "https://${FUNNEL_HOST}:${FUNNEL_PORT}${FUNNEL_PATH}" 2>"$errf")" || rc=$?
  if [ "$rc" -ne 0 ] || [ -z "$code" ] || [ "$code" = "000" ]; then
    log "ingress $1 failed (curl rc=$rc: $(tr '\n' ' ' <"$errf"))"
    rm -f "$errf"
    return 1
  fi
  rm -f "$errf"
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
done

now="$(date +%s)"
fails="$(read_num fails 0)"

if [ "$alive" = "1" ]; then
  if [ -n "$(read_state alerted "")" ]; then
    telegram "Funnel voltou: ingresso público respondendo de novo."
  fi
  if [ "$fails" -gt 0 ]; then
    log "funnel recovered after $fails failure(s)"
  fi
  write_state fails 0
  write_state restarts 0
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
our_restart="$(read_num last_restart_epoch 0)"
last_restart="$our_restart"
unit_ts="$(systemctl show -p ActiveEnterTimestamp --value tailscaled 2>/dev/null || true)"
if [ -n "$unit_ts" ] && unit_epoch="$(date -d "$unit_ts" +%s 2>/dev/null)"; then
  if [ "$unit_epoch" -gt "$last_restart" ]; then
    last_restart="$unit_epoch"
  fi
fi

first_fail="$(read_num first_fail_epoch "$now")"
minutes=$(((now - first_fail) / 60))
restarts="$(read_num restarts 0)"

# Cooldown doubles per restart (30 -> 60 -> 120 -> 240 min), capped.
cooldown="$FUNNEL_RESTART_COOLDOWN_S"
i=1
while [ "$i" -lt "$restarts" ] && [ "$cooldown" -lt "$FUNNEL_RESTART_COOLDOWN_MAX_S" ]; do
  cooldown=$((cooldown * 2))
  i=$((i + 1))
done
if [ "$cooldown" -gt "$FUNNEL_RESTART_COOLDOWN_MAX_S" ]; then
  cooldown="$FUNNEL_RESTART_COOLDOWN_MAX_S"
fi

if [ $((now - last_restart)) -lt "$cooldown" ]; then
  log "tailscaled restarted recently; cooldown ${cooldown}s active, not restarting"
  if [ "$(read_state alerted "")" != "unresolved" ]; then
    if [ "$last_restart" -gt "$our_restart" ]; then
      telegram "Funnel público morto há ${minutes} min; tailscaled (re)iniciado há $(((now - last_restart) / 60)) min; aguardando cooldown."
      write_state alerted unresolved
    elif [ "$restarts" -lt 2 ]; then
      telegram "Funnel público morto há ${minutes} min; reinício do tailscaled não resolveu / aguardando cooldown."
      write_state alerted unresolved
    fi
  fi
  exit 0
fi

# Persist state and notify BEFORE the (possibly blocking) restart.
restarts=$((restarts + 1))
write_state restarts "$restarts"
write_state alerted restart
if [ "$FUNNEL_DRY_RUN" = "1" ]; then
  log "DRY-RUN: would run systemctl restart tailscaled"
else
  write_state last_restart_epoch "$now"
fi
telegram "Funnel público morto (${fails} falhas); reiniciando tailscaled (reinício nº ${restarts})."
if [ "$FUNNEL_DRY_RUN" != "1" ]; then
  rc=0
  systemctl restart tailscaled || rc=$?
  if [ "$rc" -ne 0 ]; then
    log "systemctl restart tailscaled failed (rc=$rc)"
    telegram "restart FALHOU (rc=${rc})"
  fi
fi
exit 0
