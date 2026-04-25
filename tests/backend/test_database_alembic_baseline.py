"""
Regression guards for `_ensure_alembic_baseline` in services/database.py.

Background — prod incident 2026-04-25 (Reva submodule bump):
    The head revision `pc20260426_paperless_upload_tracking` (38 chars)
    crashed with StringDataRightTruncationError when inserted into the
    default VARCHAR(32) `alembic_version.version_num` column on a fresh
    Reva DB.

    PR #462 fixed the column width in `alembic/env.py` for the alembic
    upgrade flow. PR #477 fixed the same bug in `services/database.py`
    for the SQLAlchemy `create_all` bootstrap path.

The function has three execution paths through its main `if/else`:
    A. Table exists + has row    → early return; no INSERT
    B. Table absent              → CREATE w/ VARCHAR(64), then INSERT
    C. Table exists, empty       → falls through; widen ALTER, then INSERT

These tests guard against the two width-related regressions:
  1. CREATE statement must declare VARCHAR(64), not VARCHAR(32)  (path B)
  2. Idempotent widen ALTER must run before INSERT               (path C)

Source-file inspection is used (rather than runtime invocation) because
the function is Postgres-only — it uses `DO $$ ... $$` blocks and
`information_schema`. SQLite test engines don't enforce VARCHAR length,
so a runtime check would pass even with the bug present. Reading the
source also avoids importing `services.database` — that module triggers
SQLAlchemy engine creation and asyncpg import at module load.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DATABASE_PY = REPO_ROOT / "src" / "backend" / "services" / "database.py"


def _baseline_function_source() -> str:
    """Return only the body of `_ensure_alembic_baseline` from the source file.

    Slices from `async def _ensure_alembic_baseline` to the next top-level
    `async def` so unrelated functions in the same file can't pass the
    string assertions by accident.
    """
    src = DATABASE_PY.read_text()
    start = src.index("async def _ensure_alembic_baseline")
    rest = src[start + len("async def _ensure_alembic_baseline"):]
    end = rest.index("\nasync def ")
    return rest[:end]


@pytest.mark.unit
def test_ensure_alembic_baseline_create_uses_varchar_64():
    """CREATE TABLE alembic_version must declare VARCHAR(64) for version_num.

    Path B (table absent) regression — VARCHAR(32) is too narrow for
    Renfield revision IDs which run up to ~40 characters.
    """
    body = _baseline_function_source()
    # Match the literal SQL declaration so comments mentioning the legacy
    # width (e.g. "...with a pre-existing VARCHAR(32) column") don't
    # falsely trip the assertion.
    forbidden = '"version_num VARCHAR(32) NOT NULL'
    required = '"version_num VARCHAR(64) NOT NULL'
    assert forbidden not in body, (
        "_ensure_alembic_baseline CREATE must NOT declare "
        "version_num VARCHAR(32) — Renfield revision IDs run up to "
        "~40 chars (see PR #477)"
    )
    assert required in body, (
        "_ensure_alembic_baseline CREATE must declare "
        "version_num VARCHAR(64)"
    )


@pytest.mark.unit
def test_ensure_alembic_baseline_widens_before_insert():
    """An idempotent widen ALTER must run before INSERT.

    Path C (table exists but empty) regression — without this ALTER the
    INSERT crashes when an existing alembic_version table still carries
    the legacy VARCHAR(32) column. Mirrors `alembic/env.py` from PR #462
    so both creation paths converge on the wider column.
    """
    body = _baseline_function_source()
    widen_idx = body.find("ALTER COLUMN version_num TYPE VARCHAR(64)")
    insert_idx = body.find("INSERT INTO alembic_version")

    assert widen_idx >= 0, (
        "_ensure_alembic_baseline must include an idempotent "
        "ALTER COLUMN widen to VARCHAR(64) (path C protection)"
    )
    assert insert_idx >= 0, (
        "_ensure_alembic_baseline must execute INSERT INTO alembic_version"
    )
    assert widen_idx < insert_idx, (
        "Widen ALTER must run BEFORE the INSERT — otherwise the INSERT "
        "still crashes when the column was VARCHAR(32)"
    )
