"""The server pin survives a reboot under cloud-init.

Raspberry Pi Imager images carry `manage_etc_hosts: true` in their user-data, so
cloud-init renders /etc/hosts from its template on every boot. The pin went into
/etc/hosts only and vanished with the next reboot: the Wohnzimmer (2026-11-09)
could no longer resolve renfield.local and never reached the backend again.

These tests run the REAL tasks, lifted out of provision.yml by name, with the
real `ansible-playbook` against localhost; both paths are play vars the test
points at files it writes.
"""

import copy
import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.satellite

PLAYBOOK = Path(__file__).resolve().parents[2] / "src" / "satellite" / "provisioning" / "provision.yml"
TASKS = ("Find cloud-init's hosts template", "Pin the server hostname in /etc/hosts")
PIN = "192.0.2.10 renfield.local"

TEMPLATE = "127.0.1.1 {{fqdn}} {{hostname}}\n127.0.0.1 localhost\n"
HOSTS = "127.0.1.1 sat sat\n127.0.0.1 localhost\n"

ansible_playbook = shutil.which("ansible-playbook")
needs_ansible = pytest.mark.skipif(ansible_playbook is None, reason="ansible-playbook not installed")


def _play() -> dict:
    return yaml.safe_load(PLAYBOOK.read_text())[0]


def _task(name: str) -> dict:
    for task in _play()["tasks"]:
        if task.get("name") == name:
            return copy.deepcopy(task)
    raise AssertionError(f"task {name!r} not found in provision.yml")


def _run(tmp_path, *, with_template, static_ip="192.0.2.10"):
    # A second run in the same tmp_path keeps the files, as a re-provisioning would.
    hosts = tmp_path / "hosts"
    if not hosts.exists():
        hosts.write_text(HOSTS)
    template = tmp_path / "hosts.debian.tmpl"
    if with_template and not template.exists():
        template.write_text(TEMPLATE)

    play = {
        "name": "hosts pin under test",
        "hosts": "localhost",
        "connection": "local",
        "gather_facts": False,
        "tasks": [_task(n) for n in TASKS],
    }
    pb = tmp_path / "pb.yml"
    pb.write_text(yaml.safe_dump([play], allow_unicode=True, sort_keys=False))

    extra = {
        "etc_hosts_path": str(hosts),
        "cloud_init_hosts_template": str(template),
        "server_url": "wss://renfield.local/ws/satellite",
    }
    if static_ip:
        extra["server_static_ip"] = static_ip
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin:" + str(Path(ansible_playbook).parent),
        "HOME": str(tmp_path),
        "ANSIBLE_LOCAL_TEMP": str(tmp_path / "ansible-tmp"),
        "ANSIBLE_NOCOLOR": "1",
        "ANSIBLE_RETRY_FILES_ENABLED": "0",
        "LANG": "C.UTF-8",
    }
    cmd = [ansible_playbook, "-i", "localhost,", str(pb), "-e", json.dumps(extra), "--tags", "network"]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=180, env=env)
    assert res.returncode == 0, res.stdout + res.stderr
    return hosts.read_text(), (template.read_text() if template.exists() else None)


@needs_ansible
def test_the_pin_also_goes_into_cloud_inits_template(tmp_path):
    hosts, template = _run(tmp_path, with_template=True)
    assert PIN in hosts.splitlines()
    assert PIN in template.splitlines()
    # The template stays a template: cloud-init still fills in the host name.
    assert "{{fqdn}} {{hostname}}" in template


@needs_ansible
def test_without_cloud_init_only_etc_hosts_is_pinned(tmp_path):
    hosts, template = _run(tmp_path, with_template=False)
    assert PIN in hosts.splitlines()
    assert template is None


@needs_ansible
def test_a_changed_ip_replaces_the_old_pin(tmp_path):
    _run(tmp_path, with_template=True)
    hosts, template = _run(tmp_path, with_template=True, static_ip="192.0.2.20")
    for text in (hosts, template):
        lines = [line for line in text.splitlines() if line.endswith("renfield.local")]
        assert lines == ["192.0.2.20 renfield.local"]


@needs_ansible
def test_without_a_static_ip_nothing_is_touched(tmp_path):
    hosts, template = _run(tmp_path, with_template=True, static_ip=None)
    assert hosts == HOSTS
    assert template == TEMPLATE
