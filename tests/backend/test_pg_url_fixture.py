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

import ast
import pathlib

import pytest

pytestmark = pytest.mark.unit


_ENV_VAR = "RENFIELD_TEST_PG_URL"


def _reads_the_raw_variable(node: ast.AST) -> bool:
    """Does this expression read `RENFIELD_TEST_PG_URL` from the environment?

    🛑 Three shapes, not one. The first version of this guard matched on the
    TEXT of `ast.Call` nodes and required the substring "environ", which let
    two live idioms through:

        os.environ["RENFIELD_TEST_PG_URL"]   # a Subscript, not a Call
        os.getenv("RENFIELD_TEST_PG_URL")    # a Call, but no "environ" in it

    Both are used in this tree. Measured 2026-10-30: with those escapes, and
    after the by-name exemption below, the guard enforced ZERO files while its
    own docstring said "closed structurally".
    """

    return any(
        isinstance(sub, ast.Constant) and sub.value == _ENV_VAR
        for sub in ast.walk(node)
    )


def _normalises(node: ast.AST) -> bool:
    """Is the sync scheme rewritten to the async one on this expression?"""

    return "postgresql+asyncpg" in ast.unparse(node)


def test_no_engine_is_built_from_the_RAW_environment_variable():
    """The trap, closed on the VALUE that reaches the engine.

    Reading the variable is FINE for a `skipif` mark: marks are evaluated at
    import time and cannot take a fixture, and three files do exactly that
    (`test_document_processing_history`, `test_purge_low_quality_chunks`,
    `test_rag_service_history`) -- none of them builds an engine. What must not
    happen is a file that builds an async engine from the RAW value, because a
    sync `postgresql://` resolves to the psycopg2 dialect, which is not
    installed in the backend image, and the whole file ERRORS.

    Checked on the BODY (`ast`), so this docstring's own mentions do not trip
    it. The companion test below is the negative control: it feeds the guard
    the shapes that used to escape it.
    """

    here = pathlib.Path(__file__).parent
    offenders: list[str] = []
    for path in sorted(here.glob("test_*.py")):
        tree = ast.parse(path.read_text())
        if not _builds_async_engine(tree):
            continue            # a `skipif` gate is legitimate
        offenders.extend(f"{path.name}:{ln}" for ln in _raw_engine_lines(tree))

    assert not offenders, (
        "builds an async engine from the RAW environment variable instead of "
        f"the shared `pg_url` fixture: {offenders}"
    )


def _builds_async_engine(tree: ast.AST) -> bool:

    return any(
        isinstance(n, ast.Call) and "create_async_engine" in ast.unparse(n.func)
        for n in ast.walk(tree)
    )


def _raw_engine_lines(tree: ast.AST) -> list[int]:
    """Lines where `create_async_engine` gets an un-normalised env value.

    Follows the value one assignment deep, which is the shape that escaped
    the text-matching version:

        dsn = os.environ["RENFIELD_TEST_PG_URL"]
        engine = create_async_engine(dsn)
    """

    tainted: set[str] = set()
    for n in ast.walk(tree):
        if not isinstance(n, ast.Assign):
            continue
        if _reads_the_raw_variable(n.value) and not _normalises(n.value):
            for target in n.targets:
                if isinstance(target, ast.Name):
                    tainted.add(target.id)

    bad: list[int] = []
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Call) and "create_async_engine" in ast.unparse(n.func)):
            continue
        for arg in n.args:
            if (isinstance(arg, ast.Name) and arg.id in tainted) or (
                _reads_the_raw_variable(arg) and not _normalises(arg)
            ):
                bad.append(n.lineno)
    return bad


@pytest.mark.parametrize("source", [
    # the two that escaped the text-matching version
    'import os\nfrom x import create_async_engine\n'
    'dsn = os.environ["RENFIELD_TEST_PG_URL"]\nengine = create_async_engine(dsn)\n',
    'import os\nfrom x import create_async_engine\n'
    'dsn = os.getenv("RENFIELD_TEST_PG_URL")\nengine = create_async_engine(dsn)\n',
    # the one it already caught, kept so a rewrite cannot lose it
    'import os\nfrom x import create_async_engine\n'
    'engine = create_async_engine(os.environ.get("RENFIELD_TEST_PG_URL"))\n',
])
def test_the_guard_CATCHES_every_way_of_reading_the_variable(source):
    """Negative control on the instrument itself.

    Each source builds an async engine from the raw variable and MUST be
    reported. Without this, the guard silently stopped enforcing anything.
    """

    tree = ast.parse(source)
    assert _builds_async_engine(tree)
    assert _raw_engine_lines(tree), f"escaped the guard:\n{source}"


@pytest.mark.parametrize("source", [
    # normalised before use -- the legitimate shape
    'import os\nfrom x import create_async_engine\n'
    'dsn = os.environ["RENFIELD_TEST_PG_URL"].replace("postgresql://", "postgresql+asyncpg://", 1)\n'
    'engine = create_async_engine(dsn)\n',
    # reads the variable but builds no engine -- a skipif gate
    'import os\nSKIP = not os.environ.get("RENFIELD_TEST_PG_URL")\n',
])
def test_the_guard_does_NOT_flag_the_legitimate_shapes(source):
    """The other half of the control: no false positives on the real idioms."""

    tree = ast.parse(source)
    assert not _raw_engine_lines(tree), f"false positive on:\n{source}"
