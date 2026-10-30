"""``bin/deploy-production.sh`` -- the pre-flight gate that puts the Alembic
head IN THE BUILT IMAGE against the running database (#1392).

On 2026-10-11 a deploy rolled out without ``--migrate``, because the PR
contained no migration; the image, however, was built from ``main``, where
``pc20260929_kg_validity`` sat. The ORM selects every column unconditionally ->
~35 min of production damage, and ``/health/ready`` reported ``healthy``
throughout.

What is checked here is not THAT there is a gate but that it BITES: every case
runs the real script without ``--dry-run`` against stubs for
``ssh``/``kubectl``/``rsync`` and checks TWO things -- the return code AND that
``kubectl set image`` never happened. A gate whose biting has not been shown is
not a gate.

For this the isolated copy on the build box needs ``bin/`` AND ``k8s/`` (the
``--migrate`` step reads ``k8s/alembic-upgrade-job.yaml``).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit]

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "bin" / "deploy-production.sh"
# The --migrate branch reads this manifest. If it is missing from the
# isolated copy, the script fails with code 2 and the test would otherwise
# claim the gate had refused to let it through -- a finding about the wrong
# object. Here it aborts with the true cause instead.
_MIGRATION_JOB = _REPO / "k8s" / "alembic-upgrade-job.yaml"

# The stub for the build box. Answers the gate's two queries and swallows
# everything else (docker build/push, rsync preparation, cleanup).
_SSH_STUB = r"""#!/bin/bash
args="$*"
case "$args" in
  *"alembic heads"*)
    if [[ "${STUB_IMG_FAIL:-0}" == 1 ]]; then
      echo "docker: error during connect"
      exit 125
    fi
    printf '%s\n' "${STUB_IMG_HEADS}"; exit 0 ;;
  *"image inspect"*) exit "${STUB_IMG_PRESENT_EXIT:-0}" ;;
  # Die Richtungs-Sonde fragt BEIDE Revisionen in EINEM Aufruf und gibt die
  # Rueckgabewerte als Text zurueck. `STUB_PROBE_SILENT=1` spielt den Fall, in
  # dem ssh/docker gar nicht antworten — genau der Fail-open-Pfad aus dem
  # Review vom 2026-10-11.
  *"alembic show"*)
    if [[ "${STUB_PROBE_SILENT:-0}" == 1 ]]; then
      echo "ssh: connect to host ... port 22: Operation timed out" >&2
      exit 255
    fi
    echo "head=${STUB_SHOW_HEAD_EXIT:-0}"
    echo "db=${STUB_SHOW_EXIT:-0}"
    exit 0 ;;
esac
exit 0
"""

# The kubectl stub logs EVERY call -- the proof that the abort happens
# BEFORE the first `set image` hangs on that.
_KUBECTL_STUB = r"""#!/bin/bash
printf '%s\n' "$*" >> "$STUB_LOG"
args="$*"
case "$args" in
  *"alembic current"*)
    if [[ "${STUB_DB_FAIL:-0}" == 1 ]]; then
      echo 'Error from server (NotFound): deployments.apps "backend" not found'
      exit 1
    fi
    printf '%s\n' "${STUB_DB_OUT}"; exit 0 ;;
  *"get cm renfield-env"*)
    # Der Plugin-Riegel vor dem Push liest PLUGIN_MODULES; eine leere Antwort
    # laesst seine Pipeline unter `pipefail` scheitern. Eigenheit der Attrappe.
    printf 'stubpkg'; exit 0 ;;
  *version*) echo '{}'; exit 0 ;;
  # 🛑 MUST drain stdin. The script pipes `sed <manifest> | kubectl apply -f -`
  # under `set -o pipefail`; a reader that exits without reading leaves `sed`
  # writing into a closed pipe, so `sed` dies of SIGPIPE and the pipeline --
  # and with it the script -- returns 141. Measured on the build box with the
  # real 3041-byte manifest: 614 of 3000 runs, a 20% race that made
  # `..._with_migrate_proceeds` flaky in the full suite and green in isolation.
  # Real `kubectl apply -f -` always reads stdin; the stub has to as well, or
  # it invents a failure mode the tool does not have.
  *"apply -f -"*) cat >/dev/null; exit 0 ;;
esac
exit 0
"""


@pytest.fixture
def deploy(tmp_path):
    """Returns a caller that runs the real script against the stubs."""
    if shutil.which("bash") is None:  # pragma: no cover - environment guard
        pytest.skip("bash nicht vorhanden")
    assert _SCRIPT.is_file(), f"unvollstaendige Kopie: {_SCRIPT} fehlt (bin/ mitrsyncen)"
    assert _MIGRATION_JOB.is_file(), (
        f"unvollstaendige Kopie: {_MIGRATION_JOB} fehlt (k8s/ mitrsyncen) — "
        "ohne das Manifest scheitert der --migrate-Zweig aus einem anderen Grund"
    )

    stub_dir = tmp_path / "stub"
    stub_dir.mkdir()
    for name, body in (("ssh", _SSH_STUB), ("kubectl", _KUBECTL_STUB), ("rsync", "#!/bin/bash\nexit 0\n")):
        path = stub_dir / name
        path.write_text(body)
        path.chmod(0o755)
    log = tmp_path / "kubectl.log"
    log.write_text("")

    def _run(*args, backend_tag="stub-tag", **stub_env):
        env = dict(os.environ)
        env["PATH"] = f"{stub_dir}{os.pathsep}{env.get('PATH', '')}"
        env["RENFIELD_REGISTRY"] = "stub.example/renfield"
        # Keeps the drift step from touching real manifests.
        env["RENFIELD_MANIFESTS_DIR"] = str(tmp_path / "no-manifests")
        env["STUB_LOG"] = str(log)
        env.update({k: str(v) for k, v in stub_env.items()})
        if backend_tag is None:  # frontend-only: there is no new backend image
            flags = ["--frontend-tag", "v0.0.0-stub", "--skip-backend"]
        else:
            flags = ["--backend-tag", backend_tag, "--skip-frontend"]
        proc = subprocess.run(
            ["bash", str(_SCRIPT), *flags, *args],
            capture_output=True, text=True, env=env, timeout=120,
        )
        rolled_out = "set image" in log.read_text()
        return proc, rolled_out

    return _run


def test_image_ahead_of_db_without_migrate_aborts_before_rollout(deploy):
    """The damage case of 2026-10-11: image AHEAD of the DB, no --migrate."""
    proc, rolled_out = deploy(
        STUB_IMG_HEADS="pc20260929_kg_validity (head)",
        STUB_DB_OUT="INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.\npc20260927_voice2fa",
        STUB_SHOW_EXIT=0,
    )
    assert proc.returncode == 1
    assert not rolled_out, "das Tor muss VOR dem ersten `set image` abbrechen"
    # Both revisions in plain text, or the message is no use.
    assert "pc20260929_kg_validity" in proc.stderr
    assert "pc20260927_voice2fa" in proc.stderr
    assert "--migrate" in proc.stderr


def test_image_ahead_of_db_with_migrate_proceeds(deploy):
    """With --migrate the decision has been made -- the rollout may run."""
    proc, rolled_out = deploy(
        "--migrate",
        STUB_IMG_HEADS="pc20260929_kg_validity (head)",
        STUB_DB_OUT="pc20260927_voice2fa",
        STUB_SHOW_EXIT=0,
    )
    assert proc.returncode == 0
    assert rolled_out


def test_multiple_heads_in_image_abort_even_with_migrate(deploy):
    """A forked chain: an abort on the rollout side as well."""
    proc, rolled_out = deploy(
        "--migrate",
        STUB_IMG_HEADS="aaa111_erste (head)\nbbb222_zweite (head)",
        STUB_DB_OUT="aaa111_erste",
    )
    assert proc.returncode == 1
    assert not rolled_out
    assert "aaa111_erste" in proc.stderr and "bbb222_zweite" in proc.stderr


def test_unreadable_image_head_aborts(deploy):
    """A gate that lets through on uncertainty is exactly the #1392 defect."""
    proc, rolled_out = deploy(STUB_IMG_FAIL=1, STUB_DB_OUT="pc20260929_kg_validity")
    assert proc.returncode == 1
    assert not rolled_out


def test_unreadable_db_revision_aborts(deploy):
    proc, rolled_out = deploy(STUB_IMG_HEADS="pc20260929_kg_validity (head)", STUB_DB_FAIL=1)
    assert proc.returncode == 1
    assert not rolled_out


def test_empty_alembic_version_without_migrate_aborts(deploy):
    """Empty `alembic_version`: without --migrate the situation is unclear."""
    proc, rolled_out = deploy(
        STUB_IMG_HEADS="pc20260929_kg_validity (head)",
        STUB_DB_OUT="INFO  [alembic.runtime.migration] Context impl PostgresqlImpl.",
    )
    assert proc.returncode == 1
    assert not rolled_out


def test_explicit_bypass_proceeds(deploy):
    """The emergency exit exists, but it has to be typed."""
    proc, rolled_out = deploy(
        "--skip-migration-gate",
        STUB_IMG_HEADS="pc20260929_kg_validity (head)",
        STUB_DB_OUT="pc20260927_voice2fa",
        STUB_SHOW_EXIT=0,
    )
    assert proc.returncode == 0
    assert rolled_out
    assert "NICHT gegen" in proc.stderr  # the waiver is logged


def test_db_ahead_of_image_warns_but_proceeds(deploy):
    """A rollback to an older image: allowed, but not passed over in silence."""
    proc, rolled_out = deploy(
        STUB_IMG_HEADS="pc20260927_voice2fa (head)",
        STUB_DB_OUT="pc20260929_kg_validity",
        STUB_SHOW_EXIT=1,  # the image does NOT know the DB revision
    )
    assert proc.returncode == 0
    assert rolled_out
    assert "RUECKFALL" in proc.stderr


def test_heads_equal_proceeds_with_one_log_line(deploy):
    proc, rolled_out = deploy(
        STUB_IMG_HEADS="pc20260929_kg_validity (head)",
        STUB_DB_OUT="pc20260929_kg_validity (head)",
    )
    assert proc.returncode == 0
    assert rolled_out
    assert "keine Migration ausstehend" in proc.stdout


def test_skip_backend_skips_the_gate(deploy):
    """Without a new backend image there is no head to compare."""
    proc, _ = deploy(backend_tag=None)
    assert proc.returncode == 0
    assert "Vorflug-Tor" not in proc.stdout


def test_dry_run_shows_the_verdict_but_never_aborts(deploy):
    """The dry run is the only way to rehearse the gate beforehand -- so it
    has to EXECUTE it and must still not fail."""
    proc, rolled_out = deploy(
        "--dry-run",
        STUB_IMG_HEADS="pc20260929_kg_validity (head)",
        STUB_DB_OUT="pc20260927_voice2fa",
        STUB_SHOW_EXIT=0,
    )
    assert proc.returncode == 0
    assert not rolled_out, "ein Probelauf rollt nichts aus"
    assert "TOR WUERDE ABBRECHEN" in proc.stdout
    assert "pc20260927_voice2fa" in proc.stdout


# ---------------------------------------------------------------------------
# The direction probe must not open when it fails ITSELF
# (review finding of 2026-10-11)
# ---------------------------------------------------------------------------
#
# 🛑 The only fail-open path the gate had -- and it sat in the dangerous
# branch. The first version asked only `alembic show <db-rev>` and read EVERY
# return code != 0 as "revision unknown" -> rollback -> WARNING -> rollout.
# Measured: a database behind plus a probe exiting 255 gave rc=0 and four
# `kubectl set image` -- exactly the outage this gate is built against, only
# with a warning line in front of it.
#
# The old test wrote the gap down as INTENT: `STUB_SHOW_EXIT=1` with rc=0 plus
# a rollout did not separate "unknown revision" from "probe broken".


def test_a_silent_probe_aborts(deploy):
    """ssh/docker do not answer -> the direction is UNKNOWN, not a
    "rollback". In doubt, nothing is rolled out."""
    proc, rolled_out = deploy(
        STUB_IMG_HEADS="neu_kopf (head)",
        STUB_DB_OUT="alt_rev",
        STUB_PROBE_SILENT=1,
    )
    assert proc.returncode == 1, "eine stumme Sonde darf nicht durchlassen"
    assert not rolled_out, "der Abbruch muss VOR dem ersten `set image` liegen"
    assert "UNBEKANNT" in proc.stderr


def test_a_probe_that_fails_on_its_own_head_aborts(deploy):
    """The image MUST know its own head. If it does not, the probe is
    broken, and no conclusion can be drawn from its failure on the DB
    revision."""
    proc, rolled_out = deploy(
        STUB_IMG_HEADS="neu_kopf (head)",
        STUB_DB_OUT="alt_rev",
        STUB_SHOW_HEAD_EXIT=1,
        STUB_SHOW_EXIT=1,
    )
    assert proc.returncode == 1, "eine kaputte Sonde darf nicht als Rueckfall gelten"
    assert not rolled_out
    assert "KAPUTT" in proc.stderr


def test_a_genuine_rollback_still_proceeds(deploy):
    """🛑 A negative control, and the more important half: a GENUINE rollback
    -- the image knows its own head but not the DB revision -- has to keep
    going through. Otherwise the gate blocks every rollback to an older image,
    gets bypassed, and has no effect."""
    proc, rolled_out = deploy(
        STUB_IMG_HEADS="alt_kopf (head)",
        STUB_DB_OUT="neu_rev",
        STUB_SHOW_HEAD_EXIT=0,
        STUB_SHOW_EXIT=1,
    )
    assert proc.returncode == 0, "ein echter Rueckfall muss durchlassen"
    assert rolled_out, "und ausrollen"
    assert "RUECKFALL" in proc.stderr


def test_a_missing_image_in_dry_run_is_not_an_abort(deploy):
    """In --dry-run a tag that has not been built yet is the NORMAL case
    (build and push are only printed there). The gate reported an abort for it
    that would never happen in a real run -- and `docker run` would have pulled
    4.08 GB from the registry for it."""
    proc, _ = deploy("--dry-run", STUB_IMG_PRESENT_EXIT=1)
    assert proc.returncode == 0
    assert "TOR WUERDE ABBRECHEN" not in proc.stdout, (
        f"kein falscher Abbruch im Probelauf, war: {proc.stdout}"
    )
    assert "NICHT probebar" in proc.stdout


def test_a_missing_image_in_a_real_run_aborts(deploy):
    """In a real run step 2 has just built the image. If it is missing then,
    the build or the push went wrong -- and then nothing is rolled out."""
    proc, rolled_out = deploy(STUB_IMG_PRESENT_EXIT=1)
    assert proc.returncode == 1
    assert not rolled_out
