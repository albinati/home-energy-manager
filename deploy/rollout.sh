#!/usr/bin/env bash
# HEM prod rollout — one command, safe by construction:
#   manifest-guard → pin HEM_IMAGE_TAG → restart → health-verify (auto-rollback
#   on failure) → prune old app images (keep current + previous for rollback).
#
# Runs ON THE HOST (copy to /srv/hem/rollout.sh; the repo copy is the source of
# truth — re-copy after editing). Usage:
#   /srv/hem/rollout.sh <full-git-sha>
#
# Added 2026-07-02: deploys always pulled a new 534 MB image and never removed
# old ones — after a multi-PR session the disk crossed 85 %. The prune step
# keeps exactly current + previous.
set -euo pipefail

SHA="${1:?usage: rollout.sh <full git sha>}"
IMAGE="ghcr.io/albinati/home-energy-manager"
COMPOSE_ENV=/srv/hem/.compose.env
HEALTH_URL=http://127.0.0.1:8000/api/v1/health

# 1. Manifest-guard: never pin a tag that cannot be pulled (a piped pull under
#    `set -e` masks failure → outage; see reference_hem_deploy_procedure).
docker manifest inspect "$IMAGE:sha-$SHA" > /dev/null
echo "manifest OK: sha-${SHA:0:8}"

# 2. Pin the tag (backup first — the .bak is the rollback vehicle).
prev_tag=$(sed -n 's/^HEM_IMAGE_TAG=//p' "$COMPOSE_ENV")
cp "$COMPOSE_ENV" "$COMPOSE_ENV.bak"
sed -i "s|^HEM_IMAGE_TAG=.*|HEM_IMAGE_TAG=sha-$SHA|" "$COMPOSE_ENV"
systemctl restart hem

# 3. Health-verify: the service must come up reporting the NEW revision.
rev=""
for _ in $(seq 1 30); do
  rev=$(curl -s --max-time 5 "$HEALTH_URL" | sed -n 's/.*"revision":"\([^"]*\)".*/\1/p') || true
  [ "$rev" = "$SHA" ] && break
  sleep 6
done
if [ "$rev" != "$SHA" ]; then
  echo "HEALTH VERIFY FAILED (rev=${rev:-none}) — rolling back to ${prev_tag:-<unchanged>}" >&2
  cp "$COMPOSE_ENV.bak" "$COMPOSE_ENV"
  systemctl restart hem
  exit 1
fi
echo "health OK: rev=${rev:0:8} ($(systemctl is-active hem))"

# 4. Prune: keep current + previous app images only. UI/quartz images are NOT
#    touched (separate repos/tags). `|| true` — an in-use layer refusal is fine.
keep_new="sha-$SHA"
docker images --format '{{.Tag}}' "$IMAGE" | while read -r tag; do
  if [ "$tag" != "$keep_new" ] && [ "$tag" != "$prev_tag" ]; then
    docker rmi "$IMAGE:$tag" > /dev/null 2>&1 && echo "pruned $tag" || true
  fi
done
docker image prune -f > /dev/null
echo "prune done (kept $keep_new + ${prev_tag:-none}); disk: $(df -h / | awk 'NR==2{print $5" used, "$4" free"}')"
