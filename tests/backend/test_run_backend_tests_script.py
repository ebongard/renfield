"""``bin/run-backend-tests.sh`` and ``bin/throwaway_test_db.py`` -- a run's own
test database (``RENFIELD_TEST_DB_SUFFIX``), so concurrent runs need not queue
behind the shared ``renfield_test``.

What is pinned is what could cost data, not that the happy path exists:

- a name that is not ``renfield_test_<suffix>`` reaches neither Postgres nor
  pytest -- checked in the shell AND in the Python helper;
- a database that already exists is refused: not reused, not dropped;
- ``drop`` removes only a database carrying THIS invocation's token;
- the database is dropped when pytest fails and when the script is signalled;
- without the variable the script behaves as it always did.

The shell half runs the real script against a stub ``python``. The stub hands
the URL derivation (``python -``) to the real interpreter, so that part -- and
its redaction -- is the real code. It runs under ``sh`` AND under ``bash`` with
a UTF-8 locale: a check that holds in one shell only is not a check.

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
from pathlib import Path

import pytest

from tests.backend.conftest import _TEST_DB_NAME_RE

pytestmark = [pytest.mark.unit]

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "bin" / "run-backend-tests.sh"
_ADMIN = _REPO / "bin" / "throwaway_test_db.py"
_APP_URL = "postgresql://app:s3cr3t-pw@db:5432/renfield"
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
        "postgresql+asyncpg://u:p@db/renfield_test_app",
        "postgresql+asyncpg://u:p@db/renfield_test_app?ssl=disable",
        "postgresql://u:p@db:5432/renfield_test_app?options=a/b",
        "postgresql://u:p@db:5432/renfield_test_app?application_name=renfield/x",
        "postgresql://u:p@db:5432/renfield_test_app/",
        "postgresql://u:p%2Fw@db:5432/renfield_test_app",
    ],
)
def test_the_application_database_is_refused_even_under_a_matching_name(admin, url):
    # Parsed like the driver parses it: a slash in the query string or at the
    # end must not hide that the name IS the application database.
    with pytest.raises(admin.Refused, match="application database"):
        admin.check_name("renfield_test_app", url)


def test_another_application_database_does_not_refuse_the_name(admin):
    admin.check_name("renfield_test_app", "postgresql://u:p@db:5432/renfield?x=/renfield_test_app")


def test_an_unparsable_application_url_is_refused(admin):
    with pytest.raises(admin.Refused, match="cannot be parsed"):
        admin.check_name("renfield_test_app", "not a url")


@pytest.mark.parametrize(
    "argv",
    [["drop", "renfield_test", _TOKEN], ["drop", "renfield", _TOKEN], ["create", "renfield_test"],
     ["create", "renfield_test2"], ["drop", "renfield_test_x"], ["create"], [], ["truncate", "renfield_test_x"]],
)
def test_a_refused_call_never_opens_a_connection(admin, monkeypatch, capsys, argv):
    async def _no_connection(_url):
        raise AssertionError("connected to Postgres for a refused call")

    monkeypatch.setattr(admin, "_connect", _no_connection)
    monkeypatch.setenv("DATABASE_URL", _APP_URL)
    assert admin.main(argv) == admin.EXIT_REFUSED
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "s3cr3t-pw" not in captured.err


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


def test_a_new_database_gets_the_vector_extension_the_shared_one_has(admin, monkeypatch, capsys):
    maintenance, inside = _Conn(), _Inside()
    maintenance.close = _noop_close
    opened = _connections(admin, monkeypatch, maintenance, inside)
    assert admin.main(["create", "renfield_test_a"]) == 0
    assert opened == ["postgres", "renfield_test_a"]
    assert inside.statements == ["CREATE EXTENSION IF NOT EXISTS vector SCHEMA public"]
    assert inside.closed
    token = capsys.readouterr().out.strip()
    assert maintenance.existing == {"renfield_test_a": admin.MARK + token}


def test_a_database_that_cannot_get_its_extension_is_dropped_again(admin, monkeypatch, capsys):
    maintenance, inside = _Conn(), _Inside(fail=True)
    maintenance.close = _noop_close
    _connections(admin, monkeypatch, maintenance, inside)
    assert admin.main(["create", "renfield_test_a"]) == 1
    assert maintenance.existing == {}, "a half-prepared database must not block the suffix"
    captured = capsys.readouterr()
    assert captured.out == "", "no token for a database that does not exist"
    assert "permission denied to create extension" in captured.err


def test_a_driver_error_prints_its_type_only(admin, monkeypatch, capsys):
    async def _boom(url, database="postgres"):
        raise OSError(f"could not connect to {url}")

    monkeypatch.setattr(admin, "_connect", _boom)
    monkeypatch.setenv("DATABASE_URL", _APP_URL)
    assert admin.main(["create", "renfield_test_a"]) == 1
    captured = capsys.readouterr()
    assert "OSError" in captured.err
    assert "s3cr3t-pw" not in captured.out + captured.err


# --------------------------------------------------------------------------
# The shell script against a stub `python`
# --------------------------------------------------------------------------

# 🛑 Drains stdin on every call it answers itself: the script feeds `python -`
# a here-document, and a reader that exits without reading turns that into a
# SIGPIPE (exit 141) the real interpreter never produces -- see
# test_deploy_migration_gate.py. The `-` call goes to the real interpreter,
# which reads it. The pytest call KEEPS what it read: the script has to hand
# pytest its own stdin, not /dev/null.
#
# The drop call records whether the stub pytest is still alive at that moment:
# the database must never be dropped under a running pytest.
_PYTHON_STUB = r"""#!/bin/bash
if [[ "$1" == "-" ]]; then
  echo "derive-url" >> "$STUB_LOG"
  exec "$STUB_REAL_PYTHON" -
fi
case "$1" in
  -m|-c)
    # `-m pytest` on the shared database, `-c <launcher>` on a run's own.
    how="$1"; shift 2
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
    echo "pytest $how db=$db scheme=${RENFIELD_TEST_PG_URL%%://*} stdin=[$stdin] args=$*" >> "$STUB_LOG"
    exit "${STUB_PYTEST_RC:-0}" ;;
  */throwaway_test_db.py)
    cat >/dev/null
    case "$2" in
      create) echo "admin create $3" >> "$STUB_LOG"
              [[ -n "${STUB_EXISTS:-}" ]] && { echo "REFUSED: exists" >&2; exit 3; }
              echo "$STUB_TOKEN"; exit 0 ;;
      drop)   alive=no
              if [[ -s "$STUB_PYTEST_PID" ]] && kill -0 "$(cat "$STUB_PYTEST_PID")" 2>/dev/null; then alive=yes; fi
              echo "admin drop $3 $4 pytest_alive=$alive" >> "$STUB_LOG"
              exit "${STUB_DROP_RC:-0}" ;;
    esac ;;
esac
cat >/dev/null
echo "unexpected $*" >> "$STUB_LOG"
exit 97
"""


def _a_utf8_locale() -> str | None:
    """A locale whose collation is NOT the C one, if this machine has any.

    That is where `[a-z]` in bash stops meaning "the 26 lowercase letters".
    """
    try:
        have = subprocess.run(["locale", "-a"], capture_output=True, text=True, timeout=10).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return None
    for wanted in ("en_US.UTF-8", "en_US.utf8", "de_DE.UTF-8", "de_DE.utf8", "C.UTF-8", "C.utf8"):
        if wanted in have:
            return wanted
    return None


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
    log = tmp_path / "calls.log"
    log.write_text("")

    def _env(suffix, stub_env):
        env = {k: v for k, v in os.environ.items() if k not in ("RENFIELD_TEST_DB_SUFFIX", "LC_ALL", "LC_COLLATE")}
        env["PATH"] = f"{stub_dir}{os.pathsep}{env.get('PATH', '')}"
        env["DATABASE_URL"] = _APP_URL
        env["STUB_LOG"] = str(log)
        env["STUB_PYTEST_PID"] = str(tmp_path / "pytest.pid")
        env["STUB_REAL_PYTHON"] = sys.executable
        env["STUB_TOKEN"] = _TOKEN
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

    def _popen(suffix, **stub_env):
        return subprocess.Popen(
            [shell, str(_SCRIPT), str(_REPO)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=_env(suffix, stub_env), stdin=subprocess.DEVNULL,
            # A shell cannot trap a signal it inherited as IGNORED, and this
            # test may itself run under `nohup` (SIGHUP) or as a background
            # job (SIGINT, SIGQUIT) -- the throwaway mode starts pytest so.
            preexec_fn=_default_signals,
        )

    return _run, _popen, log


def _calls(log: Path) -> list[str]:
    return log.read_text().splitlines()


_DROP_A1 = f"admin drop renfield_test_a1 {_TOKEN} pytest_alive=no"


def test_without_the_variable_the_shared_database_is_used_and_nothing_is_created(script):
    run, _, log = script
    proc = run(None, "tests/x.py", "-q", STUB_PYTEST_RC=5)
    assert proc.returncode == 5, proc.stderr
    assert _calls(log) == [
        "derive-url",
        "pytest -m db=renfield_test scheme=postgresql+asyncpg stdin=[] "
        "args=-o asyncio_mode=auto -p no:cacheprovider tests/x.py -q",
    ]
    assert "//<redacted>@db:5432/renfield_test" in proc.stdout
    assert "s3cr3t-pw" not in proc.stdout + proc.stderr


def test_an_empty_variable_is_the_shared_database_too(script):
    run, _, log = script
    assert run("").returncode == 0
    assert [c.split(" stdin=")[0] for c in _calls(log)] == ["derive-url", "pytest -m db=renfield_test scheme=postgresql+asyncpg"]


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
    run, _, log = script
    wrong = []
    for suffix in _generated_suffixes("a9_Aé", 2):
        log.write_text("")
        by_shell = run(suffix).returncode != 2
        by_python = bool(admin.NAME_RE.fullmatch("renfield_test_" + suffix))
        if by_shell != by_python or by_python != _rule(suffix):
            wrong.append((suffix, by_shell, by_python))
        if not by_shell:
            assert _calls(log) == [], suffix
    assert wrong == []


def test_a_run_of_its_own_creates_points_pytest_at_it_and_drops(script):
    run, _, log = script
    proc = run("agent_7", "-q")
    assert proc.returncode == 0, proc.stderr
    assert _calls(log) == [
        "derive-url",
        "admin create renfield_test_agent_7",
        "pytest -c db=renfield_test_agent_7 scheme=postgresql+asyncpg stdin=[] "
        "args=-o asyncio_mode=auto -p no:cacheprovider -q",
        f"admin drop renfield_test_agent_7 {_TOKEN} pytest_alive=no",
    ]
    assert "//<redacted>@db:5432/renfield_test_agent_7" in proc.stdout
    assert "s3cr3t-pw" not in proc.stdout + proc.stderr


def test_pytest_gets_the_scripts_own_stdin_on_a_database_of_its_own(script):
    # A background command of a non-interactive shell gets /dev/null; `--pdb`
    # would have nothing to read from.
    run, _, log = script
    proc = run("a1", stdin_text="typed into the debugger")
    assert proc.returncode == 0, proc.stderr
    assert "stdin=[typed into the debugger]" in _calls(log)[2]


@pytest.mark.parametrize("rc", [1, 2, 137, 143])
def test_the_database_is_dropped_when_pytest_fails_and_its_code_is_kept(script, rc):
    run, _, log = script
    proc = run("a1", STUB_PYTEST_RC=rc)
    assert proc.returncode == rc
    assert _calls(log)[-1] == _DROP_A1


def test_an_existing_database_stops_the_run_and_is_never_dropped(script):
    run, _, log = script
    proc = run("a1", STUB_EXISTS=1)
    assert proc.returncode == 3
    assert _calls(log) == ["derive-url", "admin create renfield_test_a1"], "no pytest, and above all no drop"


def test_a_failed_drop_is_reported_and_does_not_mask_the_result(script):
    run, _, log = script
    proc = run("a1", STUB_DROP_RC=4, STUB_PYTEST_RC=1)
    assert proc.returncode == 1
    assert "renfield_test_a1 was NOT dropped" in proc.stderr
    assert sum(c.startswith("admin drop") for c in _calls(log)) == 1


def _signal_a_running_pytest(popen, tmp_path, signum, mode):
    pidfile = tmp_path / "pytest.pid"
    proc = popen("a1", STUB_PYTEST_MODE=mode)
    try:
        deadline = time.monotonic() + 30
        while not (pidfile.exists() and pidfile.read_text().strip()):
            assert proc.poll() is None, proc.stderr.read()
            assert time.monotonic() < deadline, "the stub pytest never started"
            time.sleep(0.02)
        pytest_pid = int(pidfile.read_text())
        time.sleep(0.1)  # let the stub reach its `sleep` / install its trap
        proc.send_signal(signum)
        return proc.wait(timeout=60), pytest_pid
    finally:
        if proc.poll() is None:  # pragma: no cover - only on a failing run
            proc.kill()


@pytest.mark.parametrize(
    "signum,code",
    [(signal.SIGTERM, 143), (signal.SIGINT, 130), (signal.SIGHUP, 129), (signal.SIGQUIT, 131)],
)
def test_a_signal_stops_pytest_and_drops_the_database(script, tmp_path, signum, code):
    _, popen, log = script
    returncode, pytest_pid = _signal_a_running_pytest(popen, tmp_path, signum, "hang")
    assert returncode == code
    calls = _calls(log)
    assert calls[-1] == _DROP_A1, "the drop must be issued, and only once pytest is dead"
    assert sum(c.startswith("admin drop") for c in calls) == 1, "signal trap and EXIT trap must not both drop"
    with pytest.raises(ProcessLookupError):
        os.kill(pytest_pid, 0)


@pytest.mark.parametrize("mode", ["slow-term", "ignore-term"])
def test_the_database_is_never_dropped_under_a_pytest_that_is_still_alive(script, tmp_path, mode):
    # A pytest that takes its time over SIGTERM, and one that ignores it (a
    # test installed its own handler, or hangs in one): the script waits, then
    # kills -- and only THEN drops. The stub's drop records what it saw.
    _, popen, log = script
    returncode, pytest_pid = _signal_a_running_pytest(popen, tmp_path, signal.SIGTERM, mode)
    assert returncode == 143
    assert _calls(log)[-1] == _DROP_A1
    with pytest.raises(ProcessLookupError):
        os.kill(pytest_pid, 0)


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


def _own_name() -> str:
    return f"renfield_test_selftest_{secrets.token_hex(4)}"


class _Server:
    """Two maintenance connections plus the bookkeeping to clean up by token."""

    def __init__(self, admin, url):
        self.admin, self.url = admin, url
        self.conn = self.other = None
        self.created: dict[str, str] = {}

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


@pytest.fixture
def server(admin, pg_url):
    return _Server(admin, pg_url)


@pytest.mark.postgres
class TestTheRealHelperOnRealPostgres:
    def test_the_whole_life_of_a_database_of_ones_own(self, admin, server):
        name = _own_name()

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
        name = _own_name()

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
        name = _own_name()

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
        name = _own_name()

        async def scenario():
            async with server:
                token = await server.create(name)
                await admin._drop_unmarked(server.conn, name)
                assert await server.exists(name)
                assert await admin.drop(server.conn, name, token) is True

        asyncio.run(scenario())

    def test_of_several_creates_at_once_one_wins_and_the_rest_are_refused(self, admin, server):
        name = _own_name()

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
    assert os.environ.get("PROBE_FAIL") != "1"
'''


@pytest.fixture
def real_script(admin, pg_url, tmp_path):
    """The real script, the real helper, the real launcher -- no stub in PATH.

    The script derives its server from DATABASE_URL, as it does in real use; it
    must be the server the test URL points at, or the test would look for its
    database in the wrong place.
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

    def _run(suffix, **extra):
        env = {k: v for k, v in os.environ.items() if k != "RENFIELD_TEST_PG_URL"}
        env["PATH"] = f"{Path(sys.executable).parent}{os.pathsep}{env.get('PATH', '')}"
        env["RENFIELD_TEST_DB_SUFFIX"] = suffix
        env["PROBE_OUT"] = str(out)
        env.update(extra)
        proc = subprocess.run(
            ["sh", str(_SCRIPT), str(_REPO), str(inner), "-q"],
            capture_output=True, text=True, env=env, timeout=300, stdin=subprocess.DEVNULL,
        )
        password = app.password
        if password:
            assert password not in proc.stdout + proc.stderr, "the script printed the database password"
        return proc

    return _run, out, _Server(admin, pg_url)


@pytest.mark.postgres
class TestTheRealScriptOnRealPostgres:
    @pytest.mark.parametrize("fail,code", [("0", 0), ("1", 1)])
    def test_pytest_runs_inside_its_own_database_which_is_gone_afterwards(self, admin, real_script, fail, code):
        run, out, server = real_script
        suffix = f"selftest_{secrets.token_hex(4)}"
        name = f"renfield_test_{suffix}"

        proc = run(suffix, PROBE_FAIL=fail)

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
        suffix = f"selftest_{secrets.token_hex(4)}"
        name = f"renfield_test_{suffix}"

        async def scenario():
            async with server:
                token = await server.create(name)
                proc = await asyncio.to_thread(run, suffix)
                assert proc.returncode == admin.EXIT_EXISTS, proc.stdout + proc.stderr
                assert "already exists" in proc.stderr
                assert not out.exists(), "pytest must not have run"
                assert await server.mark(name) == admin.MARK + token
                assert await admin.drop(server.conn, name, token) is True

        asyncio.run(scenario())
