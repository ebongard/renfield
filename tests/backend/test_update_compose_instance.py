"""``bin/update-compose-instance.sh`` -- the update of a single-host compose
instance, and above all its migration gate.

Same principle as ``test_deploy_migration_gate.py``: not THAT there is a gate,
but that it BITES. Every case runs the real script against stubs for ``ssh``
and ``docker`` and checks the return code AND whether ``compose up`` happened.

The ssh stub runs the target's command string in a local bash, so the remote
program, its quoting and the stdin of the ``load`` step are the real ones. Two
defects found on the real instance (2026-11-08) are pinned here:
- the remote program went over stdin and replaced the image stream, so
  ``docker load`` got nothing;
- ``alembic current`` prints a database BEHIND the head without "(head)", and
  the parser required the suffix -- the gate would have refused exactly the
  case ``--migrate`` exists for.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit]

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "bin" / "update-compose-instance.sh"
_REG = "reg.example/renfield"
OLD_B, OLD_F = f"{_REG}/backend:old", f"{_REG}/frontend:old"
NEW_B, NEW_F = f"{_REG}/backend:new", f"{_REG}/frontend:new"
HEAD = "pc_new_head"

_SSH_STUB = r"""#!/bin/bash
host="$1"; shift
if [[ "$host" == build ]]; then
  case "$*" in
    *"image inspect"*) exit 0 ;;
    *"docker save"*) printf 'image-bytes' | gzip; exit 0 ;;
  esac
  exit 0
fi
# The target: the command string is what a login shell would evaluate.
exec bash -c "$*"
"""

_DOCKER_STUB = r"""#!/bin/bash
printf '%s\n' "$*" >> "$STUB_LOG"
args="$*"
case "$args" in
  info) exit 0 ;;
  "image inspect "*)
    for i in $STUB_TARGET_IMAGES; do [[ "$i" == "$3" ]] && exit 0; done; exit 1 ;;
  load)
    n=$(wc -c | tr -d ' '); echo "load-bytes=$n" >> "$STUB_LOG"
    [[ "$n" -gt 0 ]] || { echo "unrecognized image format" >&2; exit 1; }
    echo "Loaded image"; exit 0 ;;
  *"exec -T postgres sh -c"*) printf 'PGDMP'; exit 0 ;;
  *"exec -T postgres pg_restore -l"*) cat >/dev/null; echo "1; 0 0 TABLE DATA public t"; exit 0 ;;
  *"alembic heads"*)
    [[ "${STUB_HEADS_FAIL:-0}" == 1 ]] && { echo "error" >&2; exit 1; }
    echo "$STUB_IMG_HEAD (head)"; exit 0 ;;
  *"alembic current"*)
    rev=$(cat "$STUB_DB")
    # Real alembic: "(head)" only when the database IS at the head.
    if [[ "$rev" == "$STUB_IMG_HEAD" ]]; then echo "$rev (head)"; else echo "$rev"; fi
    exit 0 ;;
  *"alembic upgrade head"*) echo "$STUB_IMG_HEAD" > "$STUB_DB"; exit 0 ;;
  *"up -d"*) exit 0 ;;
  *"ps -q backend"*) echo cid; exit 0 ;;
  "inspect -f "*) echo "${STUB_HEALTH:-healthy}"; exit 0 ;;
  *"exec -T backend curl"*) printf 200; exit 0 ;;
  *"ps --format"*) echo "backend $NEW_B_IMG"; exit 0 ;;
  *logs*) exit 0 ;;
  "images --format"*) printf '%s\n' $STUB_TARGET_IMAGES; exit 0 ;;
  "ps -a --format"*) exit 0 ;;
  rmi*) exit 0 ;;
esac
exit 0
"""


@pytest.fixture
def instance(tmp_path):
    if shutil.which("bash") is None or shutil.which("gzip") is None:  # pragma: no cover
        pytest.skip("bash/gzip not available")
    assert _SCRIPT.is_file(), f"incomplete copy: {_SCRIPT} is missing (rsync bin/ too)"
    stub = tmp_path / "stub"
    stub.mkdir()
    # `sleep` is a no-op: the health wait polls 60 x 5 s on a real instance.
    for name, body in (("ssh", _SSH_STUB), ("docker", _DOCKER_STUB), ("sleep", "#!/bin/sh\nexit 0\n")):
        (stub / name).write_text(body)
        (stub / name).chmod(0o755)
    home = tmp_path / "instance"
    home.mkdir()
    (home / "compose.yml").write_text("services: {}\n")
    (home / ".env").write_text(f"A=1\nRENFIELD_BACKEND_IMAGE={OLD_B}\nRENFIELD_FRONTEND_IMAGE={OLD_F}\nZ=2\n")
    db = tmp_path / "db"
    log = tmp_path / "docker.log"
    log.write_text("")

    def run(*args, db_rev=HEAD, target_images=(NEW_B, NEW_F, OLD_B, OLD_F), **env):
        db.write_text(db_rev)
        full_env = {
            **os.environ,
            "PATH": f"{stub}:{os.environ['PATH']}",
            "RENFIELD_REGISTRY": _REG,
            "RENFIELD_BUILD_HOST": "build",
            "STUB_LOG": str(log),
            "STUB_DB": str(db),
            "STUB_IMG_HEAD": HEAD,
            "STUB_TARGET_IMAGES": " ".join(target_images),
            "NEW_B_IMG": NEW_B,
            **env,
        }
        base = ["--target", "target", "--dir", str(home), "--compose-file", "compose.yml"]
        if "--rollback" not in args:
            base += ["--backend-tag", "new", "--frontend-tag", "new"]
        proc = subprocess.run(
            ["bash", str(_SCRIPT), *base, *args],
            env=full_env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return proc, log.read_text(), (home / ".env").read_text(), db.read_text().strip()

    run.home = home
    return run


def _ups(log: str) -> int:
    return sum(1 for line in log.splitlines() if "up -d" in line)


def test_a_database_behind_without_migrate_is_refused_and_env_restored(instance):
    proc, log, env, _ = instance(db_rev="pc_old")
    assert proc.returncode != 0
    assert "--migrate is not set" in proc.stderr
    assert _ups(log) == 0
    assert f"RENFIELD_BACKEND_IMAGE={OLD_B}" in env and f"RENFIELD_FRONTEND_IMAGE={OLD_F}" in env


def test_a_database_behind_with_migrate_is_upgraded_before_the_restart(instance):
    proc, log, env, db = instance("--migrate", db_rev="pc_old")
    assert proc.returncode == 0, proc.stderr
    lines = log.splitlines()
    upgrade = next(i for i, line in enumerate(lines) if "alembic upgrade head" in line)
    up = next(i for i, line in enumerate(lines) if "up -d" in line)
    assert upgrade < up
    assert db == HEAD
    assert f"RENFIELD_BACKEND_IMAGE={NEW_B}" in env and env.startswith("A=1\n") and "Z=2" in env


def test_an_unreadable_probe_never_passes(instance):
    proc, log, env, _ = instance("--migrate", STUB_HEADS_FAIL="1")
    assert proc.returncode != 0
    assert "unreadable" in proc.stderr
    assert _ups(log) == 0
    assert f"RENFIELD_BACKEND_IMAGE={OLD_B}" in env


def test_a_database_at_the_head_restarts_without_migrating(instance):
    proc, log, env, _ = instance()
    assert proc.returncode == 0, proc.stderr
    assert "alembic upgrade head" not in log
    assert _ups(log) == 1
    assert f"RENFIELD_FRONTEND_IMAGE={NEW_F}" in env


def test_a_backup_comes_first_and_is_named_by_one_timestamp(instance):
    proc, _, _, _ = instance()
    assert proc.returncode == 0, proc.stderr
    backups = sorted(p.name for p in (instance.home / "backups").iterdir())
    assert len(backups) == 2
    ts = backups[0].removeprefix("db-").removesuffix(".dump")
    assert backups == [f"db-{ts}.dump", f"env.{ts}"]


def test_a_missing_image_reaches_docker_load_through_stdin(instance):
    proc, log, _, _ = instance(target_images=(NEW_B, OLD_B, OLD_F))
    assert proc.returncode == 0, proc.stderr
    loads = [line for line in log.splitlines() if line.startswith("load-bytes=")]
    assert loads and int(loads[0].split("=")[1]) > 0


def test_unhealthy_containers_fail_and_name_the_rollback(instance):
    proc, _, _, _ = instance(STUB_HEALTH="unhealthy")
    assert proc.returncode != 0
    assert "--rollback" in proc.stderr


def test_rollback_restores_the_env_and_skips_the_gate(instance):
    proc, _, _, _ = instance()
    ts = next(p.name for p in (instance.home / "backups").iterdir() if p.name.startswith("env."))[4:]
    proc, log, env, _ = instance("--rollback", ts)
    assert proc.returncode == 0, proc.stderr
    assert f"RENFIELD_BACKEND_IMAGE={OLD_B}" in env
    assert "alembic" not in log.split("info\n")[-1]


def test_prune_keeps_the_rollback_target_on_a_rerun_with_unchanged_tags(instance):
    # The state after an earlier old -> new update: the .env already names the
    # new images, only the backup history still knows the old ones. (Seeded
    # rather than produced by a first run: two runs in one second would share
    # one backup timestamp.)
    (instance.home / ".env").write_text(f"RENFIELD_BACKEND_IMAGE={NEW_B}\nRENFIELD_FRONTEND_IMAGE={NEW_F}\n")
    (instance.home / "backups").mkdir()
    old_env = instance.home / "backups" / "env.20260101-000000"
    old_env.write_text(f"RENFIELD_BACKEND_IMAGE={OLD_B}\nRENFIELD_FRONTEND_IMAGE={OLD_F}\n")
    os.utime(old_env, (1_700_000_000, 1_700_000_000))
    proc, log, _, _ = instance("--prune-old", target_images=(NEW_B, NEW_F, OLD_B, OLD_F, f"{_REG}/backend:older"))
    assert proc.returncode == 0, proc.stderr
    removed = [img for line in log.splitlines() if line.startswith("rmi ") for img in line.split()[1:]]
    assert removed == [f"{_REG}/backend:older"]
