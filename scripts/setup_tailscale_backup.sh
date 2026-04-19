#!/usr/bin/env bash
# setup_tailscale_backup.sh — one-time Tailscale setup for Hetzner → local machine backups.
#
# Run this script ONCE on the Hetzner server to:
#   1. Install Tailscale
#   2. Enable Tailscale SSH (no key management ever again)
#   3. Print the auth link so you can join the tailnet
#   4. Set LOCAL_BACKUP_DEST in the environment using your machine's MagicDNS hostname
#
# After this, deploy_hetzner.sh uses rsync over Tailscale automatically.
#
# Requirements:
#   - Tailscale installed on your LOCAL machine too (https://tailscale.com/download)
#   - A free Tailscale account (tailscale.com — free up to 100 devices)
#   - Your local machine's Tailscale hostname (shown in https://login.tailscale.com/admin/machines)

set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${GREEN}[tailscale-setup]${NC} $*"; }
warn()  { echo -e "${YELLOW}[tailscale-setup]${NC} $*"; }
note()  { echo -e "${CYAN}[tailscale-setup]${NC} $*"; }

# ---------------------------------------------------------------------------
# 1. Install Tailscale (idempotent — skips if already installed)
# ---------------------------------------------------------------------------
if ! command -v tailscale &>/dev/null; then
  info "Installing Tailscale..."
  curl -fsSL https://tailscale.com/install.sh | sh
else
  info "Tailscale already installed: $(tailscale version)"
fi

# ---------------------------------------------------------------------------
# 2. Enable Tailscale SSH (replaces SSH key management entirely)
#    --ssh lets you ssh user@hetzner-hostname from any tailnet member without keys
# ---------------------------------------------------------------------------
info "Enabling Tailscale SSH on this server..."
sudo tailscale up --ssh --accept-routes 2>/dev/null || \
  sudo tailscale set --ssh

# ---------------------------------------------------------------------------
# 3. Show the tailnet status and MagicDNS hostname of THIS server
# ---------------------------------------------------------------------------
echo ""
TAILSCALE_STATUS=$(tailscale status --json 2>/dev/null || echo "{}")
THIS_HOSTNAME=$(echo "$TAILSCALE_STATUS" | python3 -c \
  "import json,sys; d=json.load(sys.stdin); \
   s=d.get('Self',{}); print(s.get('DNSName','').rstrip('.'))" 2>/dev/null || hostname)
THIS_IP=$(tailscale ip -4 2>/dev/null || echo "unknown")

info "This Hetzner server on Tailscale:"
note "  Hostname : $THIS_HOSTNAME"
note "  IP       : $THIS_IP"
echo ""

# ---------------------------------------------------------------------------
# 4. Interactive: set the local machine backup destination
# ---------------------------------------------------------------------------
warn "ACTION REQUIRED:"
echo ""
echo "  On your LOCAL machine (Windows/WSL):"
echo "    1. Install Tailscale: https://tailscale.com/download"
echo "    2. Sign in with the same Tailscale account"
echo "    3. Find your machine's MagicDNS name at: https://login.tailscale.com/admin/machines"
echo "       It will look like: lucas-laptop.tail1234ab.ts.net"
echo ""
echo "  Then run this on the Hetzner server to complete setup:"
echo ""
echo "    export LOCAL_BACKUP_DEST='lucas@lucas-laptop.tail1234ab.ts.net:/mnt/c/Users/Lucas/OneDrive/Escritorio/em-backups'"
echo "    echo \"export LOCAL_BACKUP_DEST='lucas@lucas-laptop.tail1234ab.ts.net:/mnt/c/Users/Lucas/OneDrive/Escritorio/em-backups'\" >> ~/.bashrc"
echo ""
echo "  Then test:"
echo "    rsync --dry-run -av /etc/hostname \"\$LOCAL_BACKUP_DEST/\""
echo ""

# ---------------------------------------------------------------------------
# 5. Add LOCAL_BACKUP_DEST to project .env.example as documentation
# ---------------------------------------------------------------------------
if [ -f ".env.example" ] && ! grep -q "LOCAL_BACKUP_DEST" .env.example 2>/dev/null; then
  echo "" >> .env.example
  echo "# Tailscale backup destination (Hetzner → local machine via MagicDNS)" >> .env.example
  echo "# LOCAL_BACKUP_DEST=lucas@your-laptop.tail1234ab.ts.net:/path/to/backups" >> .env.example
  info "Added LOCAL_BACKUP_DEST hint to .env.example"
fi

info "Tailscale setup complete on Hetzner side."
echo ""
echo "Next steps:"
echo "  1. Install Tailscale on your local machine and sign in"
echo "  2. Set LOCAL_BACKUP_DEST (see above)"
echo "  3. Deploy: ./scripts/deploy_hetzner.sh --backup"
echo "  4. SSH from local: ssh root@$THIS_HOSTNAME  (no keys needed!)"
