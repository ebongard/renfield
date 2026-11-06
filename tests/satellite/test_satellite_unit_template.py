"""
Provisioning template renfield-satellite.service.j2 — the memory cap per platform.

The unit carried a fixed MemoryMax=450M sized for a Pi Zero 2 W (512 MB). The
Orange Pis (4 and 12 GB) inherited it with the move to the playbook: their page
cache pressed against the cap and an OTA's pip run stalled in reclaim. The
group_vars value is itself a Jinja expression, so it is rendered first, as
Ansible resolves variables before the template.
"""

from pathlib import Path

import jinja2
import pytest
import yaml

PROVISIONING = Path(__file__).resolve().parents[2] / "src" / "satellite" / "provisioning"


def _memory_max(**host_vars) -> str:
    context = yaml.safe_load((PROVISIONING / "group_vars" / "satellites.yml").read_text())
    context.update(satellite_base_dir="/opt/renfield-satellite", ansible_user="pi", **host_vars)
    for key in ("satellite_hw_groups", "satellite_memory_max", "satellite_venv"):
        rendered = jinja2.Template(context[key]).render(**context)
        context[key] = yaml.safe_load(rendered) if rendered.startswith("[") else rendered
    unit = jinja2.Template(
        (PROVISIONING / "templates" / "renfield-satellite.service.j2").read_text(), trim_blocks=True
    ).render(**context)
    [line] = [ln for ln in unit.splitlines() if ln.startswith("MemoryMax=")]
    return line.removeprefix("MemoryMax=")


@pytest.mark.satellite
def test_a_pi_zero_keeps_the_450m_cap():
    assert _memory_max() == "450M"


@pytest.mark.satellite
def test_an_orange_pi_gets_more_room():
    assert _memory_max(satellite_platform="orangepi") == "1G"


@pytest.mark.satellite
def test_a_host_can_set_its_own_cap():
    assert _memory_max(satellite_platform="orangepi", satellite_memory_max="2G") == "2G"
