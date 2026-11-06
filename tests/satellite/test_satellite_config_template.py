"""
Provisioning template satellite.yaml.j2 — the optional LED floor and Classic-BT RSSI keys.

The Orange Pi satellites moved from k8s pods (ConfigMap) to the Ansible playbook;
two of their settings had no template key. Unset, the rendered file must stay as it
was for the Pi fleet; set, the satellite's own loader must pick the values up.
The template only uses `default` and `lower`, so plain Jinja2 renders it like Ansible.
"""

from pathlib import Path

import jinja2
import pytest
import yaml
from renfield_satellite.config import load_config

PROVISIONING = Path(__file__).resolve().parents[2] / "src" / "satellite" / "provisioning"


def _render(tmp_path, **host_vars) -> Path:
    context = yaml.safe_load((PROVISIONING / "group_vars" / "satellites.yml").read_text())
    context.update(
        satellite_id="sat-test",
        satellite_room="Test",
        hat_type="xvf3800-usb",
        led_type="xvf3800",
        led_num=12,
        **host_vars,
    )
    # Ansible renders templates with trim_blocks=True.
    template = jinja2.Template(
        (PROVISIONING / "templates" / "satellite.yaml.j2").read_text(), trim_blocks=True
    )
    out = tmp_path / "satellite.yaml"
    out.write_text(template.render(**context))
    return out


@pytest.mark.satellite
def test_unset_keys_are_not_rendered(tmp_path):
    rendered = yaml.safe_load(_render(tmp_path).read_text())

    assert "min_brightness" not in rendered["led"]
    assert "classic_rssi" not in rendered["ble"]


@pytest.mark.satellite
def test_set_keys_reach_the_satellite_config(tmp_path):
    path = _render(tmp_path, led_min_brightness=28, ble_classic_rssi=False)

    config = load_config(str(path))

    assert config.led.min_brightness == 28
    assert config.ble.classic_rssi is False


@pytest.mark.satellite
def test_classic_rssi_true_renders_as_yaml_true(tmp_path):
    path = _render(tmp_path, ble_classic_rssi=True)

    assert load_config(str(path)).ble.classic_rssi is True


def _unit_groups(platform: str) -> str:
    """SupplementaryGroups= of the rendered unit, with satellite_hw_groups resolved
    the way Ansible resolves it from group_vars."""
    env = jinja2.Environment(trim_blocks=True)
    raw = yaml.safe_load((PROVISIONING / "group_vars" / "satellites.yml").read_text())
    groups = yaml.safe_load(
        env.from_string(raw["satellite_hw_groups"]).render(satellite_platform=platform)
    )
    unit = env.from_string(
        (PROVISIONING / "templates" / "renfield-satellite.service.j2").read_text()
    ).render(satellite_hw_groups=groups, ansible_user="evdb", satellite_venv="/v",
             satellite_base_dir="/b")
    return next(line for line in unit.splitlines() if line.startswith("SupplementaryGroups="))


@pytest.mark.satellite
def test_pi_unit_keeps_its_hardware_groups():
    assert _unit_groups("raspberrypi") == "SupplementaryGroups=spi gpio i2c"


@pytest.mark.satellite
def test_orange_pi_unit_names_only_groups_ubuntu_has():
    # Ubuntu 22.04 on the A733 has no spi/gpio groups; naming one is 216/GROUP.
    assert _unit_groups("orangepi") == "SupplementaryGroups=i2c video plugdev"
