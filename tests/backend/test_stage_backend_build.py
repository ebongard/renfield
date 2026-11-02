"""The backend build-context staging step (#1359).

The backend Dockerfile COPYs ``wakeword-models`` and ``satellite``, which live
outside its build context. ``bin/lib/backend-build-extras.sh`` is the single
definition of how they are staged; ``bin/deploy-production.sh`` and the compose
stager ``bin/stage-backend-build.sh`` both run it. These tests pin:

- the commands the helper emits (``--delete`` only for the satellite tree, the
  provisioning/ + tests/ excludes, quoting that survives a path with a space);
- that the compose stager runs them against the LOCAL ``src/backend`` and seeds
  ``config/mail_accounts.yaml`` without ever overwriting an existing one;
- that the deploy script still emits the same two commands.

``rsync`` is replaced by a stub that records its argv: the backend test
container has no rsync, and what matters here is WHICH copy is requested --
the real copy is proven by the fresh compose build on the build box.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit]

_REPO = Path(__file__).resolve().parents[2]
_HELPER = _REPO / "bin" / "lib" / "backend-build-extras.sh"
_STAGER = _REPO / "bin" / "stage-backend-build.sh"
_DEPLOY = _REPO / "bin" / "deploy-production.sh"

# Records one line per call, each argument shell-quoted. It DRAINS stdin on
# purpose: the callers read their command list on FD 3, not stdin, so a command
# that reads stdin cannot swallow the rest of the list. A stub that reads stdin
# makes a regression to a plain stdin loop fail (only the first call lands).
_RECORDING_STUB = r"""#!/usr/bin/env bash
cat >/dev/null
line=""
for a in "$@"; do line+="$(printf '%q' "$a") "; done
printf '%s\n' "$line" >> "$STUB_LOG"
exit 0
"""


@pytest.fixture(autouse=True)
def _need_bash():
    if shutil.which("bash") is None:  # pragma: no cover - environment guard
        pytest.skip("bash not available")
    assert _HELPER.is_file(), f"incomplete copy: {_HELPER} missing (rsync bin/ too)"


def _stub_dir(tmp_path: Path, names: tuple[str, ...]) -> Path:
    stub = tmp_path / "stub"
    stub.mkdir(exist_ok=True)
    for name in names:
        path = stub / name
        path.write_text(_RECORDING_STUB if name == "rsync" else "#!/bin/bash\ncat >/dev/null\nexit 0\n")
        path.chmod(0o755)
    return stub


def _env(stub: Path, log: Path, **extra: str) -> dict[str, str]:
    env = dict(os.environ)
    env["PATH"] = f"{stub}{os.pathsep}{env.get('PATH', '')}"
    env["STUB_LOG"] = str(log)
    env.update(extra)
    return env


def _helper_lines(root: str, dest: str) -> list[str]:
    proc = subprocess.run(
        ["bash", "-c", '. "$1"; backend_extras_rsync_cmds "$2" "$3"', "_", str(_HELPER), root, dest],
        capture_output=True, text=True, check=True, timeout=30,
    )
    return proc.stdout.splitlines()


def test_helper_emits_exactly_the_two_copies():
    lines = _helper_lines("/repo", "host:/stage/src/backend")
    assert len(lines) == 2, lines
    wake, sat = (shlex.split(line) for line in lines)

    assert wake == ["rsync", "-az", "/repo/data/wakeword-models/", "host:/stage/src/backend/wakeword-models/"]
    # Never --delete into the models dir: a context that already holds models
    # must not lose them.
    assert "--delete" not in wake

    assert sat[:3] == ["rsync", "-az", "--delete"]
    assert sat[-2:] == ["/repo/src/satellite/", "host:/stage/src/backend/satellite/"]
    # The gitignored Ansible inventory carries real hostnames -- never staged.
    assert "--exclude=provisioning" in sat
    assert "--exclude=tests" in sat


def test_helper_quoting_survives_eval_with_a_space(tmp_path):
    """The callers `eval` each line; a repo root with a space must arrive as
    ONE argument, not two."""
    log = tmp_path / "rsync.log"
    stub = _stub_dir(tmp_path, ("rsync",))
    root = "/my repo"
    script = '. "$1"; while IFS= read -r -u 3 c; do eval "$c"; done 3< <(backend_extras_rsync_cmds "$2" "$3")'
    subprocess.run(
        ["bash", "-c", script, "_", str(_HELPER), root, "/dst dir"],
        env=_env(stub, log), check=True, timeout=30, stdin=subprocess.DEVNULL,
    )
    calls = [shlex.split(line) for line in log.read_text().splitlines()]
    assert calls[0][-2:] == ["/my repo/data/wakeword-models/", "/dst dir/wakeword-models/"]
    assert calls[1][-2:] == ["/my repo/src/satellite/", "/dst dir/satellite/"]


@pytest.fixture
def mini_repo(tmp_path):
    """A repo skeleton with the real stager + helper and the files it reads."""
    repo = tmp_path / "repo"
    (repo / "bin" / "lib").mkdir(parents=True)
    shutil.copy2(_STAGER, repo / "bin" / "stage-backend-build.sh")
    shutil.copy2(_HELPER, repo / "bin" / "lib" / "backend-build-extras.sh")
    (repo / "config").mkdir()
    (repo / "config" / "mail_accounts.default.yaml").write_text("accounts: []\n")
    (repo / "secrets").mkdir()
    (repo / "src" / "backend").mkdir(parents=True)
    return repo


def _stage(repo: Path, tmp_path: Path) -> tuple[subprocess.CompletedProcess, list[list[str]]]:
    log = tmp_path / "stager-rsync.log"
    log.write_text("")
    stub = _stub_dir(tmp_path, ("rsync",))
    proc = subprocess.run(
        ["bash", str(repo / "bin" / "stage-backend-build.sh")],
        env=_env(stub, log), capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
    )
    return proc, [shlex.split(line) for line in log.read_text().splitlines()]


def test_stager_targets_the_local_backend_context(mini_repo, tmp_path):
    proc, calls = _stage(mini_repo, tmp_path)
    assert proc.returncode == 0, proc.stderr
    expected = _helper_lines(str(mini_repo), str(mini_repo / "src" / "backend"))
    # The stub records its arguments, not its own name.
    assert calls == [shlex.split(line)[1:] for line in expected]


def test_stager_seeds_mail_accounts_once_and_never_overwrites(mini_repo, tmp_path):
    target = mini_repo / "config" / "mail_accounts.yaml"
    proc, _ = _stage(mini_repo, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert target.read_text() == "accounts: []\n"
    assert "[OK]   config/mail_accounts.yaml" in proc.stdout

    target.write_text("accounts: [real]\n")
    proc, _ = _stage(mini_repo, tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert target.read_text() == "accounts: [real]\n", "an operator's file was overwritten"
    assert "[KEEP] config/mail_accounts.yaml" in proc.stdout


def test_stager_points_at_generate_secrets_until_they_exist(mini_repo, tmp_path):
    proc, _ = _stage(mini_repo, tmp_path)
    assert "bin/generate-secrets.sh" in proc.stdout

    (mini_repo / "secrets" / "postgres_password").write_text("x")
    proc, _ = _stage(mini_repo, tmp_path)
    assert "bin/generate-secrets.sh" not in proc.stdout


def test_stager_rejects_unknown_args(mini_repo):
    proc = subprocess.run(
        ["bash", str(mini_repo / "bin" / "stage-backend-build.sh"), "--bogus"],
        capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
    )
    assert proc.returncode == 2
    assert "unknown arg" in proc.stderr


def test_deploy_dry_run_stages_through_the_shared_helper(tmp_path):
    """deploy-production.sh must emit the helper's two commands, aimed at the
    build host's staging context -- no second copy of the rsync lines."""
    log = tmp_path / "unused.log"
    stub = _stub_dir(tmp_path, ("ssh", "kubectl", "rsync"))
    env = _env(
        stub, log,
        RENFIELD_REGISTRY="stub.example/renfield",
        RENFIELD_MANIFESTS_DIR=str(tmp_path / "no-manifests"),
        RENFIELD_BUILD_HOST="stub@build",
    )
    proc = subprocess.run(
        ["bash", str(_DEPLOY), "--backend-tag", "t1359", "--skip-frontend", "--dry-run"],
        env=env, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL,
    )
    assert proc.returncode == 0, proc.stderr
    expected = _helper_lines(str(_REPO), "stub@build:/tmp/renfield-build-t1359/src/backend")
    for line in expected:
        assert f"[dry-run] {line}" in proc.stdout, proc.stdout
    assert "rsync -az $REPO_ROOT/data/wakeword-models" not in _DEPLOY.read_text(), (
        "deploy-production.sh carries its own copy of the staging rsync again"
    )


def _deploy_copy(tmp_path: Path, *, plain_stdin_loop: bool) -> Path:
    """deploy-production.sh + helper in a scratch repo. With
    ``plain_stdin_loop`` the FD-3 loop is rewritten to read stdin -- the
    regression the real-run test must catch."""
    repo = tmp_path / ("mutant" if plain_stdin_loop else "repo")
    (repo / "bin" / "lib").mkdir(parents=True)
    shutil.copy2(_HELPER, repo / "bin" / "lib" / "backend-build-extras.sh")
    text = _DEPLOY.read_text()
    if plain_stdin_loop:
        assert "read -r -u 3 _extras_cmd" in text and "done 3< <(backend_extras_rsync_cmds" in text
        text = text.replace("read -r -u 3 _extras_cmd", "read -r _extras_cmd")
        text = text.replace("done 3< <(backend_extras_rsync_cmds", "done < <(backend_extras_rsync_cmds")
    (repo / "bin" / "deploy-production.sh").write_text(text)
    return repo


def _real_deploy_extras_calls(tmp_path: Path, repo: Path) -> list[list[str]]:
    """Runs the deploy for real (no --dry-run) against stubs that drain stdin
    and returns the rsync calls aimed at the two staged directories. Only the
    rsync stage matters here; what the script does after it (it lacks the rest
    of the repo in this copy) is not asserted."""
    log = tmp_path / f"{repo.name}-rsync.log"
    log.write_text("")
    stub = _stub_dir(tmp_path, ("ssh", "kubectl", "rsync"))
    env = _env(
        stub, log,
        RENFIELD_REGISTRY="stub.example/renfield",
        RENFIELD_MANIFESTS_DIR=str(tmp_path / "no-manifests"),
        RENFIELD_BUILD_HOST="stub@build",
    )
    subprocess.run(
        ["bash", str(repo / "bin" / "deploy-production.sh"), "--backend-tag", "t1359",
         "--skip-frontend", "--skip-migration-gate"],
        env=env, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL,
    )
    calls = [shlex.split(line) for line in log.read_text().splitlines()]
    return [c for c in calls if c and c[-1].endswith(("/wakeword-models/", "/satellite/"))]


def test_real_deploy_runs_both_staging_copies(tmp_path):
    repo = _deploy_copy(tmp_path, plain_stdin_loop=False)
    calls = _real_deploy_extras_calls(tmp_path, repo)
    expected = _helper_lines(str(repo), "stub@build:/tmp/renfield-build-t1359/src/backend")
    assert calls == [shlex.split(line)[1:] for line in expected]


def test_real_deploy_test_bites_on_a_plain_stdin_loop(tmp_path):
    """Negative control: with the loop reading stdin, the stdin-draining stub
    swallows the second command -- the test above would go red."""
    repo = _deploy_copy(tmp_path, plain_stdin_loop=True)
    calls = _real_deploy_extras_calls(tmp_path, repo)
    assert len(calls) == 1, calls


_START = _REPO / "bin" / "start.sh"
_UPDATE = _REPO / "bin" / "update.sh"

# docker / docker-compose / curl / sleep stubs: record the call into the SAME
# log as rsync so the order (stage BEFORE compose) is visible.
_ORDER_STUB = r"""#!/usr/bin/env bash
cat >/dev/null
printf '%s %s\n' "$(basename "$0")" "$*" >> "$STUB_LOG"
exit 0
"""


@pytest.mark.parametrize(
    ("script", "compose_call"),
    [(_START, "docker-compose up -d"), (_UPDATE, "docker-compose build")],
)
def test_start_and_update_stage_before_compose(mini_repo, tmp_path, script, compose_call):
    """``bin/start.sh`` and ``bin/update.sh`` are the documented entry points;
    both must stage the build context before compose builds the backend."""
    shutil.copy2(script, mini_repo / "bin" / script.name)
    (mini_repo / ".env").write_text("POSTGRES_PASSWORD=x\nOLLAMA_MODEL=m\n")
    log = tmp_path / "order.log"
    log.write_text("")
    stub = _stub_dir(tmp_path, ("rsync",))
    for name in ("docker", "docker-compose", "curl", "sleep"):
        path = stub / name
        path.write_text(_ORDER_STUB)
        path.chmod(0o755)
    proc = subprocess.run(
        ["bash", str(mini_repo / "bin" / script.name)],
        env=_env(stub, log), capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    lines = log.read_text().splitlines()
    staged = [i for i, line in enumerate(lines) if line.rstrip().endswith(("/wakeword-models/", "/satellite/"))]
    compose = [i for i, line in enumerate(lines) if line.startswith(compose_call)]
    assert len(staged) == 2, lines
    assert compose, lines
    assert max(staged) < compose[0], f"compose ran before the staging step: {lines}"
