#!/usr/bin/env bash
# deploy_hetzner.sh — safe deploy/upgrade script for the Hetzner server.
#
# Usage (run on the Hetzner server after git pull):
#   ./scripts/deploy_hetzner.sh                    # build + restart in-place
#   ./scripts/deploy_hetzner.sh --backup           # backup DB to local machine, then deploy
#   ./scripts/deploy_hetzner.sh --backup-only      # backup only, no deploy
#   ./scripts/deploy_hetzner.sh --backup --no-build  # backup + restart, skip image rebuild
#
# Backup destination (off-server to save Hetzner disk space):
#   Set LOCAL_BACKUP_DEST to user@host:/path before running:
#       export LOCAL_BACKUP_DEST="lucas@192.168.x.x:/mnt/c/Users/Lucas/OneDrive/Escritorio/em-backups"
#   or put it in your ~/.bashrc on the Hetzner server.
#
#   If LOCAL_BACKUP_DEST is not set, the backup stays locally (./backups/) but only
#   the 3 newest copies are kept to avoid filling the disk.
#
# Requirements:
#   - docker compose v2  (i.e. "docker compose", not "docker-compose")
#   - sqlite3 CLI        (apt install sqlite3) for integrity check
#   - rsync + ssh        for off-server transfer to local machine
#
# The Docker volume (energy_state_data) is NEVER deleted — data survives all deploys.

set -euo pipefail
cd "$(dirname "$0")/.."

BACKUP_DIR="./backups"
DB_PATH_IN_CONTAINER="/app/data/energy_state.db"
COMPOSE_SERVICE="energy-manager"
LOCAL_BACKUP_DEST="${LOCAL_BACKUP_DEST:-}"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()  { echo -e "${GREEN}[deploy]${NC} $*"; }
warn()  { echo -e "${YELLOW}[deploy]${NC} $*"; }
error() { echo -e "${RED}[deploy]${NC} $*" >&2; exit 1; }

BACKUP=false
BACKUP_ONLY=false
NO_BUILD=false
for arg in "$@"; do
  case "$arg" in
    --backup)       BACKUP=true ;;
    --backup-only)  BACKUP=true; BACKUP_ONLY=true ;;
    --no-build)     NO_BUILD=true ;;
    *)              warn "Unknown arg: $arg" ;;
  esac
done

# ---------------------------------------------------------------------------
# 1. Backup — extract DB from container, rsync to local machine
# ---------------------------------------------------------------------------
if $BACKUP; then
  mkdir -p "$BACKUP_DIR"
  TS=$(date -u +%Y%m%dT%H%M%SZ)
  BACKUP_FILE="$BACKUP_DIR/energy_state_${TS}.sqlite"

  info "Extracting DB from container → $BACKUP_FILE"
  if docker compose ps --quiet "$COMPOSE_SERVICE" 2>/dev/null | grep -q .; then
    # sqlite3 .backup is WAL-safe — no lock needed while container is live
    docker compose exec -T "$COMPOSE_SERVICE" \
      sh -c "sqlite3 $DB_PATH_IN_CONTAINER '.backup /tmp/em_backup.sqlite'" 2>/dev/null || \
      docker compose exec -T "$COMPOSE_SERVICE" cp "$DB_PATH_IN_CONTAINER" /tmp/em_backup.sqlite
    docker compose cp "$COMPOSE_SERVICE:/tmp/em_backup.sqlite" "$BACKUP_FILE"
  else
    warn "Container stopped — using temp container to read volume"
    docker run --rm \
      -v home-energy-manager_energy_state_data:/data \
      -v "$(pwd)/backups:/out" \
      alpine sh -c "cp /data/energy_state.db /out/energy_state_${TS}.sqlite"
  fi

  if [ -f "$BACKUP_FILE" ]; then
    SIZE=$(du -sh "$BACKUP_FILE" | cut -f1)
    info "Extracted: $BACKUP_FILE ($SIZE)"
    if command -v sqlite3 &>/dev/null; then
      INTEGRITY=$(sqlite3 "$BACKUP_FILE" "PRAGMA integrity_check;" 2>&1 || true)
      [[ "$INTEGRITY" == "ok" ]] && info "DB integrity: OK" || warn "Integrity: $INTEGRITY"
    fi

    # Transfer to local machine to keep Hetzner disk free
    if [ -n "$LOCAL_BACKUP_DEST" ]; then
      info "Transferring to $LOCAL_BACKUP_DEST ..."
      if rsync -az --progress "$BACKUP_FILE" "$LOCAL_BACKUP_DEST/"; then
        info "Transfer OK — removing local copy to save Hetzner disk space"
        rm -f "$BACKUP_FILE"
      else
        warn "rsync failed — keeping local copy as fallback (check SSH/path)"
      fi
    else
      warn "LOCAL_BACKUP_DEST not set — backup stays on Hetzner."
      warn "To copy to your machine set:"
      warn "  export LOCAL_BACKUP_DEST='lucas@<your-ip>:/mnt/c/Users/Lucas/OneDrive/Escritorio/em-backups'"
      # Keep only 3 newest to avoid filling disk
      info "Pruning Hetzner local backups (keeping 3 newest)..."
      ls -t "$BACKUP_DIR"/*.sqlite 2>/dev/null | tail -n +4 | xargs rm -f || true
    fi
  else
    warn "Backup extraction may have failed — check $BACKUP_DIR"
  fi
fi

$BACKUP_ONLY && { info "Backup-only — done."; exit 0; }

# ---------------------------------------------------------------------------
# 2. Build new image
# ---------------------------------------------------------------------------
if ! $NO_BUILD; then
  info "Building Docker image (--no-cache)..."
  docker compose build --no-cache "$COMPOSE_SERVICE"
else
  info "Skipping image build (--no-build)"
fi

# ---------------------------------------------------------------------------
# 3. Rolling restart — volume is NEVER removed
# ---------------------------------------------------------------------------
info "Stopping container..."
docker compose stop "$COMPOSE_SERVICE" || true

info "Starting new container..."
docker compose up -d "$COMPOSE_SERVICE"

# ---------------------------------------------------------------------------
# 4. Health check (wait up to 90s)
# ---------------------------------------------------------------------------
info "Waiting for health check (up to 90s)..."
for i in $(seq 1 18); do
  sleep 5
  HTTP=$(curl -fsS --max-time 3 http://127.0.0.1:8000/api/v1/health 2>/dev/null && echo ok || echo fail)
  if [[ "$HTTP" == "ok" ]]; then
    info "Service healthy after $((i*5))s ✓"
    break
  fi
  if [[ $i -eq 18 ]]; then
    error "Service not healthy after 90s. Check: docker compose logs $COMPOSE_SERVICE"
  fi
  info "  attempt $i/18 — waiting..."
done

# ---------------------------------------------------------------------------
# 5. DB migration (idempotent — safe to run every deploy)
# ---------------------------------------------------------------------------
info "Applying DB migrations (idempotent)..."
docker compose exec -T "$COMPOSE_SERVICE" \
  python3 -c "from src.db import init_db; init_db(); print('Migration OK')"

info "Deploy complete."
echo ""
echo "  Logs:    docker compose logs -f $COMPOSE_SERVICE"
echo "  Shell:   docker compose exec $COMPOSE_SERVICE bash"
echo "  DB:      docker compose exec $COMPOSE_SERVICE sqlite3 $DB_PATH_IN_CONTAINER"
echo "  Backup:  export LOCAL_BACKUP_DEST='lucas@<ip>:/path' && ./scripts/deploy_hetzner.sh --backup-only"
