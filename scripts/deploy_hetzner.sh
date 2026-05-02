#!/usr/bin/env bash
# deploy_hetzner.sh — deploy/upgrade for the Hetzner server (native systemd, no Docker).
#
# The service runs as:  /root/home-energy-manager/.venv/bin/python -m src.cli serve
# Managed by systemd unit: home-energy-manager.service
#
# Usage (run on the Hetzner server):
#   ./scripts/deploy_hetzner.sh                        # git pull + reinstall deps + restart
#   ./scripts/deploy_hetzner.sh --backup               # backup DB first, then deploy
#   ./scripts/deploy_hetzner.sh --backup-only          # backup only, no deploy
#   ./scripts/deploy_hetzner.sh --backup --no-restart  # pull + deps only, skip restart
#
# Tailscale topology:
#   Hetzner server : <hem-host>.ts.net  (100.x.y.z)
#   Local machine  : <workstation>.ts.net      (100.x.y.z, WSL/Linux)
#   SSH to Hetzner : ssh root@<hem-host>.ts.net  (no keys needed)
#
# Backup (off-server via Tailscale — saves Hetzner disk space):
#   Already set in Hetzner ~/.bashrc:
#     LOCAL_BACKUP_DEST=root@<workstation>.ts.net:/root/em-backups
#   To activate: enable SSH on <workstation> once (choose one):
#     Option A — Tailscale SSH:  sudo tailscale set --ssh
#     Option B — OpenSSH:        sudo apt install openssh-server && sudo service ssh start
#   If not set, backup stays locally (./backups/) — only 3 copies kept to protect disk.
#
# Requirements:
#   - Python venv at .venv/  (re-created if missing)
#   - systemd unit home-energy-manager.service  (created by this script if missing)
#   - sqlite3 CLI  (apt install sqlite3)
#   - Tailscale + LOCAL_BACKUP_DEST for off-server backups

set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT_DIR="$(pwd)"
VENV="$PROJECT_DIR/.venv"
DB_PATH="${DB_PATH:-$PROJECT_DIR/energy_state.db}"
SYSTEMD_UNIT="home-energy-manager"
BACKUP_DIR="$PROJECT_DIR/backups"
LOCAL_BACKUP_DEST="${LOCAL_BACKUP_DEST:-}"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${GREEN}[deploy]${NC} $*"; }
warn()  { echo -e "${YELLOW}[deploy]${NC} $*"; }
error() { echo -e "${RED}[deploy]${NC} $*" >&2; exit 1; }
note()  { echo -e "${CYAN}[deploy]${NC} $*"; }

BACKUP=false
BACKUP_ONLY=false
NO_RESTART=false
for arg in "$@"; do
  case "$arg" in
    --backup)       BACKUP=true ;;
    --backup-only)  BACKUP=true; BACKUP_ONLY=true ;;
    --no-restart)   NO_RESTART=true ;;
    *)              warn "Unknown arg: $arg" ;;
  esac
done

# ---------------------------------------------------------------------------
# Auto-detect LOCAL_BACKUP_DEST via Tailscale peers (if not already set)
# ---------------------------------------------------------------------------
if $BACKUP && [ -z "$LOCAL_BACKUP_DEST" ] && command -v tailscale &>/dev/null; then
  TS_PEERS=$(tailscale status --json 2>/dev/null | python3 -c "
import json, sys
d = json.load(sys.stdin)
for p in d.get('Peer', {}).values():
    name = p.get('DNSName', '').rstrip('.')
    if name and p.get('OS', '') in ('windows', 'linux', 'darwin'):
        print(name)
" 2>/dev/null || true)
  if [ -n "$TS_PEERS" ]; then
    note "Tailscale peers found — set LOCAL_BACKUP_DEST to one of:"
    while IFS= read -r peer; do
      note "  export LOCAL_BACKUP_DEST='root@${peer}:/root/em-backups'"
    done <<< "$TS_PEERS"
    echo ""
  fi
fi

# ---------------------------------------------------------------------------
# 1. Backup DB (WAL-safe sqlite3 .backup, then rsync off-server)
# ---------------------------------------------------------------------------
if $BACKUP; then
  mkdir -p "$BACKUP_DIR"
  TS=$(date -u +%Y%m%dT%H%M%SZ)
  BACKUP_FILE="$BACKUP_DIR/energy_state_${TS}.sqlite"

  if [ -f "$DB_PATH" ]; then
    info "Backing up DB: $DB_PATH → $BACKUP_FILE"
    # sqlite3 .backup is WAL-consistent even while the service is running
    sqlite3 "$DB_PATH" ".backup $BACKUP_FILE" 2>/dev/null || cp "$DB_PATH" "$BACKUP_FILE"
    SIZE=$(du -sh "$BACKUP_FILE" | cut -f1)
    info "Backup size: $SIZE"
    if command -v sqlite3 &>/dev/null; then
      INTEGRITY=$(sqlite3 "$BACKUP_FILE" "PRAGMA integrity_check;" 2>&1 || true)
      [[ "$INTEGRITY" == "ok" ]] && info "DB integrity: OK" || warn "Integrity: $INTEGRITY"
    fi

    if [ -n "$LOCAL_BACKUP_DEST" ]; then
      info "Transferring via Tailscale → $LOCAL_BACKUP_DEST ..."
      if rsync -az --progress "$BACKUP_FILE" "$LOCAL_BACKUP_DEST/"; then
        info "Transfer OK — removing local copy to save disk space"
        rm -f "$BACKUP_FILE"
      else
        warn "rsync failed — keeping local copy (check Tailscale connection)"
      fi
    else
      warn "LOCAL_BACKUP_DEST not set — backup stays on Hetzner (3-copy limit)."
      warn "Run ./scripts/setup_tailscale_backup.sh to configure off-server backup."
      ls -t "$BACKUP_DIR"/*.sqlite 2>/dev/null | tail -n +4 | xargs rm -f || true
    fi
  else
    warn "DB not found at $DB_PATH — skipping backup (first deploy?)"
  fi
fi

$BACKUP_ONLY && { info "Backup-only — done."; exit 0; }

# ---------------------------------------------------------------------------
# 2. Pull latest code
# ---------------------------------------------------------------------------
info "Pulling latest code..."
git pull origin main

# ---------------------------------------------------------------------------
# 3. Create/update Python venv and install dependencies
# ---------------------------------------------------------------------------
if [ ! -f "$VENV/bin/python" ]; then
  info "Creating virtual environment at $VENV ..."
  python3 -m venv "$VENV"
fi
info "Installing/upgrading dependencies..."
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -r requirements.txt

# ---------------------------------------------------------------------------
# 4. Ensure systemd unit exists (creates it if missing)
# ---------------------------------------------------------------------------
UNIT_FILE="/etc/systemd/system/${SYSTEMD_UNIT}.service"
if [ ! -f "$UNIT_FILE" ]; then
  info "Creating systemd unit: $UNIT_FILE"
  # Detect the user running this script (root or a dedicated user)
  RUN_USER="${SUDO_USER:-$(whoami)}"
  cat > "$UNIT_FILE" <<UNIT
[Unit]
Description=Home Energy Manager
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$PROJECT_DIR
EnvironmentFile=$PROJECT_DIR/.env
ExecStart=$VENV/bin/python -m src.cli serve
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable "$SYSTEMD_UNIT"
  info "Systemd unit created and enabled."
fi

# ---------------------------------------------------------------------------
# 5. DB migration (idempotent)
# ---------------------------------------------------------------------------
info "Applying DB migrations..."
"$VENV/bin/python" -c "from src.db import init_db; init_db(); print('Migration OK')"

# ---------------------------------------------------------------------------
# 6. Restart service
# ---------------------------------------------------------------------------
if ! $NO_RESTART; then
  info "Restarting $SYSTEMD_UNIT ..."
  systemctl restart "$SYSTEMD_UNIT"

  # Health check — wait up to 30s for the API to respond
  info "Waiting for health check (up to 30s)..."
  HEALTHY=false
  for i in $(seq 1 6); do
    sleep 5
    HTTP=$(curl -fsS --max-time 3 http://127.0.0.1:8000/api/v1/health 2>/dev/null && echo ok || echo fail)
    if [[ "$HTTP" == "ok" ]]; then
      info "Service healthy after $((i*5))s ✓"
      HEALTHY=true
      break
    fi
    if [[ $i -eq 6 ]]; then
      warn "Health check not passing after 30s — check logs:"
      warn "  journalctl -u $SYSTEMD_UNIT -n 50 --no-pager"
    fi
  done

  # ---------------------------------------------------------------------------
  # 7. Post-deploy safety reset — put Fox ESS into Self Use mode so the
  #    inverter is never stranded in Agile/Force-charge after a release.
  #    This is idempotent and safe to re-run manually at any time.
  # ---------------------------------------------------------------------------
  if $HEALTHY; then
    info "Safety reset: setting Fox ESS work mode → Self Use ..."
    RESET_RESP=$(curl -fsS --max-time 10 -X POST \
      http://127.0.0.1:8000/api/v1/foxess/mode \
      -H 'Content-Type: application/json' \
      -d '{"mode":"Self Use","skip_confirmation":true}' 2>/dev/null || echo '{"success":false}')
    if echo "$RESET_RESP" | grep -q '"success":true'; then
      info "Fox ESS reset to Self Use ✓"
    else
      warn "Fox ESS mode reset failed (non-fatal — check manually): $RESET_RESP"
    fi
  else
    warn "Skipping Fox ESS safety reset (service not healthy)"
  fi
else
  info "Skipping restart (--no-restart)"
fi

info "Deploy complete."
echo ""
echo "  Status:  systemctl status $SYSTEMD_UNIT"
echo "  Logs:    journalctl -u $SYSTEMD_UNIT -f"
echo "  Restart: systemctl restart $SYSTEMD_UNIT"
echo "  DB:      sqlite3 $DB_PATH"
echo "  Backup:  ./scripts/deploy_hetzner.sh --backup-only"
