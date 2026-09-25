"""
Regression guards for `_stamp_pre_baseline` in services/database.py.

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

# Resolve the backend source from the importable package: in the container
# the test tree is mounted at /tests but the backend source is at /app
# (no src/backend prefix), so Path(__file__).parents[2] resolves to "/".
import services.database as _database_module

DATABASE_PY = Path(_database_module.__file__).resolve()


def _baseline_function_source() -> str:
    """Return only the body of `_stamp_pre_baseline` from the source file.

    Slices from `async def _stamp_pre_baseline` to the next top-level
    `async def` so unrelated functions in the same file can't pass the
    string assertions by accident.
    """
    src = DATABASE_PY.read_text()
    start = src.index("async def _stamp_pre_baseline")
    rest = src[start + len("async def _stamp_pre_baseline"):]
    end = rest.index("\nasync def ")
    return rest[:end]


@pytest.mark.unit
def test_stamp_pre_baseline_create_uses_varchar_64():
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
        "_stamp_pre_baseline CREATE must NOT declare "
        "version_num VARCHAR(32) — Renfield revision IDs run up to "
        "~40 chars (see PR #477)"
    )
    assert required in body, (
        "_stamp_pre_baseline CREATE must declare "
        "version_num VARCHAR(64)"
    )


@pytest.mark.unit
def test_stamp_pre_baseline_widens_before_insert():
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
        "_stamp_pre_baseline must include an idempotent "
        "ALTER COLUMN widen to VARCHAR(64) (path C protection)"
    )
    assert insert_idx >= 0, (
        "_stamp_pre_baseline must execute INSERT INTO alembic_version"
    )
    assert widen_idx < insert_idx, (
        "Widen ALTER must run BEFORE the INSERT — otherwise the INSERT "
        "still crashes when the column was VARCHAR(32)"
    )


# ---------------------------------------------------------------------------
# Die Basis-Migration und ihr Stempel (#B3)
# ---------------------------------------------------------------------------
class TestBaselineStamp:
    """`PRE_BASELINE_REVISION` MUSS die Vorgaengerrevision der Basis sein.

    🛑 Stimmt das nicht, wird die Basis auf einer frischen Datenbank
    UEBERSPRUNGEN — und alles ist wieder wie vorher: das Roh-SQL aus den
    Migrationen entsteht nie, und niemand merkt es, weil nichts kaputtgeht.
    Es wird nur langsam. Genau dieser Fehlermodus hat neun Indizes auf beiden
    Instanzen gekostet, fuenf davon HNSW.

    Ein blosser Vergleich zweier Zeichenketten waere wertlos: der Test LIEST
    die Basis-Migration und nimmt ihr `down_revision`.
    """

    @staticmethod
    def _baseline_module():
        import importlib.util

        root = Path(_database_module.__file__).resolve().parents[1]
        path = root / "alembic" / "versions" / "pc20260926_schema_baseline.py"
        assert path.exists(), f"Basis-Migration fehlt: {path}"
        spec = importlib.util.spec_from_file_location("baseline_under_test", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_the_stamp_is_the_baselines_parent(self):
        from services.database import PRE_BASELINE_REVISION

        mod = self._baseline_module()
        assert mod.down_revision == PRE_BASELINE_REVISION, (
            "Der Stempel zeigt nicht auf die Vorgaengerrevision der Basis — eine "
            "frische Datenbank wuerde die Basis ueberspringen und das Roh-SQL "
            "nie bekommen."
        )

    def test_the_baseline_is_the_head(self):
        # Zeigt eine spaetere Migration auf die Basis, ist das in Ordnung — sie
        # laeuft auf einer Neuinstallation ganz normal MIT. Der Stempel darf
        # aber niemals auf die Basis selbst oder dahinter zeigen.
        from services.database import PRE_BASELINE_REVISION

        mod = self._baseline_module()
        assert PRE_BASELINE_REVISION != mod.revision

    def test_the_guard_names_a_table_that_exists_everywhere(self):
        # Der Waechter entscheidet an EINER Tabelle, ob das Schema schon da ist.
        # Waehlt jemand eine, die es auf einer aelteren Instanz nicht gibt, liefe
        # die Basis dort an und versuchte, ein vorhandenes Schema neu zu bauen.
        mod = self._baseline_module()
        assert mod.SENTINEL_TABLE == "users"
