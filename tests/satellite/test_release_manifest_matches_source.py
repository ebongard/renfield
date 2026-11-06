"""The committed OTA release manifest must match the satellite source.

`RELEASE_MANIFEST.json` (+ `.sig`) is signed offline over the hashes of the
satellite package files and baked into the backend image; a satellite with
pinned release keys refuses an update whose files do not match it (H6,
fail-closed). Any edit to a listed file, a comment included, therefore breaks
the next OTA until the release is re-signed — and nothing else turns red.
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "bin" / "sign_satellite_release.py"
SOURCE = REPO / "src" / "satellite"


def _verify(source: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--verify", "--source", str(source)],
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.satellite
def test_the_committed_manifest_matches_the_source():
    res = _verify(SOURCE)
    assert res.returncode == 0, (
        "the satellite source no longer matches RELEASE_MANIFEST.json — re-sign the "
        "release (bin/sign_satellite_release.py --sign) and bump the version:\n"
        + res.stdout
        + res.stderr
    )


@pytest.mark.satellite
def test_a_comment_edit_breaks_the_match(tmp_path):
    """Negative control: the check sees a change that touches only a comment."""
    copy = tmp_path / "satellite"
    shutil.copytree(SOURCE, copy, ignore=shutil.ignore_patterns("__pycache__", "provisioning", "tests"))
    target = copy / "renfield_satellite" / "satellite.py"
    target.write_text(target.read_text() + "\n# an edited comment\n")

    assert _verify(copy).returncode != 0
