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
its redaction -- is the real code.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
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
    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
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


@pytest.mark.parametrize("name", ["renfield_test_a", "renfield_test_agent_7", "renfield_test_" + "a" * 40, "renfield_test_9x"])
def test_a_throwaway_name_is_accepted_here_and_by_the_harness_guard(admin, name):
    admin.check_name(name, _APP_URL)
    assert _TEST_DB_NAME_RE.fullmatch(name), "the harness would refuse a name this script creates"


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


def test_the_application_database_is_refused_even_under_a_matching_name(admin):
    with pytest.raises(admin.Refused, match="application database"):
        admin.check_name("renfield_test_app", "postgresql+asyncpg://u:p@db/renfield_test_app?ssl=disable")


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
# which reads it; every other call gets /dev/null from the script or the test.
_PYTHON_STUB = r"""#!/bin/bash
if [[ "$1" == "-" ]]; then
  echo "derive-url" >> "$STUB_LOG"
  exec "$STUB_REAL_PYTHON" -
fi
cat >/dev/null
case "$1" in
  -m|-c)
    # `-m pytest` on the shared database, `-c <launcher>` on a run's own.
    how="$1"; shift 2
    db="${RENFIELD_TEST_PG_URL##*/}"
    echo "pytest $how db=$db scheme=${RENFIELD_TEST_PG_URL%%://*} args=$*" >> "$STUB_LOG"
    if [[ -n "${STUB_PYTEST_HANG:-}" ]]; then
      echo $$ > "$STUB_PYTEST_HANG"
      exec sleep 60
    fi
    exit "${STUB_PYTEST_RC:-0}" ;;
  */throwaway_test_db.py)
    echo "admin $2 $3 ${4:-}" >> "$STUB_LOG"
    case "$2" in
      create) [[ -n "${STUB_EXISTS:-}" ]] && { echo "REFUSED: exists" >&2; exit 3; }
              echo "$STUB_TOKEN"; exit 0 ;;
      drop)   exit "${STUB_DROP_RC:-0}" ;;
    esac ;;
esac
echo "unexpected $*" >> "$STUB_LOG"
exit 97
"""


@pytest.fixture
def script(tmp_path):
    """Returns (run, popen, log): the real script with the stub first in PATH."""
    if shutil.which("bash") is None:  # pragma: no cover - environment guard
        pytest.skip("bash not available")
    assert _SCRIPT.is_file(), f"incomplete copy: {_SCRIPT} is missing (rsync bin/ too)"
    stub_dir = tmp_path / "stub"
    stub_dir.mkdir()
    stub = stub_dir / "python"
    stub.write_text(_PYTHON_STUB)
    stub.chmod(0o755)
    log = tmp_path / "calls.log"
    log.write_text("")

    def _env(suffix, stub_env):
        env = {k: v for k, v in os.environ.items() if k != "RENFIELD_TEST_DB_SUFFIX"}
        env["PATH"] = f"{stub_dir}{os.pathsep}{env.get('PATH', '')}"
        env["DATABASE_URL"] = _APP_URL
        env["STUB_LOG"] = str(log)
        env["STUB_REAL_PYTHON"] = sys.executable
        env["STUB_TOKEN"] = _TOKEN
        if suffix is not None:
            env["RENFIELD_TEST_DB_SUFFIX"] = suffix
        env.update({k: str(v) for k, v in stub_env.items()})
        return env

    def _run(suffix=None, *pytest_args, **stub_env):
        return subprocess.run(
            ["sh", str(_SCRIPT), str(_REPO), *pytest_args],
            capture_output=True, text=True, env=_env(suffix, stub_env), timeout=60,
            stdin=subprocess.DEVNULL,
        )

    def _popen(suffix, **stub_env):
        return subprocess.Popen(
            ["sh", str(_SCRIPT), str(_REPO)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=_env(suffix, stub_env), stdin=subprocess.DEVNULL,
            # A shell cannot trap a signal it inherited as IGNORED, and this
            # test may itself run under `nohup` (SIGHUP) or as a background
            # job (SIGINT) -- the throwaway mode starts pytest exactly so.
            preexec_fn=_default_signals,
        )

    return _run, _popen, log


def _calls(log: Path) -> list[str]:
    return log.read_text().splitlines()


def test_without_the_variable_the_shared_database_is_used_and_nothing_is_created(script):
    run, _, log = script
    proc = run(None, "tests/x.py", "-q", STUB_PYTEST_RC=5)
    assert proc.returncode == 5, proc.stderr
    assert _calls(log) == [
        "derive-url",
        "pytest -m db=renfield_test scheme=postgresql+asyncpg "
        "args=-o asyncio_mode=auto -p no:cacheprovider tests/x.py -q",
    ]
    assert "//<redacted>@db:5432/renfield_test" in proc.stdout
    assert "s3cr3t-pw" not in proc.stdout + proc.stderr


def test_an_empty_variable_is_the_shared_database_too(script):
    run, _, log = script
    assert run("").returncode == 0
    assert [c.split(" args=")[0] for c in _calls(log)] == ["derive-url", "pytest -m db=renfield_test scheme=postgresql+asyncpg"]


@pytest.mark.parametrize("suffix", _BAD_SUFFIXES)
def test_a_bad_suffix_is_refused_before_anything_runs(script, suffix):
    run, _, log = script
    proc = run(suffix)
    assert proc.returncode == 2
    assert "REFUSED" in proc.stderr
    assert _calls(log) == [], "neither Postgres nor pytest may be reached with a refused name"


def test_a_run_of_its_own_creates_points_pytest_at_it_and_drops(script):
    run, _, log = script
    proc = run("agent_7", "-q")
    assert proc.returncode == 0, proc.stderr
    assert _calls(log) == [
        "derive-url",
        "admin create renfield_test_agent_7 ",
        "pytest -c db=renfield_test_agent_7 scheme=postgresql+asyncpg "
        "args=-o asyncio_mode=auto -p no:cacheprovider -q",
        f"admin drop renfield_test_agent_7 {_TOKEN}",
    ]
    assert "//<redacted>@db:5432/renfield_test_agent_7" in proc.stdout
    assert "s3cr3t-pw" not in proc.stdout + proc.stderr


@pytest.mark.parametrize("rc", [1, 2, 137, 143])
def test_the_database_is_dropped_when_pytest_fails_and_its_code_is_kept(script, rc):
    run, _, log = script
    proc = run("a1", STUB_PYTEST_RC=rc)
    assert proc.returncode == rc
    assert _calls(log)[-1] == f"admin drop renfield_test_a1 {_TOKEN}"


def test_an_existing_database_stops_the_run_and_is_never_dropped(script):
    run, _, log = script
    proc = run("a1", STUB_EXISTS=1)
    assert proc.returncode == 3
    assert _calls(log) == ["derive-url", "admin create renfield_test_a1 "], "no pytest, and above all no drop"


def test_a_failed_drop_is_reported_and_does_not_mask_the_result(script):
    run, _, log = script
    proc = run("a1", STUB_DROP_RC=4, STUB_PYTEST_RC=1)
    assert proc.returncode == 1
    assert "renfield_test_a1 was NOT dropped" in proc.stderr
    assert sum(c.startswith("admin drop") for c in _calls(log)) == 1


@pytest.mark.parametrize("signum,code", [(signal.SIGTERM, 143), (signal.SIGINT, 130), (signal.SIGHUP, 129)])
def test_a_signal_stops_pytest_and_drops_the_database(script, tmp_path, signum, code):
    _, popen, log = script
    pidfile = tmp_path / "pytest.pid"
    proc = popen("a1", STUB_PYTEST_HANG=pidfile)
    try:
        deadline = time.monotonic() + 30
        while not (pidfile.exists() and pidfile.read_text().strip()):
            assert proc.poll() is None, proc.stderr.read()
            assert time.monotonic() < deadline, "the stub pytest never started"
            time.sleep(0.02)
        pytest_pid = int(pidfile.read_text())
        proc.send_signal(signum)
        assert proc.wait(timeout=30) == code
    finally:
        if proc.poll() is None:  # pragma: no cover - only on a failing run
            proc.kill()
    calls = _calls(log)
    assert calls[-1] == f"admin drop renfield_test_a1 {_TOKEN}"
    assert sum(c.startswith("admin drop") for c in calls) == 1, "signal trap and EXIT trap must not both drop"
    with pytest.raises(ProcessLookupError):
        os.kill(pytest_pid, 0)
