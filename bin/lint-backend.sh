#!/usr/bin/env bash
# Lint the backend with ruff -- on the HOST, not in the container.
#
# WHY NOT IN THE CONTAINER
# ------------------------
# Until 2026-09-27 `make lint-backend` ran as
#   docker compose exec -T backend ruff check /app --config /app/pyproject.toml
# and that was impossible for two separate reasons:
#
#   1. `pyproject.toml` sits in the project root, but the backend image's build
#      context is `src/backend/` -- the file is NEVER present in the image.
#      The error, verbatim:
#      `error: invalid value '/app/pyproject.toml' for '--config <CONFIG_OPTION>'`
#   2. `ruff` is listed in `requirements-test.txt`. The production image
#      installs only `requirements.txt` -- `ruff` does not exist in the
#      container at all (`sh: 1: ruff: not found`, checked 2026-09-27 on .159).
#
# So the container was never the right place. ruff is a single Rust binary with
# no project dependencies; it belongs on the host, exactly like eslint in the
# frontend target.
#
# WHY THE VERSION IS PINNED
# -------------------------
# `RUF100` ("unused noqa") judges against the ACTIVE rule set. Changing the
# ruff version shifts that set and with it the number of findings -- a gate
# whose verdict depends on whichever version happens to be installed is not a
# gate. When raising it: change the version HERE AND in
# `src/backend/requirements-test.txt`, then run `make lint-backend` once and
# clean up the new findings.
set -euo pipefail

RUFF_PINNED_VERSION="0.16.9"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

find_ruff() {
    for candidate in ruff "$HOME/.local/bin/ruff"; do
        command -v "$candidate" >/dev/null 2>&1 && { echo "$candidate"; return 0; }
    done
    return 1
}

if ! RUFF="$(find_ruff)"; then
    cat >&2 <<EOF
ruff is not installed. Once:

    pipx install ruff==${RUFF_PINNED_VERSION}

(or 'uv tool install ruff==${RUFF_PINNED_VERSION}'). ruff is a single
binary — it needs no project environment and no project Python.
EOF
    exit 127
fi

FOUND_VERSION="$("$RUFF" --version | awk '{print $2}')"
if [[ "$FOUND_VERSION" != "$RUFF_PINNED_VERSION" ]]; then
    # A WARNING, not an abort: a differing version should not stop anybody
    # from linting. But it has to be VISIBLE, or one puzzles over findings that
    # come purely from the version difference.
    echo "⚠ ruff ${FOUND_VERSION} instead of the pinned ${RUFF_PINNED_VERSION} —" >&2
    echo "  finding counts may differ (RUF100 depends on the rule set)." >&2
fi

cd "$ROOT"
exec "$RUFF" check "$@" src/backend tests
