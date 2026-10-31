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

_SKRIPT = Path(__file__).resolve().parents[2] / "bin" / "k8s-write-image-tags.sh"

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


def _lauf(verzeichnis: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(_SKRIPT), "--dir", str(verzeichnis), *args],
        capture_output=True,
        text=True,
    )


@pytest.fixture
def manifest(tmp_path: Path) -> Path:
    pfad = tmp_path / "backend.yaml"
    pfad.write_text(_MANIFEST)
    return pfad


def test_latest_bleibt_stehen(manifest: Path) -> None:
    """The actual finding: `latest` survives the run -- for BOTH images."""
    ergebnis = _lauf(manifest.parent, "--backend-tag", "2026-09-29-neu", "--frontend-tag", "2026-09-29-neu")
    assert ergebnis.returncode == 0, ergebnis.stderr

    inhalt = manifest.read_text()
    assert "renfield/backend:latest" in inhalt
    assert "renfield/frontend:latest" in inhalt


def test_konkrete_marken_werden_gesetzt(manifest: Path) -> None:
    """Counter-check for the guard: without it `test_latest_bleibt_stehen`
    would be green even if the script replaced nothing at all any more."""
    _lauf(manifest.parent, "--backend-tag", "2026-09-29-neu", "--frontend-tag", "v2.16.0")

    inhalt = manifest.read_text()
    assert "renfield/backend:2026-09-29-neu" in inhalt
    assert "renfield/frontend:v2.16.0" in inhalt
    assert "2026-09-01-alt" not in inhalt
    assert "v2.15.29" not in inhalt


def test_registry_name_bleibt_unberuehrt(manifest: Path) -> None:
    """The prefix before `renfield/` is never touched -- otherwise the real
    address migrates into the public repo or the placeholder into the private
    one."""
    _lauf(manifest.parent, "--backend-tag", "2026-09-29-neu", "--frontend-tag", "v2.16.0")

    inhalt = manifest.read_text()
    assert inhalt.count("your-registry.example/renfield/") == 3
    assert inhalt.count("registry.example.invalid/renfield/") == 2


def test_fremde_bilder_bleiben_unberuehrt(manifest: Path) -> None:
    """`voice-server` hat seine eigene Versionsreihe (v0.1.x)."""
    _lauf(manifest.parent, "--backend-tag", "2026-09-29-neu", "--frontend-tag", "v2.16.0")

    assert "renfield/voice-server:v0.1.7" in manifest.read_text()


def test_latest_praefix_ist_eine_echte_marke(tmp_path: Path) -> None:
    """The boundary sits tight: what is protected is the tag `latest`, not
    everything that starts with `latest`. `latest-rc` is a pinned state and is
    replaced like any other tag."""
    pfad = tmp_path / "rc.yaml"
    pfad.write_text("          image: your-registry.example/renfield/backend:latest-rc\n")

    _lauf(tmp_path, "--backend-tag", "2026-09-29-neu")

    assert pfad.read_text().strip().endswith("renfield/backend:2026-09-29-neu")


def test_reines_latest_manifest_meldet_nichts_zu_tun(tmp_path: Path) -> None:
    """A manifest that carries only `latest` is unchanged after the guard --
    so the script must not report it as written either."""
    pfad = tmp_path / "nur-latest.yaml"
    original = "          image: your-registry.example/renfield/backend:latest\n"
    pfad.write_text(original)

    ergebnis = _lauf(tmp_path, "--backend-tag", "2026-09-29-neu")

    assert ergebnis.returncode == 0, ergebnis.stderr
    assert "nothing to do" in ergebnis.stdout
    assert pfad.read_text() == original


def test_dry_run_schreibt_nicht(manifest: Path) -> None:
    ergebnis = _lauf(manifest.parent, "--backend-tag", "2026-09-29-neu", "--dry-run")

    assert ergebnis.returncode == 0, ergebnis.stderr
    assert "dry-run" in ergebnis.stdout
    assert manifest.read_text() == _MANIFEST
