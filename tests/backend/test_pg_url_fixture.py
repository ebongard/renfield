"""The shared `pg_url` fixture normalises the DSN to the async driver.

Why this exists, 2026-10-30: a broad run reported "15 errors" that looked like
a broken environment. The cause was the DSN SCHEME. `RENFIELD_TEST_PG_URL` is
documented as the async form (`postgresql+asyncpg://...`, conftest.py), but a
sync `postgresql://` is easy to hand in -- the container's own `DATABASE_URL`
has exactly that shape. `create_async_engine` then resolves to the psycopg2
dialect, which is not installed in the backend image, and every test in the
file ERRORS with `ModuleNotFoundError: No module named 'psycopg2'`.

Two of the files that read the variable passed it to `create_async_engine`
RAW (`test_pc20260924_restore_indexes.py`,
`test_pc20260927_voice_second_factor.py`) -- those were the two that errored,
13 of the 15. Others carried their own copy of the normalisation.
`conftest._pg_url_or_skip` had the correct version all along; nothing shared it.

The second file matters more than the count: it contains
`TestTheDowngradeIsExercised`. The reverse path was written down and had never
actually run.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


def test_no_engine_is_built_from_the_RAW_environment_variable():
    """The trap, closed structurally -- and the duplication with it.

    The criterion is narrow on purpose, and it took three attempts to get
    right. Reading the variable is FINE for a `skipif` mark: marks are
    evaluated at import time and cannot take a fixture, and three files do
    exactly that (`test_document_processing_history`,
    `test_purge_low_quality_chunks`, `test_rag_service_history`) -- verified:
    none of them builds an engine. What must not happen is a file that reads
    the variable AND builds an async engine, because a sync `postgresql://`
    then resolves to the psycopg2 dialect, which is not installed in the
    backend image, and the whole file ERRORS with `ModuleNotFoundError`.

    My earlier attempts: the broad one flagged nine innocent files plus itself;
    the next one produced a FALSE POSITIVE on a file that reassigned the
    normalised value before use. Both were the measuring instrument, not the
    code.

    Checked on the BODY (`ast`), so this docstring's own mentions do not trip
    it.
    """
    import ast
    import pathlib

    here = pathlib.Path(__file__).parent
    offenders: list[str] = []
    for path in sorted(here.glob("test_*.py")):
        tree = ast.parse(path.read_text())
        builds_engine = any(
            isinstance(n, ast.Call) and "create_async_engine" in ast.unparse(n.func)
            for n in ast.walk(tree)
        )
        if not builds_engine:
            continue            # a `skipif` gate is legitimate
        if path.name == "test_kb_shares_service.py":
            # MEASURED EXCEPTION, 2026-10-30. This file keeps its own copy. I
            # converted it to `pg_url` and `test_revoke_kb_share_rowcount_is_
            # per_document` then FAILED when run after `test_models.py`, while
            # passing alone -- adding the fixture parameter changes fixture
            # instantiation order, and the harness's cleanup appears to land
            # after this file's own schema setup. Reverted; the interaction is
            # its own item. The file carries the correct normalisation, so it
            # does not have the defect this check is about.
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            src = ast.unparse(node)
            if "RENFIELD_TEST_PG_URL" in src and "environ" in src:
                offenders.append(f"{path.name}:{node.lineno}")

    assert not offenders, (
        "builds an async engine AND reads RENFIELD_TEST_PG_URL from the "
        "environment. Take the `pg_url` fixture: a sync DSN there ERRORS the "
        f"whole file with ModuleNotFoundError: psycopg2. {offenders}")
