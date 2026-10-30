#!/bin/sh
# Run the backend suite inside the container against the TEST database.
# The connection string is derived here, in the container, from DATABASE_URL --
# it never passes through the session.
set -e
ROOT="$1"; shift
RENFIELD_TEST_PG_URL=$(python - <<'PY'
import os
u = os.environ["DATABASE_URL"]
base, _, _ = u.rpartition("/")
# The documented form is the ASYNC scheme (conftest.py:390). Six test files
# call create_async_engine() with this value RAW -- hand them "postgresql://"
# and they die with ModuleNotFoundError: psycopg2. That produced 15 ERRORS in
# the migration tests on 2026-10-30 and looked like a pre-existing defect.
print((base + "/renfield_test").replace("postgresql://", "postgresql+asyncpg://", 1))
PY
)
export RENFIELD_TEST_PG_URL
echo "TEST_PG_URL gesetzt: $(printf '%s' "$RENFIELD_TEST_PG_URL" | sed 's#//[^@]*@#//<verdeckt>@#')"
cd "$ROOT/src/backend"
PYTHONPATH="$ROOT/src/backend" exec python -m pytest "$@"
