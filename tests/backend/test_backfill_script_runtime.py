"""Runtime of the ``bin/backfill_*.py`` scripts: where they find the backend, and
that ``bin/backfill_paperless_metadata.py`` actually terminates with the right exit
code (2026-09-15: copied to /tmp in the pod it only ran with ``PYTHONPATH=/app``, and
after the summary the process never exited)."""

from __future__ import annotations

import asyncio
import importlib.util
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
_BACKFILL_SCRIPTS = sorted(_BIN.glob("backfill_*.py"))
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

    def __init__(self, *, configured=True, connects=True):
        self.configured, self.connects = configured, connects
        self._servers: dict = {}
        self.load_args: dict = {}
        self.shutdown_calls = 0

    def load_config(self, path, only=None, overlay_dir=None):
        self.load_args = {"path": path, "only": only, "overlay_dir": overlay_dir}
        if self.configured:
            self._servers["paperless"] = SimpleNamespace(connected=False, last_error=None, rate_limiter=None)

    async def connect_all(self):
        state = self._servers["paperless"]
        state.connected = self.connects
        state.last_error = None if self.connects else "connection refused"

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
        cancelled leftover task without a limit."""

        async def work():
            asyncio.get_running_loop().create_task(_ignore_cancellation_forever())
            await asyncio.sleep(0)

        thread = threading.Thread(target=lambda: asyncio.run(work()), daemon=True)
        thread.start()
        thread.join(1.0)
        assert thread.is_alive()

    def test_run_propagates_the_error_and_still_terminates(self):
        module = _load()

        async def work():
            asyncio.get_running_loop().create_task(_ignore_cancellation_forever())
            raise ValueError("boom")

        with pytest.raises(ValueError):
            _bounded_call(lambda: module._run(work(), teardown_timeout=0.2))


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
