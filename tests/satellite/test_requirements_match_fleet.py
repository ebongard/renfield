"""
The OTA's requirements.txt names exactly what the fleet runs.

The OTA installs src/satellite/requirements.txt into the satellite's venv, but
the fleet is provisioned from group_vars `satellite_python_packages` plus a few
dedicated pip tasks. The two lists had drifted: requirements.txt carried
soundcard, pymicro-wakeword, pyopen-wakeword and RPi.GPIO, which no satellite
has. While the OTA's bare "pip" missed the venv this stayed invisible; with the
venv pip the next OTA would have installed them — pyopen-wakeword changes which
wake word backend loads.
"""

import re
from pathlib import Path

import pytest
import yaml

SATELLITE = Path(__file__).resolve().parents[2] / "src" / "satellite"
PROVISIONING = SATELLITE / "provisioning"

# Installed by the playbook but deliberately not by an OTA, so not in
# requirements.txt. Each needs a reason; an empty reason fails the test.
PROVISIONING_ONLY = {
    "setuptools": "build tool (pkg_resources for webrtcvad); pip/setuptools are kept off the OTA allowlist",
    "requests": "kept off the OTA allowlist by policy; the satellite code does not import it",
    "scikit-learn": "pulled in for noisereduce at provisioning; not on the OTA allowlist",
}


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _name(spec: str) -> str:
    return _canonical(re.split(r"[<>=!~;\s\[]", spec.strip(), maxsplit=1)[0])


def _requirements() -> set[str]:
    names = set()
    for raw in (SATELLITE / "requirements.txt").read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            names.add(_name(line))
    return names


def _fleet() -> set[str]:
    group_vars = yaml.safe_load((PROVISIONING / "group_vars" / "satellites.yml").read_text())
    names = {_name(p) for p in group_vars["satellite_python_packages"]}
    names.add(_name(group_vars["openwakeword_package"]))
    # The dedicated pip tasks with a literal package name (cryptography, opuslib, ...).
    for play in yaml.safe_load((PROVISIONING / "provision.yml").read_text()):
        for task in play.get("tasks", []):
            pip = task.get("ansible.builtin.pip")
            if pip and "when" not in task and "{{" not in str(pip.get("name")) and pip.get("name") != "pip":
                names.add(_name(pip["name"]))
    return names


def _allowlist() -> set[str]:
    text = (SATELLITE / "renfield_satellite" / "update" / "safe_packages.txt").read_text()
    return {_canonical(line.split("#", 1)[0].strip()) for line in text.splitlines() if line.split("#", 1)[0].strip()}


@pytest.mark.satellite
def test_the_ota_installs_nothing_the_fleet_does_not_run():
    extra = _requirements() - _fleet()
    assert not extra, f"requirements.txt names packages no satellite is provisioned with: {sorted(extra)}"


@pytest.mark.satellite
def test_every_fleet_package_reaches_the_ota_or_is_exempt_with_a_reason():
    missing = _fleet() - _requirements() - set(PROVISIONING_ONLY)
    assert not missing, f"provisioned but not in requirements.txt (add it, or exempt it with a reason): {sorted(missing)}"
    assert all(PROVISIONING_ONLY.values())


@pytest.mark.satellite
def test_every_requirement_passes_the_ota_allowlist():
    """Otherwise the OTA rejects the package and aborts the update."""
    rejected = _requirements() - _allowlist()
    assert not rejected, f"not in safe_packages.txt: {sorted(rejected)}"


@pytest.mark.satellite
def test_the_fleet_list_is_read():
    """Negative control for the parser: the known packages are found."""
    fleet = _fleet()
    assert {"onnxruntime", "cryptography", "opuslib", "openwakeword"} <= fleet
    assert "envirophat" not in fleet  # host-conditional task, not fleet-wide
