"""``bin/rotate_secret_encryption.py`` — the only caller of the destructive
``--commit`` step of a SECRET_KEY rotation (BL-0357). Pins the exit-code contract
(2 = nothing to rotate FROM, 1 = rows no key can read, 0 = clean), the parsing
parity with the decryptor (a comma-only SECRET_KEY_PREVIOUS is NO previous key),
and that stdout carries counts only — never a token."""
from __future__ import annotations

import asyncio
import importlib.util
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "bin" / "rotate_secret_encryption.py"


def _load():
    spec = importlib.util.spec_from_file_location("_under_test_rotate_secret", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def cli(monkeypatch):
    module = _load()

    @asynccontextmanager
    async def _fake_session_factory():
        yield object()

    monkeypatch.setattr(module, "AsyncSessionLocal", _fake_session_factory)
    return module


def _report(**fields):
    from ha_glue.services.secret_rotation import RotationReport
    return RotationReport(**fields)


@pytest.mark.unit
class TestExitCodes:
    @pytest.mark.parametrize("prev_count", [0])
    def test_no_previous_key_exits_2_without_touching_the_db(self, cli, monkeypatch, capsys, prev_count):
        monkeypatch.setattr(cli, "previous_key_count", lambda: prev_count)
        walker = AsyncMock()
        monkeypatch.setattr(cli, "rotate_irks", walker)
        assert asyncio.run(cli.main(commit=True)) == 2
        assert "SECRET_KEY_PREVIOUS" in capsys.readouterr().err
        walker.assert_not_awaited()

    def test_comma_only_previous_is_no_previous_key(self, cli, monkeypatch, capsys):
        # Parsing parity with the decryptor: ' , ' would otherwise pass a naive
        # emptiness gate and mislabel every row as "undecryptable".
        from pydantic import SecretStr

        import services.secret_encryption as se
        monkeypatch.setattr(se.settings, "secret_key_previous", SecretStr(" , "), raising=False)
        monkeypatch.setattr(cli, "rotate_irks", AsyncMock())
        assert asyncio.run(cli.main(commit=False)) == 2

    def test_undecryptable_rows_exit_1_with_pairing_advice(self, cli, monkeypatch, capsys):
        monkeypatch.setattr(cli, "previous_key_count", lambda: 1)
        monkeypatch.setattr(cli, "rotate_irks", AsyncMock(return_value=_report(total=3, rotated=2, undecryptable=1, committed=True)))
        assert asyncio.run(cli.main(commit=True)) == 1
        out, err = capsys.readouterr()
        assert "[COMMIT]" in out and "'undecryptable': 1" in out
        assert "pairing flow" in err

    def test_clean_run_exits_0_and_prints_counts_only(self, cli, monkeypatch, capsys):
        monkeypatch.setattr(cli, "previous_key_count", lambda: 1)
        walker = AsyncMock(return_value=_report(total=2, already_current=1, rotated=1, committed=False))
        monkeypatch.setattr(cli, "rotate_irks", walker)
        assert asyncio.run(cli.main(commit=False)) == 0
        out, err = capsys.readouterr()
        assert "[DRY RUN]" in out and "'rotated': 1" in out
        assert err == ""
        # counts only: a Fernet token starts with 'gAAAA'; none may appear
        assert "gAAAA" not in out
        walker.assert_awaited_once()
        assert walker.await_args.kwargs == {"commit": False}


@pytest.mark.unit
def test_dry_run_and_commit_are_mutually_exclusive_and_required(tmp_path):
    env = {"RENFIELD_BACKEND_DIR": str(_REPO / "src" / "backend"), "PATH": "/usr/bin:/bin"}
    both = subprocess.run([sys.executable, str(_SCRIPT), "--dry-run", "--commit"],
                          capture_output=True, text=True, env=env, timeout=180)
    assert both.returncode == 2 and "not allowed with" in both.stderr
    none = subprocess.run([sys.executable, str(_SCRIPT)], capture_output=True, text=True, env=env, timeout=180)
    assert none.returncode == 2 and "required" in none.stderr
