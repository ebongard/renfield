"""Create and drop a run's OWN test database -- nothing else, ever.

Called by ``bin/run-backend-tests.sh`` when ``RENFIELD_TEST_DB_SUFFIX`` is set,
so that concurrent runs need not queue behind the shared ``renfield_test``:

    python throwaway_test_db.py create renfield_test_<suffix>   -> prints a token
    python throwaway_test_db.py drop   renfield_test_<suffix> <token>

The connection is derived here, in the container, from ``DATABASE_URL`` -- the
maintenance database ``postgres`` on the same server. No DSN is ever printed.

Three rules, each enforced in THIS file and not only by the caller:

1. The name must be ``renfield_test_<suffix>`` (lowercase letters, digits,
   underscore; no ``prod``/``live``). Never ``renfield_test`` itself, never the
   application database, never a numbered sibling like ``renfield_test2``.
2. ``create`` refuses a database that already exists. It is neither reused nor
   dropped: somebody else's run may be in it. A new one gets the ``vector``
   extension, as ``renfield_test`` has it.
3. ``drop`` only removes a database that carries the token ``create`` wrote
   into its comment in the same invocation. A matching NAME is not ownership.
   The one exception is spelled out at ``_drop_unmarked``: a database this very
   call created and could not mark.

What can be left behind (a leak, never a wrong drop): a SIGKILL, a container
stop, or an interrupt that lands between CREATE and the moment the caller holds
the token. The orphan blocks its suffix (exit 3) until it is removed by hand;
its comment identifies it.

Exit codes: 0 done, 2 refused (name / usage), 3 the database already exists,
4 not ours (token mismatch), 1 anything else.
"""
from __future__ import annotations

import asyncio
import os
import re
import secrets
import sys

# Stricter than the harness guard (`_TEST_DB_NAME_RE` in tests/backend/
# conftest.py), and a subset of it: every name accepted here is accepted there.
# 14 characters of prefix + at most 40 of suffix stays under Postgres' 63.
NAME_RE = re.compile(r"renfield_test_(?![a-z0-9_]*(?:prod|live))[a-z0-9][a-z0-9_]{0,39}")
TOKEN_RE = re.compile(r"[a-f0-9]{32}")
MARK = "throwaway test database, run token "

EXIT_REFUSED = 2
EXIT_EXISTS = 3
EXIT_NOT_OURS = 4


class Refused(Exception):
    def __init__(self, message: str, code: int = EXIT_REFUSED) -> None:
        super().__init__(message)
        self.code = code


def _database_of(url: str) -> str:
    """The database name of a DSN, parsed the way the driver parses it.

    The same parser the harness guard uses (`_db_name_only` in tests/backend/
    conftest.py), for the same reason: a slash inside the query string
    (`/renfield_test_x?options=a/b`) must not make this read the wrong name.
    A trailing slash is not part of the name either. A DSN that cannot be
    parsed is refused -- unknown is not "different".
    """
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import ArgumentError

    try:
        return (make_url(url).database or "").rstrip("/")
    except ArgumentError:
        raise Refused("DATABASE_URL cannot be parsed.") from None


def require_throwaway_name(name: str) -> None:
    """The one place that decides whether a name may be put into SQL."""
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise Refused(
            f"{name!r} is not a throwaway test database name "
            "(renfield_test_<suffix>: lowercase letters, digits, underscore; "
            "no 'prod', no 'live'; suffix at most 40 characters)."
        )


def check_name(name: str, application_url: str) -> None:
    require_throwaway_name(name)
    if name == _database_of(application_url):
        raise Refused(f"{name!r} is the application database.")


_MARK_SQL = (
    "SELECT shobj_description(oid, 'pg_database') AS mark "
    "FROM pg_database WHERE datname = $1"
)


async def _drop_unmarked(conn, name: str) -> None:
    """Remove the database `create` made a moment ago and could not mark.

    The only DROP in this file without a token, so here is exactly why it
    cannot hit a database of somebody else:

    - It is called from ONE place, after `CREATE DATABASE` for this name
      succeeded in the same call. A database that existed before would have
      made that CREATE fail, and the call would have ended as "already exists".
    - Nobody else can take the name in between: every other `create` is refused
      while it exists, and every `drop` needs a token this database never got.
    - And, checked rather than argued: it is dropped only while its comment is
      still EMPTY. A database carrying any mark -- a run's token included -- is
      left alone.

    Without this, the unmarked database would block its suffix for good: no
    token could ever drop it.
    """
    require_throwaway_name(name)
    row = await conn.fetchrow(_MARK_SQL, name)
    if row is None or row["mark"] is not None:
        return
    await conn.execute(f'DROP DATABASE "{name}" WITH (FORCE)')


async def create(conn, name: str) -> str:
    """Create the database and mark it as this invocation's. Returns the token."""
    from asyncpg.exceptions import DuplicateDatabaseError, UniqueViolationError

    # Checked HERE, where the SQL is built -- not only by whoever calls this.
    # After it, nothing in the name needs quoting or escaping.
    require_throwaway_name(name)
    token = secrets.token_hex(16)
    try:
        await conn.execute(f'CREATE DATABASE "{name}"')
    except (DuplicateDatabaseError, UniqueViolationError):
        # UniqueViolation is the same answer from a LOST RACE: two creates of
        # one name at the same instant both pass the existence check, and the
        # loser fails on the unique index of pg_database (23505) instead of
        # with duplicate_database (42P04).
        raise Refused(
            f"{name} already exists. It is neither reused nor dropped -- another "
            "run may be using it. Pick another suffix.",
            EXIT_EXISTS,
        ) from None
    try:
        await conn.execute(f"COMMENT ON DATABASE \"{name}\" IS '{MARK}{token}'")
    except Exception:
        # A statement that FAILED: the connection is usable, clean up. Not
        # BaseException -- after a cancellation or Ctrl-C the connection is in
        # an unknown state, and a second statement on it could fail and replace
        # the interrupt; that case leaves an orphan (see the module docstring).
        try:
            await _drop_unmarked(conn, name)
        except Exception as cleanup_error:
            print(
                f"WARNING: {name} was created but could neither be marked nor "
                f"removed ({type(cleanup_error).__name__}) -- remove it by hand.",
                file=sys.stderr,
            )
        raise
    return token


async def drop(conn, name: str, token: str) -> bool:
    """Drop the database if it carries `token`. False when it is already gone."""
    require_throwaway_name(name)
    if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
        raise Refused("malformed run token.")
    row = await conn.fetchrow(_MARK_SQL, name)
    if row is None:
        return False
    if row["mark"] != MARK + token:
        raise Refused(
            f"{name} was not created by this invocation (its mark does not carry "
            "this run's token). Not dropped.",
            EXIT_NOT_OURS,
        )
    # FORCE: an interrupted pytest can leave connections behind, and a plain
    # DROP would then fail with "is being accessed by other users".
    await conn.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
    return True


async def install_extensions(conn) -> None:
    """Give the new database what the shared one has had for ages.

    The harness installs `vector` itself, but only when the first test that
    asks for the `async_engine` fixture builds the schema. The migration tests
    work in throwaway schemas straight off `pg_url` and find `vector` through
    `public` -- in `renfield_test` because an earlier run left it there. On a
    database that is seconds old they failed with `type "vector" does not
    exist` (measured 2026-11-16: 8 of the first 9 tests of
    test_pc20260423_migration.py).
    """
    await conn.execute("CREATE EXTENSION IF NOT EXISTS vector SCHEMA public")


async def _connect(application_url: str, database: str = "postgres"):
    import asyncpg

    dsn = application_url.replace("postgresql+asyncpg://", "postgresql://", 1)
    return await asyncpg.connect(dsn=dsn, database=database, timeout=30)


async def create_ready(conn, name: str, application_url: str) -> str:
    """`create` plus the extensions -- or no database at all."""
    token = await create(conn, name)
    try:
        inside = await _connect(application_url, name)
        try:
            await install_extensions(inside)
        finally:
            await inside.close()
    except BaseException:
        # BaseException on purpose, and always re-raised: an interrupt while the
        # extension is being installed must not leave a database whose token
        # the caller never received. Unlike in `create`, the cleanup is safe to
        # attempt -- `conn` is the idle maintenance connection, not the one the
        # interrupt hit, and `drop` is gated by the token just written. A
        # half-prepared database would only block the suffix for the next run.
        try:
            await drop(conn, name, token)
        except Exception as cleanup_error:
            print(
                f"WARNING: {name} could not be removed again "
                f"({type(cleanup_error).__name__}) -- remove it by hand.",
                file=sys.stderr,
            )
        raise
    return token


async def _main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[0] not in ("create", "drop") or len(argv) != (2 if argv[0] == "create" else 3):
        raise Refused("usage: throwaway_test_db.py create <name> | drop <name> <token>")
    action, name = argv[0], argv[1]
    application_url = os.environ.get("DATABASE_URL", "")
    if not application_url:
        raise Refused("DATABASE_URL is not set.")
    check_name(name, application_url)

    conn = await _connect(application_url)
    try:
        if action == "create":
            print(await create_ready(conn, name, application_url))
        elif await drop(conn, name, argv[2]):
            print(f"dropped {name}", file=sys.stderr)
        else:
            print(f"{name} is already gone", file=sys.stderr)
    finally:
        await conn.close()
    return 0


def main(argv: list[str]) -> int:
    try:
        return asyncio.run(_main(argv))
    except Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return exc.code
    except Exception as exc:
        # A message from the SERVER ("permission denied to create database")
        # names no credential and is what the reader needs. Anything else --
        # a connection error may quote the DSN -- is reduced to its type.
        from asyncpg import PostgresError

        detail = f": {exc}" if isinstance(exc, PostgresError) else ""
        print(f"ERROR: {type(exc).__name__} while talking to Postgres{detail}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
