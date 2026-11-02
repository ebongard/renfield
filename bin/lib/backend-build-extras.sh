# shellcheck shell=bash
#
# The two directories the backend Dockerfile COPYs that do NOT live under its
# build context (src/backend/):
#
#   COPY wakeword-models /app/wakeword-models   <- data/wakeword-models/
#   COPY satellite       /app/satellite         <- src/satellite/
#
# Both have to be staged INTO the build context before `docker build`. This file
# is the single definition of that staging step, sourced by
#   - bin/deploy-production.sh    (destination: <build-host>:<staging>/src/backend)
#   - bin/stage-backend-build.sh  (destination: the local src/backend, for compose)
# so the two cannot drift apart (issue #1359: a fresh checkout had no staging
# step at all, and `docker compose build` failed on `COPY satellite`).
#
# Sourced, not executed. Defines one function and nothing else.

# backend_extras_rsync_cmds <repo-root> <destination>
#
# Prints one shell command per line; the caller runs each line (deploy-production
# through its dry-run aware `run`, the compose stager through `eval`).
# <destination> is the build-context directory, local (`/path/src/backend`) or
# remote (`host:/path/src/backend`). Paths are shell-quoted with %q, so a repo
# root containing spaces survives the caller's `eval`.
#
# - wakeword-models: no --delete (an rsync into a context that already holds
#   models must never drop them). The models are gitignored, so on a fresh
#   checkout only `.gitkeep` is copied and the image ships without models --
#   the satellite download endpoint then reports them as absent.
# - satellite: --delete, and WITHOUT provisioning/ (gitignored Ansible
#   inventory with real hostnames) and tests/ -- the backend .dockerignore
#   repeats both excludes as defence in depth.
backend_extras_rsync_cmds() {
  local root="$1" dest="$2"
  printf 'rsync -az %q %q\n' \
    "$root/data/wakeword-models/" "$dest/wakeword-models/"
  printf "rsync -az --delete --exclude='__pycache__' --exclude='*.pyc' --exclude='.pytest_cache' --exclude='provisioning' --exclude='tests' %q %q\n" \
    "$root/src/satellite/" "$dest/satellite/"
}
