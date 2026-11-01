"""The release scripts' help is reachable without ``RENFIELD_REGISTRY`` (#1399).

``bin/deploy-production.sh`` and ``bin/release-voice-server.sh`` both need the
registry -- but they enforced it BEFORE parsing their arguments, so ``--help``
died on the very variable it was supposed to explain.

Both directions are checked: the help works with the variable unset, AND a
real invocation still refuses without it (moving the check must not drop it).
The refusal happens before any ssh/kubectl/git step, so no stubs are needed;
``PATH`` is pinned to the system directories anyway so a regression that
reaches a network tool fails on a missing binary instead of touching anything.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit]

_REPO = Path(__file__).resolve().parents[2]
_DEPLOY = _REPO / "bin" / "deploy-production.sh"
_VOICE = _REPO / "bin" / "release-voice-server.sh"


def _run(script: Path, *args: str) -> subprocess.CompletedProcess:
    if shutil.which("bash") is None:  # pragma: no cover - environment guard
        pytest.skip("bash not available")
    assert script.is_file(), f"incomplete copy: {script} is missing (rsync bin/ too)"
    env = {k: v for k, v in os.environ.items() if k != "RENFIELD_REGISTRY"}
    env["PATH"] = "/usr/bin:/bin"
    return subprocess.run(
        ["bash", str(script), *args],
        capture_output=True, text=True, env=env, timeout=30,
        stdin=subprocess.DEVNULL,
    )


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_deploy_help_without_registry(flag):
    proc = _run(_DEPLOY, flag)
    assert proc.returncode == 0, proc.stderr
    assert "Usage:" in proc.stdout
    assert "RENFIELD_REGISTRY" not in proc.stderr


def test_deploy_without_registry_still_refuses():
    proc = _run(_DEPLOY, "--backend-tag", "x", "--skip-frontend", "--dry-run")
    assert proc.returncode != 0
    assert "set RENFIELD_REGISTRY" in proc.stderr
    # Refused before the first step, not somewhere inside the run.
    assert "preflight" not in proc.stdout


@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_voice_release_help_without_registry(flag):
    proc = _run(_VOICE, flag)
    assert proc.returncode == 0, proc.stderr
    assert "Usage:" in proc.stdout
    # The whole header, down to the digest-pin line, not a truncated slice.
    assert "voice-server@sha256" in proc.stdout
    assert "RENFIELD_REGISTRY" not in proc.stderr


def test_voice_release_usage_without_tag_needs_no_registry():
    proc = _run(_VOICE)
    assert proc.returncode == 2
    assert "Usage:" in proc.stdout
    assert "set RENFIELD_REGISTRY" not in proc.stderr


def test_voice_release_without_registry_still_refuses():
    proc = _run(_VOICE, "v9.9.9", "--dry-run")
    assert proc.returncode != 0
    assert "set RENFIELD_REGISTRY" in proc.stderr
    assert "guardrails" not in proc.stdout
