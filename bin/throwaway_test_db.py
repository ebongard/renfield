"""Run pytest on a database of the run's OWN -- create it, drop it, nothing else.

Called by ``bin/run-backend-tests.sh`` when ``RENFIELD_TEST_DB_SUFFIX`` is set,
so that concurrent runs need not queue behind the shared ``renfield_test``:

    python throwaway_test_db.py run renfield_test_<suffix> [--workers N] -- <pytest args>

creates the database, runs ``python -m pytest <args>`` as a child, and drops
the database when pytest has ended -- also when it failed or a signal came.
With ``--workers N`` it creates ``renfield_test_<suffix>_gw0 .. _gw<N-1>``
instead, under ONE run token, runs ``pytest -n N`` (pytest-xdist) with
``RENFIELD_TEST_DB_PER_WORKER=1``, and drops all of them; each worker's harness
swaps its id into the URL (tests/backend/conftest.py). If one of them cannot be
created, the ones already made are dropped and the run does not start (#1577).

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

Why pytest is supervised HERE and not by traps in the shell script: a shell
reaps a finished child on its own and then knows it only by a NUMBER, so any
``kill`` it sends after that can reach whoever holds the number now (and a job
spec, ``kill %1``, signals nothing in dash without job control -- measured).
``subprocess.Popen`` keeps the child unreaped until this process waits for it,
and refuses to signal one it has already reaped. "Is it still ours" and "signal
it" are one step here. The token never leaves this process either.

Not stopped before the drop: processes pytest itself started (it is signalled
by pid, not as a group, so that a terminal's Ctrl-C and `--pdb` work as usual).
``WITH (FORCE)`` cuts their connections and the database cannot come back.

What can be left behind (a leak, never a wrong drop): a SIGKILL of this
process, or a stop of the container. The orphan blocks its suffix (exit 3)
until it is removed by hand; its comment identifies it.

Exit codes: pytest's own; 128+N when signal N ended pytest; 2 refused (name /
usage / grace period), 3 the database already exists, 1 anything else --
including a green run whose database could not be dropped.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import re
import secrets
import signal
import subprocess
import sys
import time

# Stricter than the harness guard (`_TEST_DB_NAME_RE` in tests/backend/
# conftest.py), and a subset of it: every name accepted here is accepted there.
# 14 characters of prefix + at most 40 of suffix stays under Postgres' 63.
NAME_RE = re.compile(r"renfield_test_(?![a-z0-9_]*(?:prod|live))[a-z0-9][a-z0-9_]{0,39}")
TOKEN_RE = re.compile(r"[a-f0-9]{32}")
MARK = "throwaway test database, run token "

EXIT_REFUSED = 2
EXIT_EXISTS = 3
EXIT_NOT_OURS = 4

# Seconds pytest gets to end before the next, harder signal.
STOP_GRACE_ENV = "RENFIELD_TEST_STOP_GRACE"
STOP_GRACE_DEFAULT = 10
# A drop that takes longer than this is reported, not waited for: once a signal
# has come, nothing but SIGKILL could end this process otherwise.
DROP_TIMEOUT_SECONDS = 120
# The same command the script's `exec` branch runs on the shared database.
PYTEST_COMMAND = [sys.executable, "-m", "pytest"]


class Refused(Exception):
    def __init__(self, message: str, code: int = EXIT_REFUSED) -> None:
        super().__init__(message)
        self.code = code


class DropTimedOut(Exception):
    """The server did not finish the drop in time; the database may still exist."""


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


async def create(conn, name: str, token: str | None = None) -> str:
    """Create the database and mark it as this invocation's. Returns the token
    -- a new one, or `token` for a further database of the same run."""
    from asyncpg.exceptions import DuplicateDatabaseError, UniqueViolationError

    # Checked HERE, where the SQL is built -- not only by whoever calls this.
    # After it, nothing in the name needs quoting or escaping -- the token is
    # checked as well, since it goes into the COMMENT.
    require_throwaway_name(name)
    if token is None:
        token = secrets.token_hex(16)
    elif not TOKEN_RE.fullmatch(token):
        raise Refused("malformed run token.")
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
    try:
        row = await asyncio.wait_for(conn.fetchrow(_MARK_SQL, name), DROP_TIMEOUT_SECONDS)
    except TimeoutError:
        raise DropTimedOut(f"reading the mark of {name} did not finish within {DROP_TIMEOUT_SECONDS} s") from None
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
    try:
        await asyncio.wait_for(conn.execute(f'DROP DATABASE "{name}" WITH (FORCE)'), DROP_TIMEOUT_SECONDS)
    except TimeoutError:
        raise DropTimedOut(f"dropping {name} did not finish within {DROP_TIMEOUT_SECONDS} s") from None
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
    await _prepare_or_drop(conn, name, application_url, token)
    return token


async def _create_ready_with(conn, name: str, application_url: str, token: str) -> None:
    """`create_ready` for a further database of the same run, under its token."""
    await create(conn, name, token)
    await _prepare_or_drop(conn, name, application_url, token)


async def _prepare_or_drop(conn, name: str, application_url: str, token: str) -> None:
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


def _describe(exc: BaseException) -> str:
    """What may be printed about an error: never a DSN.

    A message from the SERVER ("permission denied to create database") names no
    credential and is what the reader needs; so does our own timeout. Anything
    else -- a connection error may quote the DSN -- is reduced to its type.
    """
    from asyncpg import PostgresError

    if isinstance(exc, (PostgresError, DropTimedOut)):
        return f"{type(exc).__name__}: {exc}"
    return type(exc).__name__


def _say(text: str, stream=None) -> None:
    """Print without ever dying of it: a reader that closed our output
    (`| head`) must not end this process before the database is dropped."""
    stream = stream or sys.stderr
    try:
        print(text, file=stream, flush=True)
    except OSError:
        # Nobody is listening. Point the descriptor somewhere harmless so the
        # interpreter's own flush at exit does not fail either.
        with open(os.devnull, "w") as devnull:
            os.dup2(devnull.fileno(), stream.fileno())


def stop_grace() -> int:
    raw = os.environ.get(STOP_GRACE_ENV) or str(STOP_GRACE_DEFAULT)
    if not re.fullmatch(r"[0-9]{1,4}", raw):
        raise Refused(f"{STOP_GRACE_ENV} must be a whole number of seconds (0-9999), not {raw!r}.")
    return int(raw)


def pytest_url(application_url: str, name: str) -> str:
    """The URL pytest gets: the application's server and credentials, THIS database.

    Built here from the validated name and handed to the child explicitly, so
    pytest cannot run anywhere but in the database just created -- whatever
    RENFIELD_TEST_PG_URL the caller's environment carries. The async scheme is
    the documented form (see run-backend-tests.sh). Never printed.
    """
    from sqlalchemy.engine import make_url

    require_throwaway_name(name)
    url = make_url(application_url).set(drivername="postgresql+asyncpg", database=name)
    return url.render_as_string(hide_password=False)


def supervise(pytest_args: list[str], grace: int, received: list[int], env: dict[str, str] | None = None) -> int:
    """Run pytest as a child and return only when it is DEAD and reaped.

    `received` is filled by the signal handlers of `run`. On SIGINT pytest gets
    the grace period BEFORE the first signal: Ctrl-C at a terminal has already
    reached it, let it print its interrupt summary. Any other signal is passed
    on as SIGTERM at once. Either way the escalation ends in SIGKILL -- a test
    may have installed its own SIGTERM handler, or hang in one.

    `Popen` signals only a child it has not reaped yet, so none of this can
    reach a process that merely inherited pytest's pid.
    """
    if received:
        return 128 + received[0]  # came during the create: pytest is not started
    # stdin, stdout and stderr are inherited: `--pdb` works, as in the `exec` branch.
    proc = subprocess.Popen([*PYTEST_COMMAND, *pytest_args], env=env)
    terminate_at = kill_at = None
    try:
        while True:
            try:
                proc.wait(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                pass
            now = time.monotonic()
            if received and terminate_at is None:
                terminate_at = now + (grace if received[0] == signal.SIGINT else 0)
            if terminate_at is not None and kill_at is None and now >= terminate_at:
                proc.terminate()
                kill_at = now + grace
            if kill_at is not None and now >= kill_at:
                proc.kill()
    finally:
        # Whatever ended the loop -- the drop that follows must never run
        # under a pytest that is still alive.
        if proc.poll() is None:
            proc.kill()
        proc.wait()
    if terminate_at is not None:
        return 128 + received[0]
    return 128 - proc.returncode if proc.returncode < 0 else proc.returncode


async def _on_the_server(application_url: str, action):
    conn = await _connect(application_url)
    try:
        return await action(conn)
    finally:
        # A server that does not answer must not hold the exit up either.
        try:
            await asyncio.wait_for(conn.close(), 10)
        except Exception:
            conn.terminate()


@contextlib.contextmanager
def signals_noted():
    """While this is open, a signal is only NOTED, in the list it yields.

    That keeps it from interrupting the create (where it would leave a database
    whose token nobody holds) and the drop; `supervise` acts on it while pytest
    runs. A signal that was already ignored when we started (`nohup`, a
    background job) stays ignored.
    """
    received: list[int] = []
    previous = {}
    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT):
        if signal.getsignal(signum) is not signal.SIG_IGN:
            previous[signum] = signal.signal(signum, lambda number, _frame: received.append(number))
    try:
        yield received
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


#: Set for pytest when every xdist worker has a database of its own; the harness
#: (tests/backend/conftest.py) refuses a worker without it rather than letting
#: two workers share one database.
PER_WORKER_ENV = "RENFIELD_TEST_DB_PER_WORKER"
MAX_WORKERS = 16


def worker_names(name: str, workers: int) -> list[str]:
    """The databases of a run: NAME itself, or NAME_gw0 .. NAME_gw<N-1> -- one
    per xdist worker, named the way xdist names the workers. Each is checked
    like any other name, so a suffix too long for the worker tag is refused."""
    if workers == 0:
        names = [name]
    elif 1 <= workers <= MAX_WORKERS:
        names = [f"{name}_gw{i}" for i in range(workers)]
    else:
        raise Refused(f"--workers must be 1..{MAX_WORKERS}, not {workers}.")
    for n in names:
        require_throwaway_name(n)
    return names


async def _create_all(conn, names: list[str], application_url: str) -> str:
    """Create every database under ONE token -- or none of them."""
    token = await create_ready(conn, names[0], application_url)
    made = [names[0]]
    try:
        for n in names[1:]:
            await _create_ready_with(conn, n, application_url, token)
            made.append(n)
    except BaseException:
        for n in made:
            try:
                await drop(conn, n, token)
            except Exception as cleanup_error:
                print(
                    f"WARNING: {n} could not be removed again "
                    f"({type(cleanup_error).__name__}) -- remove it by hand.",
                    file=sys.stderr,
                )
        raise
    return token


async def _drop_all(conn, names: list[str], token: str) -> list[str]:
    """Drop every database of the run; returns the names that were NOT dropped."""
    left: list[str] = []
    for n in names:
        try:
            await drop(conn, n, token)
        except Exception as exc:
            _say(f"WARNING: {n} was NOT dropped ({_describe(exc)}) -- remove it by hand.")
            left.append(n)
    return left


def run(name: str, pytest_args: list[str], workers: int = 0) -> int:
    application_url = os.environ.get("DATABASE_URL", "")
    if not application_url:
        raise Refused("DATABASE_URL is not set.")
    check_name(name, application_url)
    names = worker_names(name, workers)
    for n in names:
        check_name(n, application_url)
    grace = stop_grace()  # refused HERE, before anything is created
    # With workers, the URL names NAME itself, which is never created: each
    # worker's harness swaps in NAME_<its id>. A worker that does not is
    # pointed at a database that does not exist -- an error, not a shared one.
    env = {**os.environ, "RENFIELD_TEST_PG_URL": pytest_url(application_url, name)}
    if workers:
        env[PER_WORKER_ENV] = "1"
        pytest_args = ["-n", str(workers), *pytest_args]
    else:
        env.pop(PER_WORKER_ENV, None)

    with signals_noted() as received:
        token = asyncio.run(_on_the_server(application_url, lambda conn: _create_all(conn, names, application_url)))
        code = 1
        try:
            shown = names[0] if len(names) == 1 else f"{names[0]} .. {names[-1]}"
            _say(f"TEST_DB: {shown} (created for this run, dropped when it ends)", sys.stdout)
            code = supervise(pytest_args, grace, received, env)
        finally:
            # `supervise` has returned or raised: pytest is dead and reaped.
            try:
                left = asyncio.run(_on_the_server(application_url, lambda conn: _drop_all(conn, names, token)))
            except Exception as exc:
                _say(f"WARNING: the databases were NOT dropped ({_describe(exc)}) -- remove them by hand.")
                left = names
            if left:
                code = code or 1
            else:
                _say(f"dropped {shown}")
        if code == 0 and received:
            code = 128 + received[0]  # came during the drop: not acted on, but not swallowed
    return code


def main(argv: list[str]) -> int:
    try:
        usage = "usage: throwaway_test_db.py run renfield_test_<suffix> [--workers N] -- <pytest args>"
        if len(argv) < 3 or argv[0] != "run":
            raise Refused(usage)
        workers = 0
        rest = argv[2:]
        if rest[0] == "--workers":
            if len(rest) < 3 or not re.fullmatch(r"[0-9]{1,2}", rest[1]):
                raise Refused(usage)
            workers = int(rest[1])
            if workers == 0:
                raise Refused(f"--workers must be 1..{MAX_WORKERS}, not 0.")
            rest = rest[2:]
        if rest[0] != "--":
            raise Refused(usage)
        return run(argv[1], rest[1:], workers)
    except Refused as exc:
        _say(f"REFUSED: {exc}")
        return exc.code
    except Exception as exc:
        _say(f"ERROR: {_describe(exc)}")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
