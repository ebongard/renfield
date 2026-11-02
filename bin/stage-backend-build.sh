#!/usr/bin/env bash
#
# Prepare a checkout for `docker compose build` / `docker compose up` (#1359).
#
# The backend image's build context is src/backend/, but its Dockerfile also
# COPYs two directories that live elsewhere in the repo (data/wakeword-models/
# and src/satellite/). The production deploy (bin/deploy-production.sh) stages
# them into its build context; a compose build had no such step, so a fresh
# checkout failed on `COPY satellite`. This script runs the SAME staging step
# (bin/lib/backend-build-extras.sh) against the local src/backend/.
#
# It also seeds the bind-mounted local config files compose expects but git does
# not carry, so Docker does not create directories in their place:
#   config/mail_accounts.yaml  <- config/mail_accounts.default.yaml (no accounts)
# An existing file is never overwritten.
#
# Re-run it after changing src/satellite/ or data/wakeword-models/ and before
# the next `docker compose build`. The staged copies under src/backend/ are
# gitignored.
#
# Usage: bin/stage-backend-build.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# shellcheck source=lib/backend-build-extras.sh
. "$REPO_ROOT/bin/lib/backend-build-extras.sh"

case "${1:-}" in
  -h|--help) sed -n '2,21p' "$0"; exit 0 ;;
  "") ;;
  *) echo "unknown arg: $1" >&2; exit 2 ;;
esac

command -v rsync >/dev/null 2>&1 || { echo "ERROR: rsync is required" >&2; exit 1; }

echo "Staging wakeword models + satellite source into src/backend/ ..."
# Read on FD 3, not stdin, so a command that reads stdin cannot swallow the rest.
while IFS= read -r -u 3 cmd; do
  eval "$cmd"
done 3< <(backend_extras_rsync_cmds "$REPO_ROOT" "$REPO_ROOT/src/backend")

seed_config() {
  local target="$REPO_ROOT/config/$1" source="$REPO_ROOT/config/$2"
  if [[ -e "$target" ]]; then
    echo "  [KEEP] config/$1 (already exists)"
  else
    cp "$source" "$target"
    echo "  [OK]   config/$1 (seeded from config/$2)"
  fi
}
echo "Seeding local config files ..."
seed_config mail_accounts.yaml mail_accounts.default.yaml

# secrets/ itself is tracked (only its .gitignore); the files are not.
if [[ ! -f "$REPO_ROOT/secrets/postgres_password" ]]; then
  echo
  echo "NOTE: secrets/ holds no secret files yet. Run bin/generate-secrets.sh before"
  echo "      'docker compose up' -- compose refuses to start without the files."
fi
echo "Done."
