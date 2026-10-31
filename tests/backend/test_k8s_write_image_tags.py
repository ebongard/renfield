"""``bin/k8s-write-image-tags.sh`` -- writes the rolled-out image tags back
into the manifests after a deploy.

The script touches other people's files in TWO repos (public plus private),
which is why what it must NOT touch is written down here:

* ``:latest`` stays ``:latest``. A moving pointer is not a pinned state -- the
  public manifests carry it deliberately. On its first real run the script
  rewrote seven of them to that day's tag; a cluster set up fresh from those
  would have pulled that day's image forever.
* The registry name stays, placeholder as well as real address. The real
  address does not belong in the public repo, the placeholder not in the
  private one.
* Other images (``voice-server``, ``kokoro-tts``) are none of the script's
  business -- they have their own version series.

The test drives the script as a subprocess: it is shell, and the sed
expressions are exactly where it went wrong.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.backend]

_SCRIPT = Path(__file__).resolve().parents[2] / "bin" / "k8s-write-image-tags.sh"

# Both registry spellings in one file: the placeholder (public repo) and the
# real address (private repo). Neither may change.
_MANIFEST = """\
apiVersion: apps/v1
kind: Deployment
spec:
  template:
    spec:
      containers:
        - name: backend
          image: your-registry.example/renfield/backend:latest
        - name: frontend
          image: your-registry.example/renfield/frontend:latest
        - name: backend-privat
          image: registry.example.invalid/renfield/backend:2026-09-01-alt
        - name: frontend-privat
          image: registry.example.invalid/renfield/frontend:v2.15.29
        - name: voice-server
          image: your-registry.example/renfield/voice-server:v0.1.7
"""


def _run(directory: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(_SCRIPT), "--dir", str(directory), *args],
        capture_output=True,
        text=True,
    )


@pytest.fixture
def manifest(tmp_path: Path) -> Path:
    path = tmp_path / "backend.yaml"
    path.write_text(_MANIFEST)
    return path


def test_latest_stays_put(manifest: Path) -> None:
    """The actual finding: `latest` survives the run -- for BOTH images."""
    result = _run(manifest.parent, "--backend-tag", "2026-09-29-neu", "--frontend-tag", "2026-09-29-neu")
    assert result.returncode == 0, result.stderr

    content = manifest.read_text()
    assert "renfield/backend:latest" in content
    assert "renfield/frontend:latest" in content


def test_concrete_tags_are_set(manifest: Path) -> None:
    """Counter-check for the guard: without it `test_latest_stays_put`
    would be green even if the script replaced nothing at all any more."""
    _run(manifest.parent, "--backend-tag", "2026-09-29-neu", "--frontend-tag", "v2.16.0")

    content = manifest.read_text()
    assert "renfield/backend:2026-09-29-neu" in content
    assert "renfield/frontend:v2.16.0" in content
    assert "2026-09-01-alt" not in content
    assert "v2.15.29" not in content


def test_the_registry_name_stays_untouched(manifest: Path) -> None:
    """The prefix before `renfield/` is never touched -- otherwise the real
    address migrates into the public repo or the placeholder into the private
    one."""
    _run(manifest.parent, "--backend-tag", "2026-09-29-neu", "--frontend-tag", "v2.16.0")

    content = manifest.read_text()
    assert content.count("your-registry.example/renfield/") == 3
    assert content.count("registry.example.invalid/renfield/") == 2


def test_other_images_stay_untouched(manifest: Path) -> None:
    """`voice-server` has its own version series (v0.1.x)."""
    _run(manifest.parent, "--backend-tag", "2026-09-29-neu", "--frontend-tag", "v2.16.0")

    assert "renfield/voice-server:v0.1.7" in manifest.read_text()


def test_a_latest_prefix_is_a_real_tag(tmp_path: Path) -> None:
    """The boundary sits tight: what is protected is the tag `latest`, not
    everything that starts with `latest`. `latest-rc` is a pinned state and is
    replaced like any other tag."""
    path = tmp_path / "rc.yaml"
    path.write_text("          image: your-registry.example/renfield/backend:latest-rc\n")

    _run(tmp_path, "--backend-tag", "2026-09-29-neu")

    assert path.read_text().strip().endswith("renfield/backend:2026-09-29-neu")


def test_a_pure_latest_manifest_reports_nothing_to_do(tmp_path: Path) -> None:
    """A manifest that carries only `latest` is unchanged after the guard --
    so the script must not report it as written either."""
    path = tmp_path / "nur-latest.yaml"
    original = "          image: your-registry.example/renfield/backend:latest\n"
    path.write_text(original)

    result = _run(tmp_path, "--backend-tag", "2026-09-29-neu")

    assert result.returncode == 0, result.stderr
    assert "nothing to do" in result.stdout
    assert path.read_text() == original


def test_dry_run_does_not_write(manifest: Path) -> None:
    result = _run(manifest.parent, "--backend-tag", "2026-09-29-neu", "--dry-run")

    assert result.returncode == 0, result.stderr
    assert "dry-run" in result.stdout
    assert manifest.read_text() == _MANIFEST
