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
#       the run and dropped after it -- also when pytest fails or the script is
#       interrupted. Takes no lock; concurrent runs each pick their own suffix.
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
# No `exec` from here on: the shell has to outlive pytest to drop the database.
#
# Two differences from the `exec` branch remain, both on a signal only:
# - SIGINT to the SCRIPT alone (not Ctrl-C at a terminal, which reaches pytest
#   too) does not reach pytest as an interrupt: after the grace period pytest
#   gets SIGTERM and prints no summary.
# - A SIGKILL of this script, or a stop of the container, leaves the database
#   behind. It then blocks its suffix until it is removed by hand.
ADMIN="$ROOT/bin/throwaway_test_db.py"
RUN_TOKEN=""
PYTEST_DONE=""
# Seconds pytest gets to end before the next, harder signal.
STOP_GRACE="${RENFIELD_TEST_STOP_GRACE:-10}"

# Returns only when pytest is DEAD and reaped -- `wait` says so, not a guess.
#
# `$!` rather than a variable of our own: the shell sets it the moment it
# forks, and it is empty until then (pytest is the first background command of
# this script). So there is no window between starting pytest and knowing its
# pid in which a signal would find nothing to stop -- and would drop the
# database under a pytest that is still running.
#
# $1 = "wait": pytest gets the grace period BEFORE the first signal. Ctrl-C at
# a terminal has already reached it; let it print its interrupt summary.
# Either way the escalation ends in SIGKILL: a test may have installed its own
# SIGTERM handler, or hang in one.
stop_pytest() {
  [ -z "$PYTEST_DONE" ] && [ -n "$!" ] || return 0
  pid=$!
  PYTEST_DONE=1
  if [ "${1:-}" = wait ]; then
    ( sleep "$STOP_GRACE"; kill -TERM "$pid"; sleep "$STOP_GRACE"; kill -KILL "$pid" ) \
      </dev/null >/dev/null 2>&1 &
  else
    kill -TERM "$pid" 2>/dev/null || true
    ( sleep "$STOP_GRACE"; kill -KILL "$pid" ) </dev/null >/dev/null 2>&1 &
  fi
  watchdog=$!
  wait "$pid" 2>/dev/null || true
  # Before it can fire at a pid that is no longer pytest's. SIGKILL, not the
  # default: the watchdog is a subshell started from inside a signal trap that
  # has just set TERM to "ignore", it inherits that, and a plain `kill` left it
  # sitting there for the whole grace period (measured). Its `sleep` child
  # lives on for at most that long, holds none of our descriptors and signals
  # nobody.
  kill -KILL "$watchdog" 2>/dev/null || true
  wait "$watchdog" 2>/dev/null || true
}

# Drops ONLY what this invocation created: RUN_TOKEN is empty until `create`
# succeeded, and throwaway_test_db.py compares it with the mark on the database
# before it drops anything. Cleared first, so the EXIT trap that follows a
# signal trap cannot drop twice. NEVER while pytest is alive: whatever path
# led here, pytest is stopped first.
drop_own_database() {
  stop_pytest
  [ -n "$RUN_TOKEN" ] || return 0
  token="$RUN_TOKEN"; RUN_TOKEN=""
  python "$ADMIN" drop "$TEST_DB" "$token" </dev/null \
    || echo "WARNING: $TEST_DB was NOT dropped -- remove it by hand." >&2
}

# A shell runs a signal trap only once its foreground child has returned, so
# pytest runs in the background and the script sits in `wait`, which a signal
# interrupts at once.
on_signal() {
  trap '' INT TERM HUP QUIT PIPE
  stop_pytest "${2:-}"
  exit "$1"
}
trap drop_own_database EXIT
trap 'on_signal 130 wait' INT
trap 'on_signal 143' TERM
trap 'on_signal 129' HUP
trap 'on_signal 131' QUIT
# A reader that closed our stdout (`| head`) must not end the script at an
# `echo` before the EXIT trap could drop the database.
trap 'on_signal 141' PIPE

# `create` refuses a database that already exists (exit 3): it is neither
# reused nor dropped, and RUN_TOKEN stays empty.
RUN_TOKEN=$(python "$ADMIN" create "$TEST_DB" </dev/null)
echo "TEST_DB: $TEST_DB (created for this run, dropped when it ends)"

# A background command of a non-interactive shell starts with SIGINT and
# SIGQUIT IGNORED and with /dev/null as stdin. Python then never installs its
# KeyboardInterrupt handler, and `--pdb` has nothing to read from. Put all
# three back, so the tests run as they do in the `exec` branch above:
# the launcher restores the signals, fd 3 carries the script's own stdin past
# the `&`. `pytest.main()` reads sys.argv like `-m pytest`.
exec 3<&0
PYTHONPATH="$ROOT/src/backend" python -c 'import signal, sys, pytest
signal.signal(signal.SIGINT, signal.default_int_handler)
signal.signal(signal.SIGQUIT, signal.SIG_DFL)
sys.exit(pytest.main())' \
  -o asyncio_mode=auto -p no:cacheprovider "$@" <&3 3<&- &
exec 3<&-
set +e
wait "$!"
rc=$?
set -e
PYTEST_DONE=1
exit "$rc"
