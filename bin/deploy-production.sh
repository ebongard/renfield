#!/usr/bin/env bash
#
# Renfield backend/frontend production deploy — codifies the mechanical backbone
# of the deploy-production skill (.claude/skills/deploy-production/SKILL.md),
# which stays the source of truth for the "why" and for everything this script
# DELIBERATELY does NOT do (see below).
#
# What it does (idempotent, re-runnable):
#   1. rsync the build contexts to the .159 build box
#   2. build + push the backend and/or frontend image to Harbor
#   3. (optional, --migrate) run the alembic-upgrade Job with the NEW image,
#      BEFORE the rollout, and verify it completed
#   4. `set image` + `rollout status` for the changed deployments
#   5. smoke test (/health, alembic current, live image tags)
#   6. clean the staging dir + prune old images on .159
#
# What it deliberately does NOT do (keep these manual — see the skill):
#   - SATELLITE deploys. Pi Zero SD cards brick on a bad restart (skill CRITICAL
#     SAFETY). Provision/restart satellites by hand with the satellite-deploy agent.
#   - voice-server / dlna-mcp / samsung-mcp images (separate repos + cadence).
#   - Harbor 504 retry judgment on the big deps layer (the skill documents it).
#   - Inventing version tags — you pass them in.
#
# Usage:
#   bin/deploy-production.sh --backend-tag 2026-06-25-satsec --frontend-tag v2.15.29 --migrate
#   bin/deploy-production.sh --backend-tag 2026-06-26-foo --skip-frontend          # backend only
#   bin/deploy-production.sh --frontend-tag v2.15.30 --skip-backend                # frontend only
#   bin/deploy-production.sh --backend-tag x --frontend-tag y --dry-run            # print, don't run
#
# Conventions (from the live cluster): backend tags are date-based
# (YYYY-MM-DD-label), frontend tags are semver (vX.Y.Z). Both also get :latest.
set -euo pipefail

# --- config -----------------------------------------------------------------
BUILD_HOST="${RENFIELD_BUILD_HOST:-evdb@192.168.1.159}"
REGISTRY="${RENFIELD_REGISTRY:-registry.treehouse.x-idra.de/renfield}"
KCTX="${RENFIELD_KCTX:-renfield-private}"
NS="${RENFIELD_NS:-renfield}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# --- args -------------------------------------------------------------------
BACKEND_TAG="" FRONTEND_TAG="" DO_MIGRATE=0 DRY_RUN=0 SKIP_BACKEND=0 SKIP_FRONTEND=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --backend-tag)   BACKEND_TAG="$2"; shift 2 ;;
    --frontend-tag)  FRONTEND_TAG="$2"; shift 2 ;;
    --migrate)       DO_MIGRATE=1; shift ;;
    --dry-run)       DRY_RUN=1; shift ;;
    --skip-backend)  SKIP_BACKEND=1; shift ;;
    --skip-frontend) SKIP_FRONTEND=1; shift ;;
    -h|--help)       sed -n '2,40p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

[[ $SKIP_BACKEND == 0 && -z "$BACKEND_TAG" ]] && { echo "ERROR: --backend-tag required (or --skip-backend)"; exit 2; }
[[ $SKIP_FRONTEND == 0 && -z "$FRONTEND_TAG" ]] && { echo "ERROR: --frontend-tag required (or --skip-frontend)"; exit 2; }

STAGING="/tmp/renfield-build-${BACKEND_TAG:-$FRONTEND_TAG}"
KUBECTL=(kubectl --context "$KCTX" -n "$NS")

# --- helpers ----------------------------------------------------------------
log() { printf '\n\033[1;36m=== %s ===\033[0m\n' "$*"; }
run() { if [[ $DRY_RUN == 1 ]]; then printf '  [dry-run] %s\n' "$*"; else eval "$@"; fi; }
# Run a command on the build host. The command is passed to ssh as ONE argument,
# so the REMOTE shell does all the quoting — a payload containing single quotes
# (e.g. the prune's `--format '{{.Repository}}:{{.Tag}}'`) no longer breaks the
# way the old `run "ssh $HOST '$*'"` nested-single-quoting did under `eval`
# + `set -e` (which exited the script non-zero AFTER a successful deploy).
on_build() {
  if [[ $DRY_RUN == 1 ]]; then printf '  [dry-run] ssh %s: %s\n' "$BUILD_HOST" "$*"
  else ssh "$BUILD_HOST" "$*"; fi
}

# --- preflight --------------------------------------------------------------
log "preflight"
if ! git -C "$REPO_ROOT" diff --quiet || ! git -C "$REPO_ROOT" diff --cached --quiet; then
  echo "WARNING: working tree has uncommitted changes — the image will include them."
fi
echo "commit:   $(git -C "$REPO_ROOT" rev-parse --short HEAD) ($(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD))"
echo "backend:  $([[ $SKIP_BACKEND == 1 ]] && echo SKIP || echo "$BACKEND_TAG")"
echo "frontend: $([[ $SKIP_FRONTEND == 1 ]] && echo SKIP || echo "$FRONTEND_TAG")"
echo "migrate:  $DO_MIGRATE   dry-run: $DRY_RUN"
run "${KUBECTL[*]} version --output=json >/dev/null"  # cluster reachable?

# --- 1. rsync build contexts ------------------------------------------------
log "rsync build contexts → $BUILD_HOST:$STAGING"
on_build "rm -rf $STAGING; mkdir -p $STAGING/src/backend $STAGING/src/frontend"
RSYNC="rsync -az --delete"
if [[ $SKIP_BACKEND == 0 ]]; then
  run "$RSYNC --exclude='__pycache__' --exclude='.pytest_cache' --exclude='*.pyc' \
    --exclude='.coverage' --exclude='htmlcov' --exclude='.env' --exclude='.env.local' \
    --exclude='secrets/' --exclude='Users/' \
    $REPO_ROOT/src/backend/ $BUILD_HOST:$STAGING/src/backend/"
  # wakeword models + satellite source (bundled into the backend image)
  run "rsync -az $REPO_ROOT/data/wakeword-models/ $BUILD_HOST:$STAGING/src/backend/wakeword-models/"
  run "$RSYNC --exclude='__pycache__' --exclude='*.pyc' --exclude='.pytest_cache' \
    --exclude='provisioning' --exclude='tests' \
    $REPO_ROOT/src/satellite/ $BUILD_HOST:$STAGING/src/backend/satellite/"
fi
if [[ $SKIP_FRONTEND == 0 ]]; then
  run "$RSYNC --exclude='node_modules' --exclude='dist' --exclude='.vite' \
    --exclude='.cache' --exclude='.env' --exclude='.env.local' \
    $REPO_ROOT/src/frontend/ $BUILD_HOST:$STAGING/src/frontend/"
fi

# --- 2. build + push --------------------------------------------------------
if [[ $SKIP_BACKEND == 0 ]]; then
  log "build + push backend:$BACKEND_TAG"
  on_build "set -e; cd $STAGING/src/backend && \
    docker build -q -t $REGISTRY/backend:latest -t $REGISTRY/backend:$BACKEND_TAG -f Dockerfile . && \
    docker push -q $REGISTRY/backend:$BACKEND_TAG && docker push -q $REGISTRY/backend:latest"
fi
if [[ $SKIP_FRONTEND == 0 ]]; then
  log "build + push frontend:$FRONTEND_TAG"
  on_build "set -e; cd $STAGING/src/frontend && \
    docker build -q --build-arg VITE_FEATURE_VOICE_STREAM=true --build-arg VITE_BUILD_STAMP=$FRONTEND_TAG -t $REGISTRY/frontend:latest -t $REGISTRY/frontend:$FRONTEND_TAG -f Dockerfile . && \
    docker push -q $REGISTRY/frontend:$FRONTEND_TAG && docker push -q $REGISTRY/frontend:latest"
fi

# --- 3. migrate (BEFORE rollout, with the new image) ------------------------
if [[ $DO_MIGRATE == 1 ]]; then
  log "alembic migration (new image, before rollout)"
  run "${KUBECTL[*]} delete job alembic-upgrade --ignore-not-found"
  run "${KUBECTL[*]} apply -f $REPO_ROOT/k8s/alembic-upgrade-job.yaml"
  if [[ $DRY_RUN == 0 ]]; then
    if ! "${KUBECTL[@]}" wait --for=condition=Complete job/alembic-upgrade --timeout=300s; then
      echo "ERROR: migration job did not complete — logs follow:" >&2
      "${KUBECTL[@]}" logs job/alembic-upgrade >&2 || true
      echo "(left the job in place; 'Multiple head revisions' = forked chain, fix-forward needed)" >&2
      exit 1
    fi
    "${KUBECTL[@]}" logs job/alembic-upgrade | grep -iE "running upgrade|no upgrade|error" || true
    "${KUBECTL[@]}" delete job alembic-upgrade
  fi
fi

# --- 4. rollout (pinned tags → set image, not rollout restart) --------------
log "rollout"
if [[ $SKIP_BACKEND == 0 ]]; then
  run "${KUBECTL[*]} set image deploy/backend backend=$REGISTRY/backend:$BACKEND_TAG"
  run "${KUBECTL[*]} set image deploy/document-worker worker=$REGISTRY/backend:$BACKEND_TAG"  # container is 'worker'
fi
[[ $SKIP_FRONTEND == 0 ]] && run "${KUBECTL[*]} set image deploy/frontend frontend=$REGISTRY/frontend:$FRONTEND_TAG"
if [[ $DRY_RUN == 0 ]]; then
  [[ $SKIP_BACKEND == 0 ]] && "${KUBECTL[@]}" rollout status deploy/backend --timeout=600s
  [[ $SKIP_BACKEND == 0 ]] && "${KUBECTL[@]}" rollout status deploy/document-worker --timeout=600s
  [[ $SKIP_FRONTEND == 0 ]] && "${KUBECTL[@]}" rollout status deploy/frontend --timeout=600s
fi

# --- 5. smoke test ----------------------------------------------------------
log "smoke test"
if [[ $DRY_RUN == 0 ]]; then
  "${KUBECTL[@]}" exec deploy/backend -c backend -- curl -sS http://localhost:8000/health | head -c 120; echo
  "${KUBECTL[@]}" exec deploy/backend -c backend -- alembic current 2>&1 | grep -vi INFO | tail -1
  "${KUBECTL[@]}" get deploy backend document-worker frontend \
    -o 'custom-columns=D:.metadata.name,IMG:.spec.template.spec.containers[0].image'
fi

# --- 6. cleanup + prune on the build box ------------------------------------
log "cleanup + image prune on $BUILD_HOST"
on_build "rm -rf $STAGING; \
  for repo in backend frontend; do \
    docker images \"$REGISTRY/\$repo\" --format '{{.Repository}}:{{.Tag}}' | tail -n +4 | xargs -r -n1 docker rmi 2>/dev/null || true; \
  done; \
  docker image prune -f >/dev/null 2>&1; docker builder prune -f --keep-storage 10GB >/dev/null 2>&1; \
  df -h / | tail -1" \
  || echo "WARNING: build-box cleanup failed (non-fatal — deploy already succeeded); check disk on $BUILD_HOST" >&2

log "DONE"
