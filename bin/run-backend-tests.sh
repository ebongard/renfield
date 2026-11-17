#!/bin/sh
# Run the backend suite inside the container against the TEST database.
# The connection string is derived here, in the container, from DATABASE_URL --
# it never passes through the session.
#
#   run-backend-tests.sh <root> [pytest args]
#       the shared `renfield_test` -- one run at a time: hold the lock
#       directory /tmp/renfield-testdb.lock on the build box for the run.
#   RENFIELD_TEST_DB_SUFFIX=<name> run-backend-tests.sh <root> [pytest args]
#       a database of this run's own, `renfield_test_<name>`: created before
#       the run and dropped after it -- also when pytest fails or the run is
#       signalled. Takes no lock; concurrent runs each pick their own suffix.
set -e
ROOT="$1"; shift

TEST_DB=renfield_test
SUFFIX="${RENFIELD_TEST_DB_SUFFIX:-}"
if [ -n "$SUFFIX" ]; then
  # Checked HERE, before anything talks to Postgres, and again in
  # throwaway_test_db.py. Lowercase letters, digits, underscore -- the form the
  # harness guard accepts (`_TEST_DB_NAME_RE` in tests/backend/conftest.py),
  # which also refuses a suffix naming a live role.
  # 🛑 Every character spelled out, no `a-z`: a RANGE follows the locale's
  # collation, and where `sh` is bash under a UTF-8 locale `[!a-z0-9_]` lets
  # `Upper` and `ä` through (dash refuses them in every locale). A list means
  # the same thing in every shell and every locale.
  case "$SUFFIX" in
    *[!abcdefghijklmnopqrstuvwxyz0123456789_]*|_*|*prod*|*live*)
      echo "REFUSED: RENFIELD_TEST_DB_SUFFIX must be lowercase letters, digits and underscore, start with a letter or digit, and contain neither 'prod' nor 'live'." >&2
      exit 2 ;;
  esac
  if [ "${#SUFFIX}" -gt 40 ]; then
    echo "REFUSED: RENFIELD_TEST_DB_SUFFIX is longer than 40 characters." >&2
    exit 2
  fi
  TEST_DB="renfield_test_$SUFFIX"
fi

RENFIELD_TEST_PG_URL=$(TEST_DB="$TEST_DB" python - <<'PY'
import os
u = os.environ["DATABASE_URL"]
base, _, _ = u.rpartition("/")
# The documented form is the ASYNC scheme (conftest.py:390). Six test files
# call create_async_engine() with this value RAW -- hand them "postgresql://"
# and they die with ModuleNotFoundError: psycopg2. That produced 15 ERRORS in
# the migration tests on 2026-10-30 and looked like a pre-existing defect.
print((base + "/" + os.environ["TEST_DB"]).replace("postgresql://", "postgresql+asyncpg://", 1))
PY
)
export RENFIELD_TEST_PG_URL
echo "TEST_PG_URL set: $(printf '%s' "$RENFIELD_TEST_PG_URL" | sed 's#//.*@#//<redacted>@#')"
cd "$ROOT/src/backend"

# NEVER pipe this through `tail -N` or `grep` at the call site. On 2026-10-30
# that discarded the very thing being measured seven times in one session: the
# summary line and the `-rE` error list sit ABOVE pytest's teardown noise, so a
# fixed-size tail shows warnings and hides the result. Capture the whole output
# and search it afterwards.
# 🛑 `-o asyncio_mode=auto` is MANDATORY and belongs here, not at the call
# site. `asyncio_mode` lives in pyproject.toml, and the documented rsync set
# for the build box (src/backend + tests + bin + config + k8s) does NOT
# include it -- so in the isolated copy pytest finds no ini file, falls back
# to strict, and every unmarked async test errors. That is the whole reason
# this script exists: to standardise the invocation.
if [ -z "$SUFFIX" ]; then
  PYTHONPATH="$ROOT/src/backend" exec python -m pytest \
    -o asyncio_mode=auto -p no:cacheprovider "$@"
fi

# --- A database of this run's own -------------------------------------------
# throwaway_test_db.py creates the database, runs the very same
# `python -m pytest` as its child and drops the database when pytest is dead --
# also when it failed or a signal came. It is `exec`ed, so signals reach it
# directly and pytest inherits stdin and the signal dispositions exactly as in
# the branch above. No traps here on purpose: a shell knows a reaped child only
# by its pid and would signal whoever holds that number (see the helper).
#
# What differs from the branch above, on a signal only:
# - SIGINT to this process alone (not Ctrl-C at a terminal, which reaches
#   pytest too) reaches pytest as SIGTERM after the grace period
#   (RENFIELD_TEST_STOP_GRACE seconds, default 10): no interrupt summary.
# - A SIGKILL, or a stop of the container, leaves the database behind. It then
#   blocks its suffix until it is removed by hand.
PYTHONPATH="$ROOT/src/backend" exec python "$ROOT/bin/throwaway_test_db.py" run "$TEST_DB" -- \
  -o asyncio_mode=auto -p no:cacheprovider "$@"
