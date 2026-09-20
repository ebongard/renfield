"""Runtime of the ``bin/backfill_*.py`` scripts: where they find the backend, and
that ``bin/backfill_paperless_metadata.py`` actually terminates with the right exit
code (2026-09-15: copied to /tmp in the pod it only ran with ``PYTHONPATH=/app``, and
after the summary the process never exited)."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO = Path(__file__).resolve().parents[2]
_BIN = _REPO / "bin"
_BACKEND = _REPO / "src" / "backend"
_SCRIPT = _BIN / "backfill_paperless_metadata.py"
# Every operator script that carries the bootstrap block — the backfills plus the
# SECRET_KEY rotation walker (BL-0357); the drift + copied-alone tests cover all.
_BACKFILL_SCRIPTS = sorted([*_BIN.glob("backfill_*.py"), _BIN / "rotate_secret_encryption.py"])
_BLOCK_RE = re.compile(r"# --- backend import path .*?# --- end backend import path -+\n", re.S)


def _load(path: Path = _SCRIPT):
    spec = importlib.util.spec_from_file_location(f"_under_test_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake_backend(root: Path) -> Path:
    (root / "services").mkdir(parents=True)
    (root / "services" / "__init__.py").write_text("")
    (root / "utils").mkdir()
    (root / "utils" / "config.py").write_text("")
    return root


def _env_without_paths(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "RENFIELD_BACKEND_DIR")}
    env.update(extra)
    return env


def _bounded_call(fn, timeout: float = 20.0):
    """Run ``fn`` in a daemon thread; fail (instead of hanging the suite) when it
    does not return in time."""
    box: dict = {}

    def target():
        try:
            box["result"] = fn()
        except BaseException as exc:  # re-raised in the test thread
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout)
    assert not thread.is_alive(), f"did not terminate within {timeout:.0f} s"
    if "error" in box:
        raise box["error"]
    return box.get("result")


# --------------------------------------------------------------------------- import path


@pytest.mark.unit
class TestFindBackendDir:
    def test_repo_layout(self, tmp_path):
        backend = _fake_backend(tmp_path / "repo" / "src" / "backend")
        script = tmp_path / "repo" / "bin" / "x.py"
        found = _load()._find_backend_dir(script, env={}, image_root=tmp_path / "no-app")
        assert found == backend

    def test_image_layout_when_copied_outside_the_repo(self, tmp_path):
        app = _fake_backend(tmp_path / "app")
        script = tmp_path / "tmp" / "x.py"
        assert _load()._find_backend_dir(script, env={}, image_root=app) == app

    def test_repo_layout_wins_over_image_layout(self, tmp_path):
        backend = _fake_backend(tmp_path / "repo" / "src" / "backend")
        app = _fake_backend(tmp_path / "app")
        found = _load()._find_backend_dir(tmp_path / "repo" / "bin" / "x.py", env={}, image_root=app)
        assert found == backend

    def test_env_override_wins(self, tmp_path):
        _fake_backend(tmp_path / "repo" / "src" / "backend")
        other = _fake_backend(tmp_path / "elsewhere")
        found = _load()._find_backend_dir(
            tmp_path / "repo" / "bin" / "x.py",
            env={"RENFIELD_BACKEND_DIR": str(other)},
            image_root=tmp_path / "no-app",
        )
        assert found == other

    def test_invalid_env_override_is_an_error_not_a_fallback(self, tmp_path, capsys):
        _fake_backend(tmp_path / "repo" / "src" / "backend")
        with pytest.raises(SystemExit) as exc:
            _load()._find_backend_dir(
                tmp_path / "repo" / "bin" / "x.py",
                env={"RENFIELD_BACKEND_DIR": str(tmp_path / "wrong")},
                image_root=tmp_path / "no-app",
            )
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "RENFIELD_BACKEND_DIR" in err and str(tmp_path / "wrong") in err

    def test_nothing_found_names_every_candidate(self, tmp_path, capsys):
        with pytest.raises(SystemExit) as exc:
            _load()._find_backend_dir(tmp_path / "tmp" / "x.py", env={}, image_root=tmp_path / "no-app")
        assert exc.value.code == 2
        err = capsys.readouterr().err
        assert "src/backend" in err and str(tmp_path / "no-app") in err


@pytest.mark.unit
def test_every_backfill_script_carries_the_identical_bootstrap_block():
    blocks = {p.name: _BLOCK_RE.findall(p.read_text()) for p in _BACKFILL_SCRIPTS}
    assert len(blocks) >= 7
    assert all(len(b) == 1 for b in blocks.values()), {n: len(b) for n, b in blocks.items()}
    assert len({b[0] for b in blocks.values()}) == 1, "bootstrap blocks drifted apart"


@pytest.mark.unit
@pytest.mark.parametrize("script", _BACKFILL_SCRIPTS, ids=lambda p: p.name)
def test_script_copied_outside_the_repo_runs_without_pythonpath(script, tmp_path):
    """The pod workflow: the script alone in /tmp, no PYTHONPATH, foreign cwd."""
    copy = tmp_path / script.name
    shutil.copy(script, copy)
    proc = subprocess.run(
        [sys.executable, str(copy), "--help"],
        cwd=tmp_path,
        env=_env_without_paths(RENFIELD_BACKEND_DIR=str(_BACKEND)),
        capture_output=True, text=True, timeout=180,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "usage" in proc.stdout.lower()


@pytest.mark.unit
def test_script_without_a_backend_exits_2_with_a_clear_message(tmp_path):
    copy = tmp_path / _SCRIPT.name
    shutil.copy(_SCRIPT, copy)
    proc = subprocess.run(
        [sys.executable, str(copy), "--help"],
        cwd=tmp_path,
        env=_env_without_paths(RENFIELD_BACKEND_DIR=str(tmp_path / "missing")),
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 2
    assert "Renfield backend not found" in proc.stderr and "RENFIELD_BACKEND_DIR" in proc.stderr


# --------------------------------------------------------------------------- bounded exit


async def _ignore_cancellation_forever():
    while True:
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            continue


class _WedgedManager:
    """Stands in for an MCPManager whose transport teardown never completes."""

    def __init__(self, *, configured=True, connects=True, update_fails=False):
        self.configured, self.connects, self.update_fails = configured, connects, update_fails
        self._servers: dict = {}
        self.load_args: dict = {}
        self.shutdown_calls = 0
        self.tool_calls: list[str] = []

    def load_config(self, path, only=None, overlay_dir=None):
        self.load_args = {"path": path, "only": only, "overlay_dir": overlay_dir}
        if self.configured:
            self._servers["paperless"] = SimpleNamespace(connected=False, last_error=None, rate_limiter=None)

    async def connect_all(self):
        state = self._servers["paperless"]
        state.connected = self.connects
        state.last_error = None if self.connects else "connection refused"

    async def execute_tool(self, name, arguments):
        self.tool_calls.append(name)
        if name.endswith("update_document") and self.update_fails:
            return {"success": False, "message": "HTTP 500"}
        return {"success": True, "message": json.dumps({"id": arguments.get("document_id"), "correspondent": ""})}

    async def shutdown(self):
        self.shutdown_calls += 1
        await _ignore_cancellation_forever()


@pytest.mark.unit
class TestBoundedTeardown:
    def test_shutdown_returns_although_the_session_close_hangs(self):
        module = _load()
        manager = _WedgedManager()
        start = time.monotonic()
        _bounded_call(lambda: module._run(module._shutdown(manager, timeout=0.2), teardown_timeout=0.2))
        assert manager.shutdown_calls == 1
        assert time.monotonic() - start < 5

    def test_run_returns_although_a_leftover_task_ignores_cancellation(self):
        module = _load()

        async def work():
            asyncio.get_running_loop().create_task(_ignore_cancellation_forever())
            await asyncio.sleep(0)
            return 42

        assert _bounded_call(lambda: module._run(work(), teardown_timeout=0.2)) == 42

    def test_asyncio_run_is_what_hangs_on_such_a_task(self):
        """The mechanism the bounded runner replaces: asyncio.run() waits for every
        cancelled leftover task without a limit. In a subprocess, killed on timeout,
        so nothing keeps running inside the test session."""
        code = (
            "import asyncio\n"
            "async def stubborn():\n"
            "    while True:\n"
            "        try:\n"
            "            await asyncio.sleep(3600)\n"
            "        except asyncio.CancelledError:\n"
            "            continue\n"
            "async def work():\n"
            "    asyncio.get_running_loop().create_task(stubborn())\n"
            "    await asyncio.sleep(0)\n"
            "asyncio.run(work())\n"
        )
        with pytest.raises(subprocess.TimeoutExpired):
            subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=3)

    def test_run_propagates_the_error_and_still_terminates(self):
        module = _load()

        async def work():
            asyncio.get_running_loop().create_task(_ignore_cancellation_forever())
            raise ValueError("boom")

        with pytest.raises(ValueError):
            _bounded_call(lambda: module._run(work(), teardown_timeout=0.2))


_EXECUTOR_DRIVER = """
import asyncio, atexit, importlib.util, sys, time
spec = importlib.util.spec_from_file_location("bpm_exit", sys.argv[1])
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
m.LOOP_TEARDOWN_TIMEOUT_S = 0.5
m.THREAD_EXIT_GRACE_S = 0.2
block_s, ok = float(sys.argv[2]), sys.argv[3] == "ok"

async def fake_run_created_date(**kwargs):
    if block_s:
        # A blocking executor job, like Docling OCR on a stuck PDF.
        asyncio.get_running_loop().run_in_executor(None, time.sleep, block_s)
    await asyncio.sleep(0)
    print("SUMMARY", flush=True)
    return ok

m._run_created_date = fake_run_created_date
atexit.register(lambda: print("ATEXIT", flush=True))
m._terminate(m.main(["--mode", "created-date"]))
"""


@pytest.mark.unit
class TestProcessTermination:
    def _run_driver(self, tmp_path, block_s: float, ok: bool):
        driver = tmp_path / "driver.py"
        driver.write_text(_EXECUTOR_DRIVER)
        start = time.monotonic()
        proc = subprocess.run(
            [sys.executable, str(driver), str(_SCRIPT), str(block_s), "ok" if ok else "fail"],
            cwd=tmp_path,
            env=_env_without_paths(RENFIELD_BACKEND_DIR=str(_BACKEND)),
            capture_output=True, text=True, timeout=120,
        )
        return proc, time.monotonic() - start

    @pytest.mark.parametrize(("ok", "code"), [(True, 0), (False, 1)])
    def test_blocked_executor_job_does_not_keep_the_process_alive(self, tmp_path, ok, code):
        proc, elapsed = self._run_driver(tmp_path, block_s=60, ok=ok)
        assert proc.returncode == code, proc.stderr[-2000:]
        assert "SUMMARY" in proc.stdout
        assert elapsed < 30, f"process lived {elapsed:.0f} s — it waited for the 60 s executor job"
        assert "exiting without waiting" in proc.stderr
        assert "ATEXIT" not in proc.stdout  # the os._exit path

    @pytest.mark.parametrize(("ok", "code"), [(True, 0), (False, 1)])
    def test_normal_exit_path_without_stuck_threads(self, tmp_path, ok, code):
        proc, _ = self._run_driver(tmp_path, block_s=0, ok=ok)
        assert proc.returncode == code, proc.stderr[-2000:]
        assert "ATEXIT" in proc.stdout  # regular interpreter shutdown, no os._exit
        assert "exiting without waiting" not in proc.stderr


# --------------------------------------------------------------------------- exit codes


@pytest.fixture
def cli(monkeypatch):
    module = _load()
    monkeypatch.setattr(module, "MCP_SHUTDOWN_TIMEOUT_S", 0.2)
    monkeypatch.setattr(module, "LOOP_TEARDOWN_TIMEOUT_S", 0.2)
    return module


def _patch_manager(monkeypatch, manager):
    import services.mcp_client as mcp_client

    monkeypatch.setattr(mcp_client, "MCPManager", lambda: manager)


def _patch_backfill(monkeypatch, **report_fields):
    import services.paperless_metadata_backfill as bf

    calls = []

    async def fake_backfill(session_factory, mcp_manager, **kwargs):
        calls.append(kwargs)
        return bf.CreatedDateReport(commit=kwargs["commit"], **report_fields)

    monkeypatch.setattr(bf, "backfill_created_dates", fake_backfill)
    return calls


@pytest.mark.unit
class TestExitCodes:
    def test_success_exits_0_despite_a_hanging_mcp_shutdown(self, cli, monkeypatch, capsys):
        manager = _WedgedManager()
        _patch_manager(monkeypatch, manager)
        calls = _patch_backfill(monkeypatch, candidates=1, would_patch=[7], last_pid=7)
        start = time.monotonic()
        assert _bounded_call(lambda: cli.main(["--mode", "created-date"])) == 0
        assert time.monotonic() - start < 10
        assert calls and manager.shutdown_calls == 1
        assert manager.load_args["only"] == {"paperless"}
        assert '"would_patch": 1' in capsys.readouterr().out

    def test_failed_patch_exits_1(self, cli, monkeypatch):
        _patch_manager(monkeypatch, _WedgedManager())
        _patch_backfill(monkeypatch, candidates=1, failed=[7], last_pid=7)
        assert _bounded_call(lambda: cli.main(["--mode", "created-date", "--commit"])) == 1

    def test_no_candidates_exits_0(self, cli, monkeypatch):
        _patch_manager(monkeypatch, _WedgedManager())
        _patch_backfill(monkeypatch, candidates=0)
        assert _bounded_call(lambda: cli.main(["--mode", "created-date", "--commit"])) == 0

    def test_dry_run_with_every_candidate_unreachable_exits_1(self, cli, monkeypatch):
        """MCP connected, but Paperless answers every call with an error."""
        _patch_manager(monkeypatch, _WedgedManager())
        _patch_backfill(monkeypatch, candidates=3, unreachable=[1, 2, 3], last_pid=3)
        assert _bounded_call(lambda: cli.main(["--mode", "created-date"])) == 1

    def test_dry_run_with_some_unreachable_exits_0_with_a_warning(self, cli, monkeypatch, caplog):
        _patch_manager(monkeypatch, _WedgedManager())
        _patch_backfill(monkeypatch, candidates=3, unreachable=[2], would_patch=[1], already_correct=1, last_pid=3)
        with caplog.at_level(logging.WARNING, logger="backfill_paperless_metadata"):
            assert _bounded_call(lambda: cli.main(["--mode", "created-date"])) == 0
        assert "1 of 3 document(s) unreachable" in caplog.text
        assert "--after-pid 3" in caplog.text

    def test_commit_with_any_unreachable_exits_1(self, cli, monkeypatch, caplog):
        _patch_manager(monkeypatch, _WedgedManager())
        _patch_backfill(monkeypatch, candidates=3, unreachable=[2], patched=[1, 3], last_pid=3)
        with caplog.at_level(logging.WARNING, logger="backfill_paperless_metadata"):
            assert _bounded_call(lambda: cli.main(["--mode", "created-date", "--commit"])) == 1
        assert "--after-pid 3" in caplog.text

    def test_paperless_not_connected_exits_1_without_running_the_backfill(self, cli, monkeypatch):
        manager = _WedgedManager(connects=False)
        _patch_manager(monkeypatch, manager)
        calls = _patch_backfill(monkeypatch)
        assert _bounded_call(lambda: cli.main(["--mode", "created-date"])) == 1
        assert calls == [] and manager.shutdown_calls == 1

    def test_paperless_not_configured_exits_1(self, cli, monkeypatch):
        _patch_manager(monkeypatch, _WedgedManager(configured=False))
        calls = _patch_backfill(monkeypatch)
        assert _bounded_call(lambda: cli.main(["--mode", "created-date"])) == 1
        assert calls == []

    def test_unexpected_error_exits_1(self, cli, monkeypatch):
        async def boom(**kwargs):
            raise RuntimeError("db down")

        monkeypatch.setattr(cli, "_run_created_date", boom)
        assert _bounded_call(lambda: cli.main(["--mode", "created-date"])) == 1


def _patch_correspondent_env(monkeypatch, tmp_path):
    """One filed document without a correspondent; extraction resolves one."""
    import services.database as database
    import services.folder_ingest_paperless as fip
    import services.paperless_metadata_extractor as pme

    recovery = tmp_path / "doc.pdf"
    recovery.write_bytes(b"%PDF-1.4")
    doc = SimpleNamespace(id=1, paperless_document_id=7, filename="doc.pdf", file_path=str(recovery), user_id=None)

    class _Result:
        def scalars(self):
            return SimpleNamespace(all=lambda: [doc])

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, stmt):
            return _Result()

        async def commit(self):
            return None

    class _Extractor:
        def __init__(self, mcp_manager):
            pass

        async def extract_from_file(self, path, user_id=None, lang="de"):
            return SimpleNamespace(error=None, metadata={})

    async def names(manager):
        return ["ACME"]

    async def resolve(manager, metadata, names=None, create=True):
        return "ACME"

    monkeypatch.setattr(database, "AsyncSessionLocal", lambda: _Session())
    monkeypatch.setattr(fip, "_fetch_correspondent_names", names)
    monkeypatch.setattr(fip, "resolve_correspondent_from_metadata", resolve)
    monkeypatch.setattr(pme, "PaperlessMetadataExtractor", _Extractor)


@pytest.mark.unit
class TestCorrespondentExitCodes:
    def test_failed_write_in_commit_exits_1(self, cli, monkeypatch, tmp_path):
        manager = _WedgedManager(update_fails=True)
        _patch_manager(monkeypatch, manager)
        _patch_correspondent_env(monkeypatch, tmp_path)
        assert _bounded_call(lambda: cli.main(["--mode", "correspondent", "--commit"])) == 1
        assert "mcp.paperless.update_document" in manager.tool_calls

    def test_successful_write_in_commit_exits_0(self, cli, monkeypatch, tmp_path):
        manager = _WedgedManager()
        _patch_manager(monkeypatch, manager)
        _patch_correspondent_env(monkeypatch, tmp_path)
        assert _bounded_call(lambda: cli.main(["--mode", "correspondent", "--commit"])) == 0
        assert "mcp.paperless.update_document" in manager.tool_calls

    def test_dry_run_writes_nothing_and_exits_0(self, cli, monkeypatch, tmp_path):
        manager = _WedgedManager(update_fails=True)
        _patch_manager(monkeypatch, manager)
        _patch_correspondent_env(monkeypatch, tmp_path)
        assert _bounded_call(lambda: cli.main(["--mode", "correspondent"])) == 0
        assert "mcp.paperless.update_document" not in manager.tool_calls
