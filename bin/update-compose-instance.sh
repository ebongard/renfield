#!/usr/bin/env bash
#
# Update a pull-based single-host Docker Compose instance (e.g. "mama") to
# image tags that already exist on the build box. The k8s instances are
# deployed by bin/deploy-production.sh; a compose host is not reached by it
# and falls behind silently (2026-11-08: one week and four migrations).
#
# What it does:
#   1. preflight: target reachable, compose file + .env present, images on the build box
#   2. backup: pg_dump -Fc of the instance database + a copy of .env, both in
#      <dir>/backups/ with one timestamp; the dump is checked (pg_restore -l)
#   3. copy the images build box -> target (docker save | docker load), only
#      the ones the target does not have yet
#   4. write the tags into .env (RENFIELD_BACKEND_IMAGE / RENFIELD_FRONTEND_IMAGE)
#   5. GATE: Alembic head IN THE NEW IMAGE against `alembic current` of the
#      instance database. Differ without --migrate -> abort and put the old
#      .env back. An unreadable probe never passes.
#   6. (--migrate) `alembic upgrade head` with the new image, BEFORE the restart
#   7. `up -d`, wait for the backend healthcheck, /health/ready from inside,
#      error count in the logs of backend + workers
#   8. (--prune-old) remove Renfield images on the target other than the new
#      and the previous tags (the previous one is the rollback target)
#
# Rollback: --rollback <timestamp> puts backups/env.<timestamp> back and runs
# `up -d`. It skips the gate on purpose: an older image on a newer schema is
# safe (the ORM only misses columns it does not know). The database is NOT
# restored; the matching dump is named in the output.
#
# Usage:
#   bin/update-compose-instance.sh --target seven.local --dir '~/renfield-mama' \
#     --compose-file docker-compose.mama.yml --env-script env.sh \
#     --backend-tag 2026-11-08-chat-fk-rules --frontend-tag 2026-11-07-backlog-fixes --migrate
#   bin/update-compose-instance.sh --target seven.local --dir '~/renfield-mama' \
#     --compose-file docker-compose.mama.yml --env-script env.sh --rollback 20261108-141756
#   add --dry-run to print the steps without changing anything
#
# Requires RENFIELD_REGISTRY (as deploy-production.sh). The compose file must
# name its images ${RENFIELD_BACKEND_IMAGE} / ${RENFIELD_FRONTEND_IMAGE} and
# call its services backend / postgres (the workers share the backend image).
#
set -euo pipefail

BUILD_HOST="${RENFIELD_BUILD_HOST:-evdb@192.168.1.159}"

# --- args -------------------------------------------------------------------
TARGET="" DIR="" COMPOSE_FILE="" ENV_SCRIPT="" BACKEND_TAG="" FRONTEND_TAG=""
DO_MIGRATE=0 DRY_RUN=0 PRUNE_OLD=0 ROLLBACK=""
need_value() {
  [[ $# -ge 2 && -n "$2" && "$2" != -* ]] || { echo "ERROR: $1 needs a value" >&2; exit 2; }
}
while [[ $# -gt 0 ]]; do
  case "$1" in
    --target)        need_value "$@"; TARGET="$2"; shift 2 ;;
    --dir)           need_value "$@"; DIR="$2"; shift 2 ;;
    --compose-file)  need_value "$@"; COMPOSE_FILE="$2"; shift 2 ;;
    --env-script)    need_value "$@"; ENV_SCRIPT="$2"; shift 2 ;;
    --backend-tag)   need_value "$@"; BACKEND_TAG="$2"; shift 2 ;;
    --frontend-tag)  need_value "$@"; FRONTEND_TAG="$2"; shift 2 ;;
    --rollback)      need_value "$@"; ROLLBACK="$2"; shift 2 ;;
    --migrate)       DO_MIGRATE=1; shift ;;
    --prune-old)     PRUNE_OLD=1; shift ;;
    --dry-run)       DRY_RUN=1; shift ;;
    -h|--help)       sed -n '2,41p' "$0"; exit 0 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

REGISTRY="${RENFIELD_REGISTRY:?set RENFIELD_REGISTRY, e.g. export RENFIELD_REGISTRY=your-registry.example/renfield}"
[[ -n "$TARGET" && -n "$DIR" && -n "$COMPOSE_FILE" ]] \
  || { echo "ERROR: --target, --dir and --compose-file are required" >&2; exit 2; }
if [[ -n "$ROLLBACK" ]]; then
  [[ "$ROLLBACK" =~ ^[0-9]{8}-[0-9]{6}$ ]] || { echo "ERROR: --rollback takes a backup timestamp (YYYYMMDD-HHMMSS)" >&2; exit 2; }
  [[ -z "$BACKEND_TAG$FRONTEND_TAG" && $DO_MIGRATE == 0 ]] \
    || { echo "ERROR: --rollback takes no tags and no --migrate" >&2; exit 2; }
else
  [[ -n "$BACKEND_TAG" && -n "$FRONTEND_TAG" ]] \
    || { echo "ERROR: --backend-tag and --frontend-tag are required" >&2; exit 2; }
fi

BACKEND_IMAGE="$REGISTRY/backend:$BACKEND_TAG"
FRONTEND_IMAGE="$REGISTRY/frontend:$FRONTEND_TAG"

# --- helpers ----------------------------------------------------------------
log() { printf '\n\033[1;36m=== %s ===\033[0m\n' "$*"; }
die() { echo "ERROR: $*" >&2; exit 1; }

# Everything on the target runs through ONE remote program, read by `bash -s`
# from stdin, so the target's login shell (zsh on macOS) never parses it and
# the quoting stays in one place. Arguments are passed %q-quoted.
read -r -d '' REMOTE <<'REMOTE_EOF' || true
set -euo pipefail
DIR=$1 CF=$2 ENVSH=$3 STEP=$4; shift 4
cd "${DIR/#\~/$HOME}"
if [[ -n "$ENVSH" ]]; then set +u; . "./$ENVSH" >/dev/null 2>&1; set -u; fi
dc() { docker compose -f "$CF" "$@"; }
case "$STEP" in
  check)
    [[ -f "$CF" ]] || { echo "no compose file $CF in $PWD" >&2; exit 3; }
    [[ -f .env ]] || { echo "no .env in $PWD" >&2; exit 3; }
    docker info >/dev/null
    grep -E '^RENFIELD_(BACKEND|FRONTEND)_IMAGE=' .env ;;
  has-image)
    if docker image inspect "$1" >/dev/null 2>&1; then echo yes; else echo no; fi ;;
  load)
    gunzip | docker load ;;
  backup)
    mkdir -p backups
    dump="backups/db-$1.dump"
    dc exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$dump"
    cp .env "backups/env.$1"
    chmod 600 "$dump" "backups/env.$1"
    n=$(dc exec -T postgres pg_restore -l < "$dump" | grep -c 'TABLE DATA' || true)
    [[ "$n" -gt 0 ]] || { echo "dump $dump lists no table data" >&2; exit 4; }
    echo "$dump ($(wc -c < "$dump" | tr -d ' ') bytes, $n tables with data)" ;;
  set-tags)
    awk -v b="$1" -v f="$2" '
      /^RENFIELD_BACKEND_IMAGE=/  { print "RENFIELD_BACKEND_IMAGE=" b; nb=1; next }
      /^RENFIELD_FRONTEND_IMAGE=/ { print "RENFIELD_FRONTEND_IMAGE=" f; nf=1; next }
      { print }
      END { if (!nb || !nf) exit 5 }' .env > .env.update-tmp
    mv .env.update-tmp .env
    grep -E '^RENFIELD_(BACKEND|FRONTEND)_IMAGE=' .env ;;
  restore-env)
    [[ -f "backups/env.$1" ]] || { echo "no backups/env.$1" >&2; exit 3; }
    cp "backups/env.$1" .env
    grep -E '^RENFIELD_(BACKEND|FRONTEND)_IMAGE=' .env ;;
  heads)
    dc run --rm --no-deps -T backend alembic heads ;;
  current)
    dc run --rm --no-deps -T backend alembic current ;;
  migrate)
    dc run --rm --no-deps -T backend alembic upgrade head ;;
  up)
    dc up -d
    cid=$(dc ps -q backend)
    [[ -n "$cid" ]] || { echo "no backend container" >&2; exit 6; }
    status=""
    for _ in $(seq 1 60); do
      status=$(docker inspect -f '{{.State.Health.Status}}' "$cid" 2>/dev/null || true)
      [[ "$status" == healthy ]] && break
      sleep 5
    done
    echo "backend health: ${status:-none}"
    [[ "$status" == healthy ]] || exit 7
    code=$(dc exec -T backend curl -s -o /dev/null -w '%{http_code}' --max-time 10 http://localhost:8000/health/ready || true)
    echo "/health/ready: $code"
    [[ "$code" == 200 ]] || exit 7 ;;
  log-errors)
    total=0
    while read -r svc img; do
      [[ "$img" == *"/backend:"* ]] || continue
      n=$(dc logs --since "$1" "$svc" 2>&1 | grep -ciE 'traceback|\berror\b|critical' || true)
      echo "$svc: $n"
      total=$((total + n))
    done < <(dc ps --format '{{.Service}} {{.Image}}')
    echo "total=$total" ;;
  history)
    # Image references from the .env backups, newest first.
    ls -t backups/env.* 2>/dev/null | while read -r f; do
      grep -E '^RENFIELD_(BACKEND|FRONTEND)_IMAGE=' "$f" | cut -d= -f2-
    done ;;
  images)
    docker images --format '{{.Repository}}:{{.Tag}}' | grep -E "^$1/(backend|frontend):" || true ;;
  in-use)
    docker ps -a --format '{{.Image}}' ;;
  rmi)
    docker rmi "$@" ;;
  *)
    echo "unknown step $STEP" >&2; exit 2 ;;
esac
REMOTE_EOF

q() { printf '%q ' "$@"; }
# Read-only steps run in a dry run too, so the trial shows the real state.
on_target() {
  ssh "$TARGET" "bash -s -- $(q "$DIR" "$COMPOSE_FILE" "$ENV_SCRIPT" "$@")" <<<"$REMOTE"
}
on_target_change() {
  if [[ $DRY_RUN == 1 ]]; then printf '  [dry-run] target: %s\n' "$*"; else on_target "$@"; fi
}
# The first revision id an `alembic heads` / `alembic current` printed.
first_rev() { grep -oE '^[A-Za-z0-9_]+ \((head|current)' | head -1 | cut -d' ' -f1; }

# --- 1. preflight -----------------------------------------------------------
log "preflight: $TARGET:$DIR ($COMPOSE_FILE)"
on_target check || die "target not ready"

if [[ -n "$ROLLBACK" ]]; then
  log "rollback to backups/env.$ROLLBACK"
  on_target_change restore-env "$ROLLBACK"
  on_target_change up
  echo "Database NOT restored. The dump from that run: backups/db-$ROLLBACK.dump"
  echo "(older runs named it renfield-pre-*-$ROLLBACK.dump)"
  exit 0
fi

for img in "$BACKEND_IMAGE" "$FRONTEND_IMAGE"; do
  ssh "$BUILD_HOST" "docker image inspect $img >/dev/null 2>&1" \
    || on_target has-image "$img" | grep -qx yes \
    || die "$img is neither on $BUILD_HOST nor on the target"
done
echo "backend:  $BACKEND_IMAGE"
echo "frontend: $FRONTEND_IMAGE"
echo "migrate: $DO_MIGRATE  prune-old: $PRUNE_OLD  dry-run: $DRY_RUN"

PREVIOUS_IMAGES=$(on_target check | cut -d= -f2-)

# --- 2. backup --------------------------------------------------------------
TS=$(date +%Y%m%d-%H%M%S)
log "backup ($TS)"
on_target_change backup "$TS"

# --- 3. images --------------------------------------------------------------
log "images build box -> target"
missing=()
for img in "$BACKEND_IMAGE" "$FRONTEND_IMAGE"; do
  if on_target has-image "$img" | grep -qx yes; then echo "  present: $img"; else missing+=("$img"); fi
done
if [[ ${#missing[@]} -gt 0 ]]; then
  echo "  copying: ${missing[*]}"
  if [[ $DRY_RUN == 1 ]]; then
    echo "  [dry-run] ssh $BUILD_HOST docker save ${missing[*]} | gzip -1 | target docker load"
  else
    ssh "$BUILD_HOST" "docker save ${missing[*]} | gzip -1" | on_target load
  fi
fi

# --- 4. tags ----------------------------------------------------------------
log "tags into .env"
on_target_change set-tags "$BACKEND_IMAGE" "$FRONTEND_IMAGE"

# --- 5. gate ----------------------------------------------------------------
log "gate: Alembic head in the new image against the instance database"
if [[ $DRY_RUN == 1 ]]; then
  echo "  [dry-run] the gate reads the NEW image, which a dry run did not set — not testable here"
else
  restore_and_die() {
    on_target restore-env "$TS" >/dev/null || echo "WARNING: could not restore .env from backups/env.$TS" >&2
    die "$*"
  }
  img_head=$(on_target heads 2>/dev/null | first_rev || true)
  db_rev=$(on_target current 2>/dev/null | first_rev || true)
  [[ -n "$img_head" && -n "$db_rev" ]] \
    || restore_and_die "gate probe unreadable (image head '${img_head:-?}', database '${db_rev:-?}') — .env restored, nothing restarted"
  echo "  image head: $img_head"
  echo "  database:   $db_rev"
  if [[ "$img_head" != "$db_rev" ]]; then
    [[ $DO_MIGRATE == 1 ]] \
      || restore_and_die "database is not at the image head and --migrate is not set — .env restored, nothing restarted"
    # --- 6. migrate -----------------------------------------------------------
    log "alembic upgrade head (new image, before the restart)"
    on_target migrate || restore_and_die "migration failed — .env restored, nothing restarted"
    db_rev=$(on_target current 2>/dev/null | first_rev || true)
    [[ "$db_rev" == "$img_head" ]] || restore_and_die "after the upgrade the database is at '${db_rev:-?}', not $img_head"
    echo "  database now: $db_rev"
  fi
fi

# --- 7. restart + checks ----------------------------------------------------
log "up -d + health"
on_target_change up || die "the new containers are not healthy — roll back with --rollback $TS"
if [[ $DRY_RUN == 0 ]]; then
  log "errors in the logs since the restart"
  on_target log-errors 3m
fi

# --- 8. prune ---------------------------------------------------------------
if [[ $PRUNE_OLD == 1 ]]; then
  log "prune old images (keep new + the rollback target)"
  # The rollback target is the newest OTHER image of each kind: from the .env
  # this run replaced, else from the backup history. Keeping just "the .env
  # this run replaced" would drop it on a re-run with unchanged tags.
  keep=$(printf '%s\n' "$BACKEND_IMAGE" "$FRONTEND_IMAGE")
  for kind in backend frontend; do
    new_one=$BACKEND_IMAGE; [[ $kind == frontend ]] && new_one=$FRONTEND_IMAGE
    prev=$( { printf '%s\n' $PREVIOUS_IMAGES; on_target history; } \
      | grep -E "/$kind:" | grep -vxF "$new_one" | head -1 || true)
    [[ -n "$prev" ]] && { echo "  rollback target kept: $prev"; keep+=$'\n'"$prev"; }
  done
  in_use=$(on_target in-use)
  drop=()
  while IFS= read -r img; do
    [[ -z "$img" ]] && continue
    grep -qxF "$img" <<<"$keep" && continue
    grep -qxF "$img" <<<"$in_use" && continue
    drop+=("$img")
  done < <(on_target images "$REGISTRY")
  if [[ ${#drop[@]} -eq 0 ]]; then echo "  nothing to remove"
  else on_target_change rmi "${drop[@]}"; fi
fi

log "done: $BACKEND_TAG / $FRONTEND_TAG on $TARGET (backup $TS)"
