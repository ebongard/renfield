"""Both stale-overlay cleanups in provision.yml read ONE list.

The playbook used to carry two hand-written lists of I2S HAT overlays to strip
from config.txt, one per sound card, and the USB one had fallen behind: it
lacked the two `seeed-2mic-gpclk-*` overlays, so a satellite swapped from that
HAT to the USB microphone would have kept a phantom card.
"""

from pathlib import Path

import pytest
import yaml
from jinja2 import Environment

pytestmark = pytest.mark.satellite

PROVISIONING = Path(__file__).resolve().parents[2] / "src" / "satellite" / "provisioning"
OWN_OVERLAY_2MIC_V2 = "respeaker-2mic-v2_0"
TASK_2MIC = "Remove stale conflicting I2S HAT overlays from config.txt (2-mic-v2)"
TASK_USB = "Remove stale I2S HAT overlays from config.txt (USB mic)"


def _tasks() -> list[dict]:
    return yaml.safe_load((PROVISIONING / "provision.yml").read_text())[0]["tasks"]


def _task(name: str) -> dict:
    for task in _tasks():
        if task.get("name") == name:
            return task
    raise AssertionError(f"task {name!r} not found in provision.yml")


def _overlays() -> list[str]:
    return yaml.safe_load((PROVISIONING / "group_vars" / "satellites.yml").read_text())[
        "stale_i2s_hat_overlays"
    ]


def _loop_items(task: dict) -> list[str]:
    """Evaluate the task's loop expression the way Ansible would."""
    expression = task["loop"]
    assert isinstance(expression, str), f"{task['name']!r} loops over a literal list again"
    inner = expression.strip().removeprefix("{{").removesuffix("}}")
    return list(Environment().compile_expression(inner)(stale_i2s_hat_overlays=_overlays()))


def test_the_list_names_every_known_i2s_hat_overlay():
    assert sorted(_overlays()) == sorted(
        [
            "seeed-2mic-gpclk-simple",
            "seeed-2mic-gpclk-full",
            "seeed-2mic-voicecard",
            "seeed-4mic-voicecard",
            "respeaker-2mic-v2_0",
            "wm8960-soundcard",
        ]
    )


def test_the_usb_microphone_strips_every_overlay():
    assert _loop_items(_task(TASK_USB)) == _overlays()


def test_the_2mic_v2_hat_strips_every_overlay_but_its_own():
    items = _loop_items(_task(TASK_2MIC))
    assert OWN_OVERLAY_2MIC_V2 not in items
    assert items == [name for name in _overlays() if name != OWN_OVERLAY_2MIC_V2]


def test_no_overlay_cleanup_carries_a_list_of_its_own():
    """Any task that deletes `dtoverlay=<item>` lines loops over the shared list."""
    cleanups = [
        task
        for task in _tasks()
        if task.get("ansible.builtin.lineinfile", {}).get("regexp") == "^dtoverlay={{ item }}"
        and task["ansible.builtin.lineinfile"].get("state") == "absent"
    ]
    assert {task["name"] for task in cleanups} == {TASK_2MIC, TASK_USB}
    for task in cleanups:
        assert isinstance(task["loop"], str) and "stale_i2s_hat_overlays" in task["loop"]
