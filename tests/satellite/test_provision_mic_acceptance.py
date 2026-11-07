"""The provisioning run fails when the satellite has no microphone (#1387).

`provision.yml` waited for the sound card with `ignore_errors` and never looked
at the result again, so a run on a satellite without a capture device ended
green — exactly the state of the Wohnzimmer on 2026-10-10 (only `vc4hdmi`).

These tests run the REAL tasks, lifted out of provision.yml by name, with the
real `ansible-playbook` against localhost; only `/proc/asound/cards` is replaced
by a file the test writes (the play var `asound_cards_path`). A structural
assertion on the YAML would only prove the words are there.
"""

import copy
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.satellite

PROVISIONING = Path(__file__).resolve().parents[2] / "src" / "satellite" / "provisioning"
PLAYBOOK = PROVISIONING / "provision.yml"

WAIT_TASK = "Wait for the microphone's sound card"
SHOW_TASKS = ("Show detected sound cards", "Display sound cards")
GATE_TASK = "Acceptance — microphone and service"
SUMMARY_TASK = "Show provisioning summary"

# /proc/asound/cards as the devices print it.
CARDS_XVF = (
    " 0 [vc4hdmi        ]: vc4-hdmi - vc4-hdmi\n"
    "                      vc4-hdmi\n"
    " 1 [Array          ]: USB-Audio - reSpeaker XVF3800 4-Mic Array\n"
    "                      Seeed Studio reSpeaker XVF3800 at usb-3f980000.usb-1\n"
)
CARDS_HDMI_ONLY = " 0 [vc4hdmi        ]: vc4-hdmi - vc4-hdmi\n                      vc4-hdmi\n"
CARDS_2MIC = " 1 [seeed2micvoicec]: seeed-2mic-voic - seeed-2mic-voicecard\n"
CARDS_WHISPLAY = " 1 [wm8960soundcard]: wm8960-soundcar - wm8960-soundcard\n"

ansible_playbook = shutil.which("ansible-playbook")
needs_ansible = pytest.mark.skipif(ansible_playbook is None, reason="ansible-playbook not installed")


def _play() -> dict:
    return yaml.safe_load(PLAYBOOK.read_text())[0]


def _task(name: str) -> dict:
    for task in _play()["tasks"]:
        if task.get("name") == name:
            return copy.deepcopy(task)
    raise AssertionError(f"task {name!r} not found in provision.yml")


def _run(tmp_path, *, hat_type, cards, service_rc=0, tags=None, check=False):
    """Run the wait + display + gate + summary tasks against localhost."""
    cards_file = tmp_path / "cards"
    cards_file.write_text(cards)

    fake_service = {
        "name": "Check service status",
        "tags": ["service"],
        "ansible.builtin.command": f"sh -c 'exit {service_rc}'",
        "register": "service_status",
        "changed_when": False,
        "ignore_errors": True,
    }
    tasks = [_task(WAIT_TASK), *(_task(n) for n in SHOW_TASKS), fake_service, _task(GATE_TASK), _task(SUMMARY_TASK)]
    play = {
        "name": "mic acceptance under test",
        "hosts": "localhost",
        "connection": "local",
        "gather_facts": False,
        "vars": _play()["vars"],
        "tasks": tasks,
    }
    pb = tmp_path / "pb.yml"
    pb.write_text(yaml.safe_dump([play], allow_unicode=True, sort_keys=False))

    extra = {
        "hat_type": hat_type,
        "asound_cards_path": str(cards_file),
        "mic_card_timeout": 1,
        # Referenced by the summary only.
        "satellite_id": "sat-test",
        "satellite_room": "Test",
        "server_url": "ws://example.invalid",
    }
    cmd = [ansible_playbook, "-i", "localhost,", str(pb), "-e", json.dumps(extra)]
    if tags:
        cmd += ["--tags", tags]
    if check:
        cmd.append("--check")
    env = {
        "PATH": "/usr/bin:/bin:/usr/local/bin:" + str(Path(ansible_playbook).parent),
        "HOME": str(tmp_path),
        "ANSIBLE_LOCAL_TEMP": str(tmp_path / "ansible-tmp"),
        "ANSIBLE_NOCOLOR": "1",
        "ANSIBLE_RETRY_FILES_ENABLED": "0",
        "LANG": "C.UTF-8",
    }
    return subprocess.run(cmd, capture_output=True, text=True, timeout=180, env=env)


@needs_ansible
def test_usb_mic_present_passes(tmp_path):
    res = _run(tmp_path, hat_type="xvf3800-usb", cards=CARDS_XVF)
    assert res.returncode == 0, res.stdout + res.stderr
    assert "Satellite provisioned successfully" in res.stdout


@needs_ansible
def test_no_mic_fails_the_run(tmp_path):
    """The Wohnzimmer case: only the HDMI card — the run must end red."""
    res = _run(tmp_path, hat_type="xvf3800-usb", cards=CARDS_HDMI_ONLY)
    assert res.returncode != 0
    assert "is NOT accepted" in res.stdout
    assert "MICROPHONE" in res.stdout and "'Array'" in res.stdout
    assert "Satellite provisioned successfully" not in res.stdout
    # The service clause stays silent when only the mic is missing.
    assert "SERVICE:" not in res.stdout


@needs_ansible
def test_hat_mic_present_passes(tmp_path):
    res = _run(tmp_path, hat_type="2mic", cards=CARDS_2MIC)
    assert res.returncode == 0, res.stdout + res.stderr


@needs_ansible
def test_another_hat_card_does_not_stand_in(tmp_path):
    """The old regex "seeed|wm8960" accepted any HAT card for any HAT."""
    res = _run(tmp_path, hat_type="2mic", cards=CARDS_WHISPLAY)
    assert res.returncode != 0
    assert "'seeed2micvoicec'" in res.stdout


@needs_ansible
def test_inactive_service_fails_the_run(tmp_path):
    res = _run(tmp_path, hat_type="whisplay", cards=CARDS_WHISPLAY, service_rc=3)
    assert res.returncode != 0
    assert "SERVICE:" in res.stdout and "MICROPHONE" not in res.stdout
    assert "Satellite provisioned successfully" not in res.stdout


@needs_ansible
def test_unknown_hat_type_fails_closed(tmp_path):
    res = _run(tmp_path, hat_type="8mic", cards=CARDS_XVF)
    assert res.returncode != 0
    assert "unknown hat_type 8mic" in res.stdout


@needs_ansible
def test_config_only_run_is_not_blocked_by_a_missing_mic(tmp_path):
    """`--tags config` does not run the mic check, so it gives no verdict."""
    res = _run(tmp_path, hat_type="xvf3800-usb", cards=CARDS_HDMI_ONLY, tags="config")
    assert res.returncode == 0, res.stdout + res.stderr
    assert "Satellite provisioned successfully" in res.stdout


@needs_ansible
def test_check_mode_gives_no_verdict(tmp_path):
    """`--check` skips wait_for; a skipped check must not count as a failure."""
    res = _run(tmp_path, hat_type="xvf3800-usb", cards=CARDS_HDMI_ONLY, check=True)
    assert res.returncode == 0, res.stdout + res.stderr


def test_card_map_matches_the_asoundrc_templates():
    """The gate's card id per hat_type is the card its asoundrc template opens."""
    card_map = _play()["vars"]["mic_alsa_card"]
    templates = sorted((PROVISIONING / "templates").glob("asoundrc-*.j2"))
    hat_types = {t.name[len("asoundrc-") : -len(".j2")] for t in templates}
    assert set(card_map) == hat_types
    for template in templates:
        hat = template.name[len("asoundrc-") : -len(".j2")]
        opened = set(re.findall(r'"(?:plug)?hw:([A-Za-z0-9_]+)', template.read_text())) - {"vc4hdmi"}
        assert opened == {card_map[hat]}, f"{template.name} opens {opened}, map says {card_map[hat]}"
        # ALSA truncates card ids to 15 characters; a longer id never matches.
        assert len(card_map[hat]) <= 15


def test_every_check_with_ignore_errors_feeds_the_gate():
    """No check in provision.yml may swallow its result again (#1387 class)."""
    gate = _task(GATE_TASK)
    gate_text = json.dumps(gate)
    ignoring = [t for t in _play()["tasks"] if t.get("ignore_errors")]
    assert ignoring, "expected the deferred checks"
    for task in ignoring:
        register = task.get("register")
        assert register, f"{task['name']!r} ignores errors without registering a result"
        assert register in gate_text, f"{task['name']!r} registers {register!r}, which the gate never reads"
    tasks = [t.get("name") for t in _play()["tasks"]]
    assert tasks.index(GATE_TASK) < tasks.index(SUMMARY_TASK)
    assert "always" in gate["tags"]
