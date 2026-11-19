"""``bin/run-backend-tests.sh`` and ``bin/throwaway_test_db.py`` -- a run's own
test database (``RENFIELD_TEST_DB_SUFFIX``), so concurrent runs need not queue
behind the shared ``renfield_test``.

What is pinned is what could cost data, not that the happy path exists:

- a name that is not ``renfield_test_<suffix>`` reaches neither Postgres nor
  pytest -- checked in the shell AND in the Python helper;
- a database that already exists is refused: not reused, not dropped;
- ``drop`` removes only a database carrying THIS invocation's token;
- the database is dropped when pytest fails and when the run is signalled --
  and never while pytest is still alive;
- without the variable the script behaves as it always did.

The middle section runs the real script against a stub ``python``. The stub
hands the URL derivation (``python -``) to the real interpreter, so that part
-- and its redaction -- is the real code; a call of the helper goes to the REAL
helper too, with only its connection replaced by a recording one and its
pytest by a stub. It runs under ``sh`` AND under ``bash``: a check that holds
in one shell only is not a check.

The last section uses neither stub nor fake: the real helper, and the real
script with the real pytest launcher, against real Postgres -- on a database
each test creates under a random ``renfield_test_selftest_<hex>`` name and
removes again. Those tests skip without ``RENFIELD_TEST_PG_URL``.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import itertools
import os
import secrets
import shutil
import signal
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

from tests.backend.conftest import _TEST_DB_NAME_RE

pytestmark = [pytest.mark.unit]

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "bin" / "run-backend-tests.sh"
_ADMIN = _REPO / "bin" / "throwaway_test_db.py"
_APP_URL = "postgresql://app:PASSWORD@db:5432/renfield"
# What an OUTER run under pytest-xdist sets for its own workers. A run this
# file starts must not inherit it, or it becomes an xdist run itself (#1577).
_OUTER_XDIST_VARS = ("PYTEST_XDIST_WORKER", "RENFIELD_TEST_WORKERS", "RENFIELD_TEST_DB_PER_WORKER")
_TOKEN = "0123456789abcdef0123456789abcdef"

_BAD_SUFFIXES = [
    "Upper", "a-b", "a b", "a;b", "a/b", "a'b", 'a"b', "a$b", "ä", "_lead",
    "prod", "my_prod_2", "live", "x_live_y", "a" * 41, "a\nb",
]


def _default_signals() -> None:
    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT, signal.SIGPIPE):
        signal.signal(signum, signal.SIG_DFL)


def _load_admin():
    spec = importlib.util.spec_from_file_location("_under_test_throwaway_test_db", _ADMIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def admin():
    assert _ADMIN.is_file(), f"incomplete copy: {_ADMIN} is missing (rsync bin/ too)"
    return _load_admin()


# --------------------------------------------------------------------------
# The Python helper: names
# --------------------------------------------------------------------------


_ALLOWED = set("abcdefghijklmnopqrstuvwxyz0123456789_")


def _rule(suffix: str) -> bool:
    """The suffix rule, written out a THIRD time and on purpose not as a
    regular expression: the shell pattern and `NAME_RE` are both checked
    against this, so neither can drift without a test noticing."""
    return (
        0 < len(suffix) <= 40
        and set(suffix) <= _ALLOWED
        and not suffix.startswith("_")
        and "prod" not in suffix
        and "live" not in suffix
    )


def _generated_suffixes(alphabet: str, longest: int) -> list[str]:
    out = {"".join(c) for n in range(1, longest + 1) for c in itertools.product(alphabet, repeat=n)}
    out |= {
        head + word + tail
        for word in ("prod", "live", "pro", "liv", "PROD", "Live", "p_rod")
        for head in ("", "a", "_", "0")
        for tail in ("", "a", "_", "1")
    }
    out |= {"a" * 39, "a" * 40, "a" * 41, "a" * 39 + "_", "0" * 40, "___", "a" * 64}
    return sorted(out)


def test_the_name_rule_agrees_with_the_harness_guard_over_a_generated_set(admin):
    suffixes = _generated_suffixes("az09_A-\u00e9 .", 3)
    accepted = [s for s in suffixes if admin.NAME_RE.fullmatch("renfield_test_" + s)]
    assert [s for s in suffixes if _rule(s)] == accepted
    assert len(accepted) > 100 and len(suffixes) - len(accepted) > 500, "the set must exercise both answers"
    refused_by_harness = [s for s in accepted if not _TEST_DB_NAME_RE.fullmatch("renfield_test_" + s)]
    assert refused_by_harness == [], "the harness would refuse a database this script creates"
    for s in accepted:
        admin.check_name("renfield_test_" + s, _APP_URL)


@pytest.mark.parametrize(
    "name",
    ["renfield_test", "renfield_test2", "renfield", "postgres", "template1", "",
     "renfield_test_", "renfield_test__x", "x_renfield_test_a", "renfield_test_a ",
     "renfield_test_a\n", 'renfield_test_a"; DROP DATABASE renfield; --',
     "renfield_base", "renfield_vt1", *[f"renfield_test_{s}" for s in _BAD_SUFFIXES]],
)
def test_any_other_name_is_refused(admin, name):
    with pytest.raises(admin.Refused) as exc:
        admin.check_name(name, _APP_URL)
    assert exc.value.code == admin.EXIT_REFUSED


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://u:PASSWORD@db/renfield_test_app",
        "postgresql+asyncpg://u:PASSWORD@db/renfield_test_app?ssl=disable",
        "postgresql://u:PASSWORD@db:5432/renfield_test_app?options=a/b",
        "postgresql://u:PASSWORD@db:5432/renfield_test_app?application_name=renfield/x",
        "postgresql://u:PASSWORD@db:5432/renfield_test_app/",
        "postgresql://u:" + "p%2Fw" + "@db:5432/renfield_test_app",
    ],
)
def test_the_application_database_is_refused_even_under_a_matching_name(admin, url):
    # Parsed like the driver parses it: a slash in the query string or at the
    # end must not hide that the name IS the application database.
    with pytest.raises(admin.Refused, match="application database"):
        admin.check_name("renfield_test_app", url)


def test_another_application_database_does_not_refuse_the_name(admin):
    admin.check_name("renfield_test_app", "postgresql://u:PASSWORD@db:5432/renfield?x=/renfield_test_app")


def test_an_unparsable_application_url_is_refused(admin):
    with pytest.raises(admin.Refused, match="cannot be parsed"):
        admin.check_name("renfield_test_app", "not a url")


@pytest.mark.parametrize(
    "argv",
    [["run", "renfield_test", "--"], ["run", "renfield", "--"], ["run", "renfield_test2", "--"],
     ["run", "renfield_test_x"], ["run", "renfield_test_x", "-q"], ["run"], [],
     ["create", "renfield_test_x"], ["drop", "renfield_test_x", _TOKEN], ["truncate", "renfield_test_x", "--"]],
)
def test_a_refused_call_never_opens_a_connection(admin, monkeypatch, capsys, argv):
    async def _no_connection(_url, database="postgres"):
        raise AssertionError("connected to Postgres for a refused call")

    monkeypatch.setattr(admin, "_connect", _no_connection)
    monkeypatch.setattr(admin, "PYTEST_COMMAND", ["false"])
    monkeypatch.setenv("DATABASE_URL", _APP_URL)
    assert admin.main(argv) == admin.EXIT_REFUSED
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "PASSWORD" not in captured.err


def test_the_application_database_is_refused_by_a_run_before_any_connection(admin, monkeypatch, capsys):
    # `check_name` is tested above; this pins that `run` is where it is called.
    async def _no_connection(_url, database="postgres"):
        raise AssertionError("connected to Postgres for the application database")

    monkeypatch.setattr(admin, "_connect", _no_connection)
    monkeypatch.setattr(admin, "PYTEST_COMMAND", ["false"])
    monkeypatch.setenv("DATABASE_URL", "postgresql://app:PASSWORD@db:5432/renfield_test_app")
    assert admin.main(["run", "renfield_test_app", "--"]) == admin.EXIT_REFUSED
    captured = capsys.readouterr()
    assert "application database" in captured.err
    assert "PASSWORD" not in captured.out + captured.err


@pytest.mark.parametrize(
    "application_url,expected",
    [
        ("postgresql://app:PASSWORD@db:5432/renfield", "postgresql+asyncpg://app:PASSWORD@db:5432/renfield_test_a"),
        ("postgresql+asyncpg://app:PASSWORD@db/renfield", "postgresql+asyncpg://app:PASSWORD@db/renfield_test_a"),
        # A slash in the query string: the part a naive split takes for the name.
        (
            "postgresql://app:PASSWORD@db:5432/renfield?options=a/b",
            "postgresql+asyncpg://app:PASSWORD@db:5432/renfield_test_a?options=a%2Fb",
        ),
    ],
)
def test_pytests_url_is_the_applications_server_with_the_validated_name(admin, application_url, expected):
    from sqlalchemy.engine import make_url

    built = admin.pytest_url(application_url, "renfield_test_a")
    assert make_url(built) == make_url(expected)
    assert make_url(built).database == "renfield_test_a"
    with pytest.raises(admin.Refused):
        admin.pytest_url(application_url, "renfield_test")


def test_pytest_is_dead_and_reaped_even_when_supervising_it_fails(admin, monkeypatch):
    # The `finally` in `supervise`: whatever ends the loop, the drop that
    # follows must not run under a living pytest. Nothing raises in there
    # today -- so something is made to.
    started = []

    class _Recorded(subprocess.Popen):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            started.append(self)

    class _BrokenClock:
        @staticmethod
        def monotonic():
            raise RuntimeError("the clock broke")

    monkeypatch.setattr(admin, "PYTEST_COMMAND", ["sleep", "60"])
    # On the helper's own names, not on the real modules pytest uses too.
    monkeypatch.setattr(admin, "subprocess", types.SimpleNamespace(Popen=_Recorded, TimeoutExpired=subprocess.TimeoutExpired))
    monkeypatch.setattr(admin, "time", _BrokenClock)
    try:
        with pytest.raises(RuntimeError, match="the clock broke"):
            admin.supervise([], 1, [])
        (proc,) = started
        assert proc.returncode is not None, "pytest was left running (or unreaped) behind a failed supervision"
    finally:
        for proc in started:
            if proc.poll() is None:
                proc.kill()
                proc.wait()


@pytest.mark.parametrize("grace", ["abc", "-1", "1.5", " 3", "3 ", "1e3", "12345", "٣"])
def test_a_grace_period_that_is_no_whole_number_is_refused_before_anything_is_created(
    admin, monkeypatch, capsys, grace
):
    # A value `sleep` chokes on used to mean TERM and KILL with no grace at all.
    async def _no_connection(_url, database="postgres"):
        raise AssertionError("connected to Postgres although the grace period is unusable")

    monkeypatch.setattr(admin, "_connect", _no_connection)
    monkeypatch.setenv("DATABASE_URL", _APP_URL)
    monkeypatch.setenv(admin.STOP_GRACE_ENV, grace)
    assert admin.main(["run", "renfield_test_a", "--"]) == admin.EXIT_REFUSED
    assert admin.STOP_GRACE_ENV in capsys.readouterr().err


@pytest.mark.parametrize("grace,seconds", [("0", 0), ("7", 7), ("0010", 10), ("", 10), (None, 10)])
def test_a_whole_number_of_seconds_is_the_grace_period_and_unset_is_ten(admin, monkeypatch, grace, seconds):
    if grace is None:
        monkeypatch.delenv(admin.STOP_GRACE_ENV, raising=False)
    else:
        monkeypatch.setenv(admin.STOP_GRACE_ENV, grace)
    assert admin.stop_grace() == seconds


def test_noted_signals_do_not_stay_noted_afterwards(admin):
    before = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT)}
    with admin.signals_noted() as received:
        if before[signal.SIGTERM] is not signal.SIG_IGN:
            os.kill(os.getpid(), signal.SIGTERM)
            assert received == [signal.SIGTERM], "a noted signal must neither kill nor raise"
    assert {s: signal.getsignal(s) for s in before} == before


# --------------------------------------------------------------------------
# The Python helper: create / drop against a recording connection
# --------------------------------------------------------------------------


class _Conn:
    """Records statements; `existing` maps database name -> comment."""

    def __init__(self, existing=None, fail_comment=False):
        self.existing = dict(existing or {})
        self.fail_comment = fail_comment
        self.statements: list[str] = []

    async def execute(self, sql):
        from asyncpg.exceptions import DuplicateDatabaseError

        self.statements.append(sql)
        if sql.startswith("CREATE DATABASE"):
            name = sql.split('"')[1]
            if name in self.existing:
                raise DuplicateDatabaseError(f'database "{name}" already exists')
            self.existing[name] = None
        elif sql.startswith("COMMENT ON DATABASE"):
            if self.fail_comment:
                raise RuntimeError("connection lost")
            self.existing[sql.split('"')[1]] = sql.split("'")[1]
        elif sql.startswith("DROP DATABASE"):
            self.existing.pop(sql.split('"')[1], None)

    async def fetchrow(self, _sql, name):
        return {"mark": self.existing[name]} if name in self.existing else None

    @property
    def drops(self):
        return [s for s in self.statements if s.startswith("DROP")]


def test_create_marks_the_database_and_drop_with_that_token_removes_it(admin):
    conn = _Conn()
    token = asyncio.run(admin.create(conn, "renfield_test_a"))
    assert admin.TOKEN_RE.fullmatch(token)
    assert conn.existing == {"renfield_test_a": admin.MARK + token}
    assert asyncio.run(admin.drop(conn, "renfield_test_a", token)) is True
    assert conn.drops == ['DROP DATABASE "renfield_test_a" WITH (FORCE)']
    assert conn.existing == {}


def test_create_refuses_an_existing_database_and_leaves_it_alone(admin):
    conn = _Conn(existing={"renfield_test_a": admin.MARK + _TOKEN})
    with pytest.raises(admin.Refused) as exc:
        asyncio.run(admin.create(conn, "renfield_test_a"))
    assert exc.value.code == admin.EXIT_EXISTS
    assert conn.statements == ['CREATE DATABASE "renfield_test_a"'], "no COMMENT, no DROP on a database that was already there"
    assert conn.existing == {"renfield_test_a": admin.MARK + _TOKEN}


@pytest.mark.parametrize(
    "mark",
    [None, "", "something else", "throwaway test database, run token " + "f" * 32,
     "throwaway test database, run token " + _TOKEN + "0"],
)
def test_drop_refuses_a_database_that_does_not_carry_this_runs_token(admin, mark):
    conn = _Conn(existing={"renfield_test_a": mark})
    with pytest.raises(admin.Refused) as exc:
        asyncio.run(admin.drop(conn, "renfield_test_a", _TOKEN))
    assert exc.value.code == admin.EXIT_NOT_OURS
    assert conn.drops == []
    assert "renfield_test_a" in conn.existing


@pytest.mark.parametrize("token", ["", "x", _TOKEN.upper(), _TOKEN + "'", "' OR ''='"])
def test_drop_refuses_a_malformed_token_before_any_statement(admin, token):
    conn = _Conn(existing={"renfield_test_a": admin.MARK + _TOKEN})
    with pytest.raises(admin.Refused):
        asyncio.run(admin.drop(conn, "renfield_test_a", token))
    assert conn.statements == []


def test_a_drop_that_hangs_ends_with_a_clear_error_instead_of_hanging(admin, monkeypatch):
    # After a signal nothing but SIGKILL could end a run stuck in its drop.
    class _Stuck(_Conn):
        async def execute(self, sql):
            if sql.startswith("DROP"):
                await asyncio.sleep(3600)
            await super().execute(sql)

    monkeypatch.setattr(admin, "DROP_TIMEOUT_SECONDS", 0.05)
    conn = _Stuck()
    token = asyncio.run(admin.create(conn, "renfield_test_a"))
    started = time.monotonic()
    with pytest.raises(admin.DropTimedOut, match="dropping renfield_test_a did not finish"):
        asyncio.run(admin.drop(conn, "renfield_test_a", token))
    assert time.monotonic() - started < 10


def test_a_mark_that_cannot_be_read_in_time_is_the_same_error_and_drops_nothing(admin, monkeypatch):
    class _Stuck(_Conn):
        async def fetchrow(self, _sql, name):
            await asyncio.sleep(3600)

    monkeypatch.setattr(admin, "DROP_TIMEOUT_SECONDS", 0.05)
    conn = _Stuck({"renfield_test_a": admin.MARK + _TOKEN})
    with pytest.raises(admin.DropTimedOut):
        asyncio.run(admin.drop(conn, "renfield_test_a", _TOKEN))
    assert conn.drops == []


def test_drop_of_a_database_that_is_gone_is_not_an_error(admin):
    conn = _Conn()
    assert asyncio.run(admin.drop(conn, "renfield_test_a", _TOKEN)) is False
    assert conn.drops == []


def test_a_database_that_could_not_be_marked_is_removed_again(admin):
    # Unmarked, nobody could ever drop it: `create` cleans up after itself.
    conn = _Conn(fail_comment=True)
    with pytest.raises(RuntimeError):
        asyncio.run(admin.create(conn, "renfield_test_a"))
    assert conn.existing == {}


@pytest.mark.parametrize("name", ["renfield_test", "renfield", "postgres", 'renfield_test_a"; DROP DATABASE renfield; --', "", None])
def test_create_and_drop_check_the_name_themselves(admin, name):
    # The check belongs where the SQL is built: a caller that skips `_main`
    # must not get a raw name into CREATE or DROP.
    conn = _Conn(existing={"renfield": None, "renfield_test": None, "postgres": None})
    with pytest.raises(admin.Refused):
        asyncio.run(admin.create(conn, name))
    with pytest.raises(admin.Refused):
        asyncio.run(admin.drop(conn, name, _TOKEN))
    with pytest.raises(admin.Refused):
        asyncio.run(admin._drop_unmarked(conn, name))
    assert conn.statements == []


def test_a_lost_creation_race_is_already_exists_too(admin):
    # Two creates of one name at the same instant: the loser fails on the
    # unique index of pg_database (23505), not with duplicate_database.
    from asyncpg.exceptions import UniqueViolationError

    class _Racing(_Conn):
        async def execute(self, sql):
            self.statements.append(sql)
            raise UniqueViolationError('duplicate key value violates unique constraint "pg_database_datname_index"')

    conn = _Racing()
    with pytest.raises(admin.Refused) as exc:
        asyncio.run(admin.create(conn, "renfield_test_a"))
    assert exc.value.code == admin.EXIT_EXISTS
    assert conn.statements == ['CREATE DATABASE "renfield_test_a"']


@pytest.mark.parametrize("mark", ["somebody's mark", "throwaway test database, run token " + _TOKEN, ""])
def test_the_unmarked_cleanup_leaves_a_marked_database_alone(admin, mark):
    conn = _Conn(existing={"renfield_test_a": mark})
    asyncio.run(admin._drop_unmarked(conn, "renfield_test_a"))
    assert conn.drops == []


def test_an_interrupt_while_marking_issues_no_further_statement(admin):
    # After a cancellation the connection is in an unknown state; a cleanup
    # statement on it could fail and replace the interrupt.
    class _Cancelled(_Conn):
        async def execute(self, sql):
            if sql.startswith("COMMENT"):
                self.statements.append(sql)
                raise asyncio.CancelledError
            await super().execute(sql)

    conn = _Cancelled()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(admin.create(conn, "renfield_test_a"))
    assert conn.drops == []
    assert [s.split()[0] for s in conn.statements] == ["CREATE", "COMMENT"]


def test_a_cleanup_that_fails_does_not_replace_the_original_error(admin, capsys):
    class _Broken(_Conn):
        async def fetchrow(self, _sql, name):
            raise OSError("connection is closed")

    conn = _Broken(fail_comment=True)
    with pytest.raises(RuntimeError, match="connection lost"):
        asyncio.run(admin.create(conn, "renfield_test_a"))
    assert "could neither be marked nor removed" in capsys.readouterr().err


def test_an_interrupt_while_installing_the_extension_drops_by_token(admin, monkeypatch):
    class _Interrupted(_Inside):
        async def execute(self, sql):
            raise asyncio.CancelledError

    maintenance = _Conn()
    _connections(admin, monkeypatch, maintenance, _Interrupted())
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(admin.create_ready(maintenance, "renfield_test_a", _APP_URL))
    assert maintenance.existing == {}
    assert maintenance.drops == ['DROP DATABASE "renfield_test_a" WITH (FORCE)']


def _connections(admin, monkeypatch, maintenance, inside):
    """Route `_connect` to the maintenance or the in-database connection."""
    opened: list[str] = []

    async def _fake_connect(_url, database="postgres"):
        opened.append(database)
        return maintenance if database == "postgres" else inside

    monkeypatch.setattr(admin, "_connect", _fake_connect)
    monkeypatch.setenv("DATABASE_URL", _APP_URL)
    return opened


class _Inside:
    """The connection INTO the new database."""

    def __init__(self, fail=False):
        self.fail = fail
        self.statements: list[str] = []
        self.closed = False

    async def execute(self, sql):
        self.statements.append(sql)
        if self.fail:
            from asyncpg.exceptions import InsufficientPrivilegeError

            raise InsufficientPrivilegeError('permission denied to create extension "vector"')

    async def close(self):
        self.closed = True


async def _noop_close():
    return None


def test_a_new_database_gets_the_vector_extension_the_shared_one_has(admin, monkeypatch):
    maintenance, inside = _Conn(), _Inside()
    opened = _connections(admin, monkeypatch, maintenance, inside)
    token = asyncio.run(admin.create_ready(maintenance, "renfield_test_a", _APP_URL))
    assert opened == ["renfield_test_a"]
    assert inside.statements == ["CREATE EXTENSION IF NOT EXISTS vector SCHEMA public"]
    assert inside.closed
    assert maintenance.existing == {"renfield_test_a": admin.MARK + token}


def test_a_database_that_cannot_get_its_extension_is_dropped_again(admin, monkeypatch, capsys):
    maintenance, inside = _Conn(), _Inside(fail=True)
    maintenance.close = _noop_close
    _connections(admin, monkeypatch, maintenance, inside)
    monkeypatch.setattr(admin, "PYTEST_COMMAND", ["false"])
    assert admin.main(["run", "renfield_test_a", "--"]) == 1
    assert maintenance.existing == {}, "a half-prepared database must not block the suffix"
    captured = capsys.readouterr()
    assert "TEST_DB" not in captured.out, "nothing may be announced for a database that does not exist"
    assert "permission denied to create extension" in captured.err


def test_a_driver_error_prints_its_type_only(admin, monkeypatch, capsys):
    async def _boom(url, database="postgres"):
        raise OSError(f"could not connect to {url}")

    monkeypatch.setattr(admin, "_connect", _boom)
    monkeypatch.setenv("DATABASE_URL", _APP_URL)
    assert admin.main(["run", "renfield_test_a", "--"]) == 1
    captured = capsys.readouterr()
    assert "OSError" in captured.err
    assert "PASSWORD" not in captured.out + captured.err


# --------------------------------------------------------------------------
# The real script and the real helper; stubbed are Postgres and pytest only
# --------------------------------------------------------------------------

# 🛑 Drains stdin on every call it answers itself: the script feeds `python -`
# a here-document, and a reader that exits without reading turns that into a
# SIGPIPE (exit 141) the real interpreter never produces -- see
# test_deploy_migration_gate.py. The `-` call goes to the real interpreter,
# which reads it. The pytest call KEEPS what it read: pytest has to get the
# script's own stdin, not /dev/null. The helper call does not touch stdin at
# all -- it is pytest's.
_PYTHON_STUB = r"""#!/bin/bash
if [[ "$1" == "-" ]]; then
  echo "derive-url" >> "$STUB_LOG"
  exec "$STUB_REAL_PYTHON" -
fi
case "$1" in
  -c)
    # `python -c "import xdist"`: is pytest-xdist installed?
    echo "xdist-check" >> "$STUB_LOG"
    [[ -n "${STUB_NO_XDIST:-}" ]] && exit 1
    exit 0 ;;
  -m)
    shift 2
    [[ -n "${RENFIELD_TEST_DB_PER_WORKER:-}" ]] && echo "per-worker=$RENFIELD_TEST_DB_PER_WORKER" >> "$STUB_LOG"
    echo $$ > "$STUB_PYTEST_PID"
    case "${STUB_PYTEST_MODE:-}" in
      hang)        exec sleep 60 ;;
      ignore-term) trap '' TERM; exec sleep 60 ;;
      slow-term)   sleep 60 & child=$!
                   trap 'kill "$child"; sleep 0.5; exit 0' TERM
                   wait; exit 0 ;;
    esac
    stdin=$(cat)
    db="${RENFIELD_TEST_PG_URL##*/}"
    echo "pytest db=$db scheme=${RENFIELD_TEST_PG_URL%%://*} stdin=[$stdin] args=$*" >> "$STUB_LOG"
    exit "${STUB_PYTEST_RC:-0}" ;;
  */throwaway_test_db.py)
    # As if the helper were called by hand, with a leftover variable around.
    [[ -n "${STUB_STALE_URL:-}" ]] && export RENFIELD_TEST_PG_URL="$STUB_STALE_URL"
    exec "$STUB_REAL_PYTHON" "$STUB_DRIVER" "$@" ;;
esac
cat >/dev/null
echo "unexpected $*" >> "$STUB_LOG"
exit 97
"""

# Loads the REAL helper and runs its `main`, with two things replaced: the
# connection (a recording one -- no Postgres here) and the pytest command (the
# stub above). Everything that decides when the database is created, when
# pytest is signalled and when the drop is issued is the code under test.
#
# The drop records whether the stub pytest is still alive at that moment: the
# database must never be dropped under a running pytest.
_DRIVER = r"""
import asyncio
import importlib.util
import os
import sys

spec = importlib.util.spec_from_file_location("throwaway_test_db", sys.argv[1])
admin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(admin)


def log(line):
    with open(os.environ["STUB_LOG"], "a") as out:
        out.write(line + "\n")


def pytest_alive():
    try:
        with open(os.environ["STUB_PYTEST_PID"]) as pidfile:
            os.kill(int(pidfile.read()), 0)
    except (OSError, ValueError):
        return "no"
    return "yes"


class Conn:
    marks = {}

    async def execute(self, sql):
        name = sql.split('"')[1] if '"' in sql else ""
        if sql.startswith("CREATE DATABASE"):
            log(f"admin create {name}")
            await asyncio.sleep(float(os.environ.get("STUB_CREATE_DELAY", "0")))
            if os.environ.get("STUB_EXISTS"):
                from asyncpg.exceptions import DuplicateDatabaseError

                raise DuplicateDatabaseError(f'database "{name}" already exists')
            Conn.marks[name] = None
        elif sql.startswith("COMMENT ON DATABASE"):
            Conn.marks[name] = sql.split("'")[1]
        elif sql.startswith("DROP DATABASE"):
            log(f"admin drop {name} pytest_alive={pytest_alive()}")
            mode = os.environ.get("STUB_DROP", "")
            if mode == "fail" or name == os.environ.get("STUB_DROP_FAIL_NAME"):
                raise RuntimeError("connection lost")
            if mode == "hang":
                await asyncio.sleep(3600)
            if mode == "slow":
                await asyncio.sleep(2)
            Conn.marks.pop(name, None)
            log(f"admin dropped {name}")

    async def fetchrow(self, _sql, name):
        return {"mark": Conn.marks[name]} if name in Conn.marks else None

    async def close(self):
        pass

    def terminate(self):
        pass


async def connect(_url, database="postgres"):
    return Conn()


admin._connect = connect
admin.PYTEST_COMMAND = [os.environ["STUB_PYTHON"], "-m", "pytest"]
if os.environ.get("STUB_DROP_TIMEOUT"):
    admin.DROP_TIMEOUT_SECONDS = float(os.environ["STUB_DROP_TIMEOUT"])
sys.exit(admin.main(sys.argv[2:]))
"""


def _a_utf8_locale() -> str | None:
    """A UTF-8 locale of this machine, the ones with a collation of their own first.

    In those, `[a-z]` in bash stops meaning "the 26 lowercase letters". The
    last two candidates collate by codepoint, exactly like C -- see
    test_the_bash_runs_carry_a_negative_control_on_this_machine.
    """
    try:
        have = subprocess.run(["locale", "-a"], capture_output=True, text=True, timeout=10).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return None
    for wanted in ("en_US.UTF-8", "en_US.utf8", "de_DE.UTF-8", "de_DE.utf8", "C.UTF-8", "C.utf8"):
        if wanted in have:
            return wanted
    return None


def test_the_bash_runs_carry_a_negative_control_on_this_machine():
    # Not a check of the script: it says, in every run's summary, what the
    # `bash` half of the parametrisation below is worth HERE.
    name = _a_utf8_locale()
    if name is None or name.upper().startswith("C."):
        pytest.skip(
            f"only {name or 'the inherited locale'} here: it collates by codepoint like C, so a range "
            "pattern `[!a-z0-9_]` would pass the bash runs too -- they carry no negative control on this "
            "image. The script lists every character and does not depend on it."
        )


@pytest.fixture(params=["sh", "bash"])
def script(request, tmp_path):
    """Returns (run, popen, log): the real script with the stub first in PATH.

    Once under `sh` (dash in the backend image) and once under bash with a
    UTF-8 locale: the suffix check used to hold under the first only.
    """
    shell = request.param
    if shutil.which("bash") is None:  # pragma: no cover - environment guard
        pytest.skip("bash not available (the stub is a bash script)")
    assert _SCRIPT.is_file(), f"incomplete copy: {_SCRIPT} is missing (rsync bin/ too)"
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir()
    stub = stub_dir / "python"
    stub.write_text(_PYTHON_STUB)
    stub.chmod(0o755)
    driver = tmp_path / "driver.py"
    driver.write_text(_DRIVER)
    log = tmp_path / "calls.log"
    log.write_text("")

    def _env(suffix, stub_env):
        env = {
            k: v for k, v in os.environ.items()
            if k not in (
                "RENFIELD_TEST_DB_SUFFIX", "RENFIELD_TEST_STOP_GRACE", "LC_ALL", "LC_COLLATE",
                *_OUTER_XDIST_VARS,
            )
        }
        env["PATH"] = f"{stub_dir}{os.pathsep}{env.get('PATH', '')}"
        env["DATABASE_URL"] = _APP_URL
        env["STUB_LOG"] = str(log)
        env["STUB_PYTEST_PID"] = str(tmp_path / "pytest.pid")
        env["STUB_REAL_PYTHON"] = sys.executable
        env["STUB_PYTHON"] = str(stub)
        env["STUB_DRIVER"] = str(driver)
        env["RENFIELD_TEST_STOP_GRACE"] = "1"
        locale_name = _a_utf8_locale()
        if shell == "bash" and locale_name:
            env["LC_ALL"] = locale_name
        if suffix is not None:
            env["RENFIELD_TEST_DB_SUFFIX"] = suffix
        env.update({k: str(v) for k, v in stub_env.items()})
        return env

    def _run(suffix=None, *pytest_args, stdin_text=None, **stub_env):
        return subprocess.run(
            [shell, str(_SCRIPT), str(_REPO), *pytest_args],
            capture_output=True, text=True, env=_env(suffix, stub_env), timeout=120,
            **({"input": stdin_text} if stdin_text is not None else {"stdin": subprocess.DEVNULL}),
        )

    def _popen(suffix, *pytest_args, **stub_env):
        return subprocess.Popen(
            [shell, str(_SCRIPT), str(_REPO), *pytest_args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=_env(suffix, stub_env), stdin=subprocess.DEVNULL,
            # A signal inherited as IGNORED stays ignored, by design; and this
            # test may itself run under `nohup` (SIGHUP) or as a background
            # job of a shell (SIGINT, SIGQUIT).
            preexec_fn=_default_signals,
        )

    return _run, _popen, log


def _calls(log: Path) -> list[str]:
    return log.read_text().splitlines()


def _wait_for(condition, proc, what: str):
    deadline = time.monotonic() + 30
    while not condition():
        assert proc.poll() is None, f"the run ended before {what}: {proc.stderr.read()}"
        assert time.monotonic() < deadline, f"never saw: {what}"
        time.sleep(0.02)


_DROP_A1 = ["admin drop renfield_test_a1 pytest_alive=no", "admin dropped renfield_test_a1"]
_PYTEST_A1 = "pytest db=renfield_test_a1 scheme=postgresql+asyncpg stdin=[] args=-o asyncio_mode=auto -p no:cacheprovider"


def test_without_the_variable_the_shared_database_is_used_and_nothing_is_created(script):
    run, _, log = script
    proc = run(None, "tests/x.py", "-q", STUB_PYTEST_RC=5)
    assert proc.returncode == 5, proc.stderr
    assert _calls(log) == [
        "derive-url",
        "pytest db=renfield_test scheme=postgresql+asyncpg stdin=[] "
        "args=-o asyncio_mode=auto -p no:cacheprovider tests/x.py -q",
    ]
    assert "//<redacted>@db:5432/renfield_test" in proc.stdout
    assert "PASSWORD" not in proc.stdout + proc.stderr


def test_an_empty_variable_is_the_shared_database_too(script):
    run, _, log = script
    assert run("").returncode == 0
    assert [c.split(" stdin=")[0] for c in _calls(log)] == ["derive-url", "pytest db=renfield_test scheme=postgresql+asyncpg"]


@pytest.mark.parametrize("suffix", _BAD_SUFFIXES)
def test_a_bad_suffix_is_refused_before_anything_runs(script, suffix):
    run, _, log = script
    proc = run(suffix)
    assert proc.returncode == 2
    assert "REFUSED" in proc.stderr
    assert _calls(log) == [], "neither Postgres nor pytest may be reached with a refused name"


def test_the_shell_rule_and_the_python_rule_accept_the_same_suffixes(script, admin):
    # Nothing but this ties the `case` pattern in the script to NAME_RE. A
    # suffix the shell lets through and Python refuses is "validated once";
    # one the shell refuses and Python accepts is a usable name locked out.
    # Here the two are compared through the real chain: a suffix the shell
    # passed reaches the real helper, whose own check then has to agree.
    run, _, log = script
    wrong = []
    for suffix in _generated_suffixes("a9_Aé", 2):
        log.write_text("")
        by_script = run(suffix).returncode != 2
        by_python = bool(admin.NAME_RE.fullmatch("renfield_test_" + suffix))
        if by_script != by_python or by_python != _rule(suffix):
            wrong.append((suffix, by_script, by_python))
        if not by_script:
            assert _calls(log) == [], suffix
    assert wrong == []


def test_a_run_of_its_own_creates_points_pytest_at_it_and_drops(script):
    run, _, log = script
    proc = run("agent_7", "-q")
    assert proc.returncode == 0, proc.stderr
    assert _calls(log) == [
        "derive-url",
        "admin create renfield_test_agent_7",
        "pytest db=renfield_test_agent_7 scheme=postgresql+asyncpg stdin=[] "
        "args=-o asyncio_mode=auto -p no:cacheprovider -q",
        "admin drop renfield_test_agent_7 pytest_alive=no",
        "admin dropped renfield_test_agent_7",
    ]
    assert "//<redacted>@db:5432/renfield_test_agent_7" in proc.stdout
    assert "TEST_DB: renfield_test_agent_7" in proc.stdout
    assert "PASSWORD" not in proc.stdout + proc.stderr


def test_pytest_runs_in_the_database_just_created_whatever_url_was_lying_around(script):
    # The helper builds pytest's URL from the name it validated and created. A
    # stale RENFIELD_TEST_PG_URL -- the shared database, say -- must not reach
    # pytest: it would run there while an unused throwaway one comes and goes.
    run, _, log = script
    proc = run("a1", STUB_STALE_URL="postgresql+asyncpg://app:PASSWORD@db:5432/renfield_test")
    assert proc.returncode == 0, proc.stderr
    assert _calls(log)[2].startswith("pytest db=renfield_test_a1 scheme=postgresql+asyncpg "), _calls(log)
    assert "PASSWORD" not in proc.stdout + proc.stderr


def test_pytest_gets_the_scripts_own_stdin_on_a_database_of_its_own(script):
    # `--pdb` needs something to read from.
    run, _, log = script
    proc = run("a1", stdin_text="typed into the debugger")
    assert proc.returncode == 0, proc.stderr
    assert "stdin=[typed into the debugger]" in _calls(log)[2]


@pytest.mark.parametrize("rc", [1, 2, 137, 143])
def test_the_database_is_dropped_when_pytest_fails_and_its_code_is_kept(script, rc):
    run, _, log = script
    proc = run("a1", STUB_PYTEST_RC=rc)
    assert proc.returncode == rc
    assert _calls(log)[-2:] == _DROP_A1


def test_an_existing_database_stops_the_run_and_is_never_dropped(script):
    run, _, log = script
    proc = run("a1", STUB_EXISTS=1)
    assert proc.returncode == 3
    assert _calls(log) == ["derive-url", "admin create renfield_test_a1"], "no pytest, and above all no drop"


@pytest.mark.parametrize("pytest_rc,code", [(1, 1), (5, 5), (0, 1)])
def test_a_failed_drop_is_reported_never_hides_a_failure_and_fails_a_green_run(script, pytest_rc, code):
    run, _, log = script
    proc = run("a1", STUB_DROP="fail", STUB_PYTEST_RC=pytest_rc)
    assert proc.returncode == code
    assert "renfield_test_a1 was NOT dropped" in proc.stderr
    assert sum(c.startswith("admin drop ") for c in _calls(log)) == 1


def test_a_drop_that_hangs_ends_the_run_with_a_message_and_a_failure(script):
    run, _, log = script
    started = time.monotonic()
    proc = run("a1", STUB_DROP="hang", STUB_DROP_TIMEOUT=1)
    assert time.monotonic() - started < 60
    assert proc.returncode == 1
    assert "renfield_test_a1 was NOT dropped" in proc.stderr
    assert "did not finish within" in proc.stderr
    assert _calls(log)[-1] == "admin drop renfield_test_a1 pytest_alive=no"


@pytest.mark.parametrize("grace", ["abc", "-1", "1.5"])
def test_an_unusable_grace_period_is_refused_before_anything_is_created(script, grace):
    run, _, log = script
    proc = run("a1", RENFIELD_TEST_STOP_GRACE=grace)
    assert proc.returncode == 2
    assert "RENFIELD_TEST_STOP_GRACE" in proc.stderr
    assert _calls(log) == ["derive-url"]


def _signal_a_running_pytest(popen, tmp_path, signum, mode, *pytest_args, **stub_env):
    """Returns (exit code, pid the stub pytest had, seconds from signal to exit)."""
    pidfile = tmp_path / "pytest.pid"
    proc = popen("a1", *pytest_args, STUB_PYTEST_MODE=mode, **stub_env)
    try:
        _wait_for(lambda: pidfile.exists() and pidfile.read_text().strip(), proc, "the stub pytest starting")
        pytest_pid = int(pidfile.read_text())
        time.sleep(0.1)  # let the stub reach its `sleep` / install its trap
        sent = time.monotonic()
        proc.send_signal(signum)
        return proc.wait(timeout=60), pytest_pid, time.monotonic() - sent
    finally:
        if proc.poll() is None:  # pragma: no cover - only on a failing run
            proc.kill()


@pytest.mark.parametrize(
    "signum,code",
    [(signal.SIGTERM, 143), (signal.SIGINT, 130), (signal.SIGHUP, 129), (signal.SIGQUIT, 131)],
)
def test_a_signal_stops_pytest_and_drops_the_database(script, tmp_path, signum, code):
    _, popen, log = script
    returncode, pytest_pid, _ = _signal_a_running_pytest(popen, tmp_path, signum, "hang")
    assert returncode == code
    calls = _calls(log)
    assert calls[-2:] == _DROP_A1, "the drop must be issued, and only once pytest is dead"
    assert sum(c.startswith("admin drop ") for c in calls) == 1
    with pytest.raises(ProcessLookupError):
        os.kill(pytest_pid, 0)


@pytest.mark.parametrize("mode", ["slow-term", "ignore-term"])
def test_the_database_is_never_dropped_under_a_pytest_that_is_still_alive(script, tmp_path, mode):
    # A pytest that takes its time over SIGTERM, and one that ignores it (a
    # test installed its own handler, or hangs in one): the run waits, then
    # kills -- and only THEN drops. The recording connection notes what it saw.
    _, popen, log = script
    returncode, pytest_pid, _ = _signal_a_running_pytest(popen, tmp_path, signal.SIGTERM, mode)
    assert returncode == 143
    assert _calls(log)[-2:] == _DROP_A1
    with pytest.raises(ProcessLookupError):
        os.kill(pytest_pid, 0)


def test_a_signal_while_the_database_is_being_created_starts_no_pytest_and_drops_it(script):
    # The create is not interrupted half-way (that would leave a database whose
    # token nobody holds): it finishes, pytest is never started, the drop runs.
    _, popen, log = script
    proc = popen("a1", STUB_CREATE_DELAY=2)
    try:
        _wait_for(lambda: "admin create renfield_test_a1" in _calls(log), proc, "the create")
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=60) == 143
    finally:
        if proc.poll() is None:  # pragma: no cover - only on a failing run
            proc.kill()
    assert _calls(log) == ["derive-url", "admin create renfield_test_a1", *_DROP_A1]


@pytest.mark.parametrize("pytest_rc,code", [(5, 5), (0, 143)])
def test_a_signal_while_the_database_is_being_dropped_does_not_abort_the_drop(script, pytest_rc, code):
    # pytest has ended by itself: a failure keeps its own exit code. A green
    # run does not answer a TERM with "0, all fine" -- the sender gets its 143.
    _, popen, log = script
    proc = popen("a1", STUB_DROP="slow", STUB_PYTEST_RC=pytest_rc)
    try:
        _wait_for(lambda: any(c.startswith("admin drop ") for c in _calls(log)), proc, "the drop")
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=60) == code
    finally:
        if proc.poll() is None:  # pragma: no cover - only on a failing run
            proc.kill()
    assert _calls(log)[-2:] == _DROP_A1


def test_a_reader_that_closed_the_output_stops_neither_the_run_nor_the_drop(script):
    # `... | head -1`: the reader is gone when the helper announces its
    # database. Dying of that line (SIGPIPE, or the exception Python makes of
    # it) would skip pytest -- or, in a shell, the drop.
    _, popen, log = script
    proc = popen("a1", "-q", STUB_CREATE_DELAY=2, STUB_PYTEST_RC=5)
    try:
        _wait_for(lambda: "admin create renfield_test_a1" in _calls(log), proc, "the create")
        proc.stdout.close()  # the script's own lines are out; the helper's are still to come
        assert proc.wait(timeout=60) == 5, proc.stderr.read()
    finally:
        if proc.poll() is None:  # pragma: no cover - only on a failing run
            proc.kill()
    assert _calls(log) == ["derive-url", "admin create renfield_test_a1", _PYTEST_A1 + " -q", *_DROP_A1]


def _processes_mentioning(marker: str) -> list[str]:
    found = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit() and int(entry.name) != os.getpid():
            try:
                cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
            except OSError:
                continue
            if marker in cmdline:
                found.append(cmdline[:80])
    return found


@pytest.mark.skipif(not Path("/proc/self/cmdline").exists(), reason="needs /proc to look for leftover processes")
def test_nothing_of_the_run_outlives_it_and_a_dead_pytest_is_not_waited_out(script, tmp_path):
    # An earlier version escalated through a watchdog subshell that survived a
    # plain `kill` and sat there for the grace period, to fire SIGKILL at a pid
    # that was no longer pytest's. Both halves are pinned: no process of the
    # run is left, AND the run is over long before the grace period is -- a
    # leftover that is merely waited for would pass the first half.
    _, popen, _log = script
    marker = f"marker-{secrets.token_hex(8)}"
    returncode, _pid, seconds = _signal_a_running_pytest(
        popen, tmp_path, signal.SIGTERM, "hang", marker, RENFIELD_TEST_STOP_GRACE=20,
    )
    assert returncode == 143
    assert seconds < 10, f"took {seconds:.1f} s of a 20 s grace period although pytest died at once"
    assert _processes_mentioning(marker) == []


# --------------------------------------------------------------------------
# No stub, no fake: the real helper and the real script on real Postgres
# --------------------------------------------------------------------------
#
# Everything above checks the control flow against a dictionary. A typo in
# `COMMENT ON DATABASE`, in `WITH (FORCE)` or in the query that reads the mark
# back leaves all of it green -- so the statements themselves run here, each
# test on a database it creates under a random name and removes again. The
# role needs CREATEDB (on the build box it is a superuser); without it, or
# without RENFIELD_TEST_PG_URL, these tests skip.

_EXISTS_SQL = "SELECT count(*) FROM pg_database WHERE datname = $1"
_READ_MARK_SQL = "SELECT d.description FROM pg_shdescription d JOIN pg_database b ON b.oid = d.objoid WHERE b.datname = $1"


_SELFTEST_PREFIX = "renfield_test_selftest_"


class _Server:
    """Two maintenance connections plus the bookkeeping to clean up.

    Two layers. Inside the scenario, by token and through the code under test.
    And after the test, whatever happened in it (`remove_leftovers`): by the
    exact NAME this test generated -- a test that fails the way it is meant to
    detect has no token to drop by. A name is enough there only because
    `new_name` hands out, and registers, a name it has SEEN ABSENT on the
    server: a database that was there before is never this test's, is never
    used and never dropped.
    """

    def __init__(self, admin, url):
        self.admin, self.url = admin, url
        self.conn = self.other = None
        self.created: dict[str, str] = {}
        self.names: list[str] = []

    def new_name(self) -> str:
        return asyncio.run(self._a_name_nobody_has())

    async def _a_name_nobody_has(self) -> str:
        conn = await self.admin._connect(self.url)
        try:
            for _ in range(20):
                name = f"{_SELFTEST_PREFIX}{secrets.token_hex(4)}"
                if not await conn.fetchval(_EXISTS_SQL, name):
                    self.names.append(name)
                    return name
        finally:
            await conn.close()
        raise AssertionError("twenty random selftest names all exist already")

    async def __aenter__(self):
        self.conn = await self.admin._connect(self.url)
        self.other = await self.admin._connect(self.url)
        return self

    async def __aexit__(self, *_exc):
        # By token, through the code under test -- never by name alone.
        for name, token in self.created.items():
            # Best effort: the assertion that failed is what matters.
            with contextlib.suppress(Exception):
                await self.admin.drop(self.conn, name, token)
        await self.conn.close()
        await self.other.close()

    async def create(self, name: str) -> str:
        from asyncpg.exceptions import InsufficientPrivilegeError

        try:
            token = await self.admin.create(self.conn, name)
        except InsufficientPrivilegeError:
            pytest.skip("the test role may not create databases")
        self.created[name] = token
        return token

    async def exists(self, name: str) -> bool:
        return bool(await self.other.fetchval(_EXISTS_SQL, name))

    async def mark(self, name: str):
        # Read from the catalogue directly, not through the helper's own query.
        return await self.other.fetchval(_READ_MARK_SQL, name)

    async def remove_leftovers(self) -> list[str]:
        """Drop what is still there of the names this test generated."""
        removed = []
        conn = await self.admin._connect(self.url)
        try:
            for name in self.names:
                self.admin.require_throwaway_name(name)
                assert name.startswith(_SELFTEST_PREFIX) and len(name) == len(_SELFTEST_PREFIX) + 8, name
                if await conn.fetchval(_EXISTS_SQL, name):
                    await conn.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
                    removed.append(name)
        finally:
            await conn.close()
        return removed


@pytest.fixture
def server(admin, pg_url):
    server = _Server(admin, pg_url)
    yield server
    # A FRESH connection and a fresh loop: the test may have failed anywhere.
    asyncio.run(server.remove_leftovers())


def test_a_database_that_was_there_before_is_neither_used_nor_removed_by_the_finalizer(admin):
    # The by-name cleanup may only ever meet a database this test created.
    class _Recording:
        def __init__(self):
            self.present: set[str] = set()
            self.asked: list[str] = []
            self.statements: list[str] = []

        async def fetchval(self, _sql, name):
            if not self.asked:
                self.present.add(name)  # the first name offered is somebody else's
            self.asked.append(name)
            return int(name in self.present)

        async def execute(self, sql):
            self.statements.append(sql)

        async def close(self):
            pass

    conn = _Recording()

    async def _fake_connect(_url, database="postgres"):
        return conn

    admin._connect = _fake_connect  # `admin` is a fresh module per test
    server = _Server(admin, _APP_URL)
    name = server.new_name()
    foreign = conn.asked[0]
    assert name != foreign and server.names == [name], "a name that already exists must not be handed out"
    conn.present.add(name)  # the test's own database, left behind by a failure
    assert asyncio.run(server.remove_leftovers()) == [name]
    assert conn.statements == [f'DROP DATABASE "{name}" WITH (FORCE)']
    assert foreign in conn.present and foreign not in "".join(conn.statements)


def test_the_finalizer_removes_only_names_the_test_generated_itself(admin):
    # The guard of the by-name cleanup, without a server: a foreign name is
    # refused before any statement.
    class _NoStatements:
        async def fetchval(self, *_args):
            raise AssertionError("a statement was issued for a name the test did not generate")

        execute = fetchval

        async def close(self):
            pass

    async def _fake_connect(_url, database="postgres"):
        return _NoStatements()

    for foreign in ("renfield_test", "renfield", "renfield_test_agent_7", "renfield_test_selftest_"):
        server = _Server(admin, _APP_URL)
        admin._connect = _fake_connect  # `admin` is a fresh module per test
        server.names.append(foreign)
        # Refused by the helper's rule, or by the selftest shape on top of it.
        with pytest.raises((admin.Refused, AssertionError)):
            asyncio.run(server.remove_leftovers())


@pytest.mark.postgres
class TestTheRealHelperOnRealPostgres:
    def test_the_whole_life_of_a_database_of_ones_own(self, admin, server):
        name = server.new_name()

        async def scenario():
            async with server:
                token = await server.create(name)
                assert await server.mark(name) == admin.MARK + token

                wrong = "0" * 32 if token != "0" * 32 else "1" * 32
                with pytest.raises(admin.Refused) as refused:
                    await admin.drop(server.conn, name, wrong)
                assert refused.value.code == admin.EXIT_NOT_OURS
                assert await server.exists(name), "a wrong token must leave the database standing"

                with pytest.raises(admin.Refused) as refused:
                    await admin.create(server.other, name)
                assert refused.value.code == admin.EXIT_EXISTS
                assert await server.mark(name) == admin.MARK + token, "the refused create must not re-mark it"

                # A connection left inside, as an interrupted pytest leaves one:
                # a plain DROP would fail on it, FORCE must not.
                inside = await admin._connect(server.url, name)
                assert await inside.fetchval("SELECT current_database()") == name
                assert await admin.drop(server.conn, name, token) is True
                assert not await server.exists(name)
                assert await admin.drop(server.conn, name, token) is False, "gone is not an error"

        asyncio.run(scenario())

    def test_a_ready_database_has_the_vector_extension(self, admin, server):
        name = server.new_name()

        async def scenario():
            from asyncpg.exceptions import InsufficientPrivilegeError

            async with server:
                try:
                    token = await admin.create_ready(server.conn, name, server.url)
                except InsufficientPrivilegeError:
                    assert not await server.exists(name), "a database that could not be prepared must not stay"
                    pytest.skip("the test role may not create databases or install vector")
                server.created[name] = token
                inside = await admin._connect(server.url, name)
                try:
                    assert await inside.fetchval("SELECT count(*) FROM pg_extension WHERE extname = 'vector'") == 1
                    await inside.execute("CREATE TABLE probe (v vector(3))")
                finally:
                    await inside.close()
                assert await admin.drop(server.conn, name, token) is True

        asyncio.run(scenario())

    def test_a_database_that_cannot_be_marked_does_not_stay(self, admin, server):
        # No token ever exists here; if the cleanup under test is broken, the
        # `server` fixture removes the database by its generated name.
        name = server.new_name()

        class _CommentFails:
            def __init__(self, conn):
                self._conn = conn

            async def execute(self, sql):
                if sql.startswith("COMMENT"):
                    raise RuntimeError("the comment could not be written")
                return await self._conn.execute(sql)

            async def fetchrow(self, *args):
                return await self._conn.fetchrow(*args)

        async def scenario():
            from asyncpg.exceptions import InsufficientPrivilegeError

            async with server:
                try:
                    with pytest.raises(RuntimeError, match="comment could not be written"):
                        await admin.create(_CommentFails(server.conn), name)
                except InsufficientPrivilegeError:
                    pytest.skip("the test role may not create databases")
                assert not await server.exists(name)

        asyncio.run(scenario())

    def test_the_unmarked_cleanup_does_not_touch_a_marked_database(self, admin, server):
        name = server.new_name()

        async def scenario():
            async with server:
                token = await server.create(name)
                await admin._drop_unmarked(server.conn, name)
                assert await server.exists(name)
                assert await admin.drop(server.conn, name, token) is True

        asyncio.run(scenario())

    def test_of_several_creates_at_once_one_wins_and_the_rest_are_refused(self, admin, server):
        name = server.new_name()

        async def scenario():
            from asyncpg.exceptions import InsufficientPrivilegeError

            async with server:
                racers = [await admin._connect(server.url) for _ in range(6)]
                try:
                    results = await asyncio.gather(*(admin.create(c, name) for c in racers), return_exceptions=True)
                finally:
                    for c in racers:
                        await c.close()
                tokens = [r for r in results if isinstance(r, str)]
                if tokens:
                    server.created[name] = tokens[0]
                if any(isinstance(r, InsufficientPrivilegeError) for r in results):
                    pytest.skip("the test role may not create databases")
                losers = [r for r in results if not isinstance(r, str)]
                assert len(tokens) == 1, results
                # Whichever way a loser lost -- duplicate_database or the unique
                # index of pg_database -- it is "already exists", exit 3.
                assert [type(r) for r in losers] == [admin.Refused] * 5, results
                assert {r.code for r in losers} == {admin.EXIT_EXISTS}
                assert await server.mark(name) == admin.MARK + tokens[0]
                assert await admin.drop(server.conn, name, tokens[0]) is True

        asyncio.run(scenario())


_INNER_TEST = '''
import asyncio
import os

import asyncpg


def test_where_am_i():
    url = os.environ["RENFIELD_TEST_PG_URL"].replace("postgresql+asyncpg://", "postgresql://", 1)

    async def ask():
        conn = await asyncpg.connect(url)
        try:
            return (
                await conn.fetchval("SELECT current_database()"),
                await conn.fetchval("SELECT count(*) FROM pg_extension WHERE extname = 'vector'"),
                await conn.fetchval(
                    "SELECT shobj_description(oid, 'pg_database') FROM pg_database"
                    " WHERE datname = current_database()"
                ),
            )
        finally:
            await conn.close()

    database, vector, mark = asyncio.run(ask())
    with open(os.environ["PROBE_OUT"], "w") as out:
        out.write(f"{database}|{vector}|{mark}")
    # Not `assert os.environ.get(...) != "1"`: pytest's introspection prints
    # `os.environ` on failure -- DATABASE_URL and its password included. It
    # stayed hidden behind the repr truncation until an xdist run reordered the
    # environment (#1577).
    asked_to_fail = os.environ.get("PROBE_FAIL") == "1"
    assert not asked_to_fail
'''


@pytest.fixture
def real_script(admin, pg_url, server, tmp_path):
    """The real script, the real helper, the real pytest -- no stub in PATH.

    The script derives its server from DATABASE_URL, as it does in real use; it
    must be the server the test URL points at, or the test would look for its
    database in the wrong place.

    Returns (run, out, server). Take the database name from `server.new_name()`:
    the `server` fixture then removes it on every path, including the timeout
    below and a script that failed to drop.
    """
    from sqlalchemy.engine import make_url

    application_url = os.environ.get("DATABASE_URL")
    if not application_url:
        pytest.skip("DATABASE_URL not set -- the script derives its connection from it")
    app, test = make_url(application_url), make_url(pg_url)
    if (app.host, app.port or 5432) != (test.host, test.port or 5432):
        pytest.skip("DATABASE_URL and RENFIELD_TEST_PG_URL name different servers")
    inner = tmp_path / "test_inner_probe.py"
    inner.write_text(_INNER_TEST)
    out = tmp_path / "probe.out"

    def _run(name, **extra):
        assert name in server.names, "take the name from server.new_name(), or nothing cleans it up"
        env = {
            k: v for k, v in os.environ.items()
            if k not in ("RENFIELD_TEST_PG_URL", "RENFIELD_TEST_STOP_GRACE", *_OUTER_XDIST_VARS)
        }
        env["PATH"] = f"{Path(sys.executable).parent}{os.pathsep}{env.get('PATH', '')}"
        env["RENFIELD_TEST_DB_SUFFIX"] = name.removeprefix("renfield_test_")
        env["PROBE_OUT"] = str(out)
        env.update(extra)
        # A session of its own: on a timeout the helper AND the pytest under
        # it are killed, not the helper alone -- an orphaned pytest would keep
        # running against a database the finalizer is about to drop.
        proc = subprocess.Popen(
            ["sh", str(_SCRIPT), str(_REPO), str(inner), "-q"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
            stdin=subprocess.DEVNULL, start_new_session=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=300)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            raise
        password = app.password
        if password:
            assert password not in stdout + stderr, "the script printed the database password"
        return subprocess.CompletedProcess(proc.args, proc.returncode, stdout, stderr)

    return _run, out, server


@pytest.mark.postgres
class TestTheRealScriptOnRealPostgres:
    @pytest.mark.parametrize("fail,code", [("0", 0), ("1", 1)])
    def test_pytest_runs_inside_its_own_database_which_is_gone_afterwards(self, admin, real_script, fail, code):
        run, out, server = real_script
        name = server.new_name()

        proc = run(name, PROBE_FAIL=fail)

        if "permission denied" in proc.stderr:
            pytest.skip("the test role may not create databases or install vector")
        assert proc.returncode == code, proc.stdout + proc.stderr
        database, vector, mark = out.read_text().split("|")
        assert database == name, "pytest was not pointed at the run's own database"
        assert vector == "1"
        assert mark.startswith(admin.MARK) and admin.TOKEN_RE.fullmatch(mark[len(admin.MARK):])
        assert f"dropped {name}" in proc.stderr

        async def gone():
            async with server:
                return not await server.exists(name)

        assert asyncio.run(gone()), f"{name} is still there"

    def test_an_existing_database_is_refused_with_3_and_left_exactly_as_it_was(self, admin, real_script):
        run, out, server = real_script
        name = server.new_name()

        async def scenario():
            async with server:
                token = await server.create(name)
                proc = await asyncio.to_thread(run, name)
                assert proc.returncode == admin.EXIT_EXISTS, proc.stdout + proc.stderr
                assert "already exists" in proc.stderr
                assert not out.exists(), "pytest must not have run"
                assert await server.mark(name) == admin.MARK + token
                assert await admin.drop(server.conn, name, token) is True

        asyncio.run(scenario())

    def test_a_real_pytest_is_stopped_by_a_signal_and_its_database_is_gone(self, admin, real_script, tmp_path):
        # The one signal run with nothing stubbed: the real helper, a real
        # pytest that sits in a test, real Postgres.
        _run, out, server = real_script
        name = server.new_name()
        inner = tmp_path / "test_inner_sleeper.py"
        inner.write_text(
            "import os, time\n\n\ndef test_sits_there():\n"
            "    open(os.environ['PROBE_OUT'], 'w').write('started')\n    time.sleep(120)\n"
        )
        env = {k: v for k, v in os.environ.items() if k not in ("RENFIELD_TEST_PG_URL", *_OUTER_XDIST_VARS)}
        env["PATH"] = f"{Path(sys.executable).parent}{os.pathsep}{env.get('PATH', '')}"
        env.update(RENFIELD_TEST_DB_SUFFIX=name.removeprefix("renfield_test_"), PROBE_OUT=str(out),
                   RENFIELD_TEST_STOP_GRACE="5")
        proc = subprocess.Popen(
            ["sh", str(_SCRIPT), str(_REPO), str(inner), "-q"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
            stdin=subprocess.DEVNULL, start_new_session=True, preexec_fn=_default_signals,
        )
        try:
            deadline = time.monotonic() + 120
            while not out.exists():
                if proc.poll() is not None:
                    stderr = proc.stderr.read()
                    if "permission denied" in stderr:
                        pytest.skip("the test role may not create databases or install vector")
                    raise AssertionError(f"the run ended before the test started: {stderr}")
                assert time.monotonic() < deadline, "the inner test never started"
                time.sleep(0.05)
            proc.send_signal(signal.SIGTERM)
            _stdout, stderr = proc.communicate(timeout=120)
        finally:
            if proc.poll() is None:  # still ours, not yet reaped: the group id is not reused
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.communicate()
        assert proc.returncode == 143, stderr
        assert f"dropped {name}" in stderr

        async def gone():
            async with server:
                return not await server.exists(name)

        assert asyncio.run(gone()), f"{name} is still there"


# --------------------------------------------------------------------------
# RENFIELD_TEST_WORKERS: pytest-xdist, one database per worker (#1577)
# --------------------------------------------------------------------------


def test_workers_get_a_database_each_pytest_gets_n_and_all_are_dropped(script):
    run, _, log = script
    proc = run("a1", "-q", RENFIELD_TEST_WORKERS="3")
    assert proc.returncode == 0, proc.stderr
    assert _calls(log) == [
        "xdist-check",
        "derive-url",
        "admin create renfield_test_a1_gw0",
        "admin create renfield_test_a1_gw1",
        "admin create renfield_test_a1_gw2",
        "per-worker=renfield_test_a1",
        # The URL names the base; each worker's harness builds its own from it.
        "pytest db=renfield_test_a1 scheme=postgresql+asyncpg stdin=[] "
        "args=-n 3 -o asyncio_mode=auto -p no:cacheprovider -q",
        "admin drop renfield_test_a1_gw0 pytest_alive=no",
        "admin dropped renfield_test_a1_gw0",
        "admin drop renfield_test_a1_gw1 pytest_alive=no",
        "admin dropped renfield_test_a1_gw1",
        "admin drop renfield_test_a1_gw2 pytest_alive=no",
        "admin dropped renfield_test_a1_gw2",
    ]
    assert "TEST_DB: renfield_test_a1_gw0 .. renfield_test_a1_gw2" in proc.stdout
    assert "PASSWORD" not in proc.stdout + proc.stderr


def test_workers_without_a_suffix_are_refused_before_anything_runs(script):
    # The shared renfield_test is ONE database.
    run, _, log = script
    proc = run(None, RENFIELD_TEST_WORKERS="2")
    assert proc.returncode == 2
    assert "needs RENFIELD_TEST_DB_SUFFIX" in proc.stderr
    assert _calls(log) == []


@pytest.mark.parametrize("workers", ["0", "17", "x", "03", "-1", "2 ", "1e1"])
def test_a_bad_worker_count_is_refused_before_anything_runs(script, workers):
    run, _, log = script
    proc = run("a1", RENFIELD_TEST_WORKERS=workers)
    assert proc.returncode == 2, (workers, proc.stderr)
    assert "RENFIELD_TEST_WORKERS" in proc.stderr
    assert _calls(log) == []


def test_without_pytest_xdist_the_run_is_refused_before_a_database_exists(script):
    run, _, log = script
    proc = run("a1", RENFIELD_TEST_WORKERS="2", STUB_NO_XDIST="1")
    assert proc.returncode == 2
    assert "pytest-xdist is not installed" in proc.stderr
    assert _calls(log) == ["xdist-check"]


def test_a_suffix_too_long_for_the_worker_tag_is_refused_by_the_helper(script):
    # The shell accepts 40 characters; `_gw0` makes the database name too long.
    run, _, log = script
    proc = run("a" * 40, RENFIELD_TEST_WORKERS="2")
    assert proc.returncode == 2
    assert "REFUSED" in proc.stderr
    assert not any(c.startswith(("admin create", "pytest")) for c in _calls(log))


def test_worker_names_follow_the_xdist_ids_and_are_checked(admin):
    assert admin.worker_names("renfield_test_a", 0) == ["renfield_test_a"]
    assert admin.worker_names("renfield_test_a", 2) == ["renfield_test_a_gw0", "renfield_test_a_gw1"]
    for bad in (-1, 17):
        with pytest.raises(admin.Refused):
            admin.worker_names("renfield_test_a", bad)
    with pytest.raises(admin.Refused):
        admin.worker_names("renfield_test_" + "a" * 40, 1)


def test_if_one_worker_database_exists_none_is_left_behind_and_it_is_untouched(admin, monkeypatch):
    conn = _Conn(existing={"renfield_test_a_gw1": "somebody else's"})

    async def no_extension(*_args, **_kwargs):
        return None

    monkeypatch.setattr(admin, "_prepare_or_drop", no_extension)
    with pytest.raises(admin.Refused):
        asyncio.run(admin._create_all(conn, ["renfield_test_a_gw0", "renfield_test_a_gw1"], _APP_URL))
    assert conn.drops == ['DROP DATABASE "renfield_test_a_gw0" WITH (FORCE)']
    assert conn.existing == {"renfield_test_a_gw1": "somebody else's"}


def test_all_worker_databases_carry_one_token_and_drop_with_it(admin, monkeypatch):
    conn = _Conn()

    async def no_extension(*_args, **_kwargs):
        return None

    monkeypatch.setattr(admin, "_prepare_or_drop", no_extension)
    names = ["renfield_test_a_gw0", "renfield_test_a_gw1"]
    token = asyncio.run(admin._create_all(conn, names, _APP_URL))
    assert conn.existing == {n: admin.MARK + token for n in names}
    assert asyncio.run(admin._drop_all(conn, names, token)) == []
    assert conn.existing == {}


def test_a_given_token_is_checked_before_it_goes_into_sql(admin):
    conn = _Conn()
    with pytest.raises(admin.Refused):
        asyncio.run(admin.create(conn, "renfield_test_a", "x'; DROP DATABASE renfield; --"))
    assert conn.statements == []


def test_one_worker_database_that_cannot_be_dropped_fails_a_green_run_the_rest_still_go(script):
    run, _, log = script
    proc = run("a1", RENFIELD_TEST_WORKERS="3", STUB_DROP_FAIL_NAME="renfield_test_a1_gw1")
    assert proc.returncode == 1, proc.stderr
    calls = _calls(log)
    assert "admin dropped renfield_test_a1_gw0" in calls
    assert "admin dropped renfield_test_a1_gw2" in calls
    assert "admin dropped renfield_test_a1_gw1" not in calls
    assert "renfield_test_a1_gw1 was NOT dropped" in proc.stderr
    assert "dropped renfield_test_a1_gw0 .. " not in proc.stderr


def test_drop_all_reports_exactly_the_databases_left_behind(admin):
    class _Failing(_Conn):
        async def execute(self, sql):
            if sql.startswith("DROP") and "_gw1" in sql:
                raise RuntimeError("connection lost")
            await super().execute(sql)

    conn = _Failing()
    names = ["renfield_test_a_gw0", "renfield_test_a_gw1", "renfield_test_a_gw2"]
    token = "0" * 32
    conn.existing = {n: admin.MARK + token for n in names}
    assert asyncio.run(admin._drop_all(conn, names, token)) == ["renfield_test_a_gw1"]
    assert conn.existing == {"renfield_test_a_gw1": admin.MARK + token}
