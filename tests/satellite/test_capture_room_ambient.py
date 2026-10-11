"""Tests for `bin/capture-room-ambient.sh` — the mic-ownership branch (#1388).

The script's own docs knew the USB case and it did not act on it: it promised
"the service keeps running" unconditionally, while `asoundrc-xvf3800-usb.j2`
deliberately defines no dsnoop PCM, so `arecord` and the running satellite
service both want `hw:Array` exclusively and the second opener loses the device.

These tests run the REAL script against stubbed `ansible` / `ansible-inventory`
/ `scp` on PATH (same approach as test_net_watchdog.py) and assert on the side
effects: was the service stopped, was it started again, what did the operator
get told. The script derives its repo root from its own location, so it is
copied into a tmp tree that mirrors the repo layout — the capture then lands in
the tmp tree and never touches `data/wakeword-ambient/`.

The load-bearing case is `test_interrupted_capture_still_restarts_the_service`:
a trap nobody exercised is a claim, not a guarantee, and a satellite left deaf
by an aborted capture would be worse than today's failure.
"""
import json
import os
import shutil
import signal
import stat
import subprocess
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.satellite

REPO = Path(__file__).resolve().parents[2]
SCRIPT_SRC = REPO / "bin/capture-room-ambient.sh"
INSPECT_SRC = REPO / "bin/_ambient_inspect.py"

HOST = "satellite-testraum"
WAV_PAYLOAD = "RIFFfake-wav-payload"

PROBE_JSON = json.dumps(
    {
        "device": "default",
        "channels": 2,
        "sample_rate": 16000,
        "beamforming": False,
        "combine": "",
        "select_channel": "",
        "mic_spacing": "",
        "mixer": {"PGA": "Mono: 50 [78%]"},
    }
)
GATE_JSON = json.dumps(
    {
        "duration_seconds": 900.0,
        "channels": 2,
        "sample_rate": 16000,
        "mixer": {"PGA": "Mono: 50 [78%]"},
        "per_channel": [
            {
                "channel": c,
                "dc_offset": 1.2,
                "rms": 500.0,
                "peak": 3000.0,
                "rms_dbfs": -36.3,
                "clipped_fraction": 0.0,
                "crest": 6.0,
            }
            for c in (0, 1)
        ],
    }
)


def _write_exec(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def box(tmp_path):
    """A sandbox: the real script in a mirrored repo tree + stub commands whose
    behaviour the test steers through env vars."""
    (tmp_path / "bin").mkdir()
    script = tmp_path / "bin/capture-room-ambient.sh"
    shutil.copy2(SCRIPT_SRC, script)
    shutil.copy2(INSPECT_SRC, tmp_path / "bin/_ambient_inspect.py")

    inventory = tmp_path / "src/satellite/provisioning/inventory.yml"
    inventory.parent.mkdir(parents=True)
    # Content is irrelevant: the stub ansible-inventory answers the lookups.
    inventory.write_text("all:\n  children: {}\n")

    stubs = tmp_path / "stubs"
    stubs.mkdir()
    calls = tmp_path / "calls"
    marker = tmp_path / "capture-started"

    # ansible-inventory --host <h>: emits the host vars, or fails when the host
    # is not in the inventory (HOST_IN_INVENTORY=0).
    _write_exec(
        stubs / "ansible-inventory",
        "#!/usr/bin/env bash\n"
        'if [ "${HOST_IN_INVENTORY:-1}" != "1" ]; then\n'
        '  echo "ERROR! Could not match supplied host pattern" >&2\n'
        "  exit 1\n"
        "fi\n"
        'if [ -n "${HAT_TYPE:-}" ]; then\n'
        '  printf \'{"ansible_host": "10.0.0.9", "ansible_user": "pi", "hat_type": "%s"}\\n\' "$HAT_TYPE"\n'
        "else\n"
        '  printf \'{"ansible_host": "10.0.0.9", "ansible_user": "pi"}\\n\'\n'
        "fi\n",
    )

    # ansible <host> -i <inv> [-b] -m shell -a <cmd>
    _write_exec(
        stubs / "ansible",
        "#!/usr/bin/env bash\n"
        "cmd=\"\"\n"
        "while [ $# -gt 0 ]; do\n"
        '  if [ "$1" = "-a" ]; then cmd="$2"; break; fi\n'
        "  shift\n"
        "done\n"
        'echo "'
        + HOST
        + ' | CHANGED | rc=0 >>"\n'
        'case "$cmd" in\n'
        "  *systemctl*)\n"
        '    echo "$cmd" >> "$CALLS"\n'
        '    case "$cmd" in\n'
        '      *"systemctl stop"*)\n'
        '        [ "${STOP_FAILS:-0}" = "1" ] && { echo "Failed to stop unit" >&2; exit 2; }\n'
        "        exit 0 ;;\n"
        '      *"systemctl start"*)\n'
        '        [ "${START_FAILS:-0}" = "1" ] && { echo "Failed to start unit" >&2; exit 2; }\n'
        "        exit 0 ;;\n"
        '      *"systemctl is-active"*)\n'
        '        if [ "${START_FAILS:-0}" = "1" ]; then echo "inactive"; exit 3; fi\n'
        '        echo "active"; exit 0 ;;\n'
        "    esac ;;\n"
        "  *_inspect.py\\ probe*)\n"
        '    echo "AMBIENT_JSON:$PROBE_JSON"; exit 0 ;;\n'
        "  *_inspect.py\\ gate*)\n"
        '    echo "AMBIENT_JSON:$GATE_JSON"; exit 0 ;;\n'
        "  *arecord*)\n"
        '    case "${ARECORD_MODE:-ok}" in\n'
        "      busy)\n"
        '        echo "arecord: main:830: audio open error: Device or resource busy" >&2\n'
        "        exit 2 ;;\n"
        "      fail)\n"
        '        echo "arecord: set_params:1416: Sample format non available" >&2\n'
        "        exit 2 ;;\n"
        "      hang)\n"
        '        : > "$CAPTURE_MARKER"\n'
        "        sleep 60; exit 0 ;;\n"
        "      *) exit 0 ;;\n"
        "    esac ;;\n"
        "  *sha256sum*)\n"
        '    printf \'%s  /tmp/renfield-ambient/x.wav\\n\' "$(printf \'%s\' "$WAV_PAYLOAD" | shasum -a 256 | cut -d" " -f1)"\n'
        "    exit 0 ;;\n"
        "esac\n"
        "exit 0\n",
    )

    # scp: materialise the "transferred" capture locally so the checksum matches.
    _write_exec(
        stubs / "scp",
        "#!/usr/bin/env bash\n"
        'dest="${@: -1}"\n'
        'printf \'%s\' "$WAV_PAYLOAD" > "$dest"\n',
    )

    env = dict(os.environ)
    env.update(
        {
            "PATH": f"{stubs}:{env['PATH']}",
            "CALLS": str(calls),
            "CAPTURE_MARKER": str(marker),
            "PROBE_JSON": PROBE_JSON,
            "GATE_JSON": GATE_JSON,
            "WAV_PAYLOAD": WAV_PAYLOAD,
            "HAT_TYPE": "2mic",
        }
    )

    class Box:
        def __init__(self):
            self.script = script
            self.env = env
            self.calls_file = calls
            self.marker = marker
            self.out_dir = tmp_path / "data/wakeword-ambient" / HOST

        def run(self, *args, timeout=60):
            return subprocess.run(
                ["bash", str(self.script), HOST, *args],
                capture_output=True,
                text=True,
                env=self.env,
                timeout=timeout,
                stdin=subprocess.DEVNULL,  # no terminal -> no interactive prompt
            )

        def popen(self, *args):
            return subprocess.Popen(
                ["bash", str(self.script), HOST, *args],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=self.env,
                stdin=subprocess.DEVNULL,
                start_new_session=True,  # own process group: SIGINT stays here
            )

        @property
        def calls(self):
            if not self.calls_file.exists():
                return []
            return [ln for ln in self.calls_file.read_text().splitlines() if ln]

        def sidecar(self):
            files = sorted(self.out_dir.glob("*.json"))
            assert files, f"no sidecar written in {self.out_dir}"
            return json.loads(files[-1].read_text())

    return Box()


# --- the two branches --------------------------------------------------------


def test_hat_mic_captures_concurrently_and_never_touches_the_service(box):
    """2mic/4mic/whisplay share the mic via dsnoop — this is the one case where
    "the service keeps running" is true."""
    box.env["HAT_TYPE"] = "2mic"
    res = box.run("--minutes", "15")

    assert res.returncode == 0, res.stderr
    assert "the service keeps running" in res.stdout
    assert box.calls == [], f"the service was touched on a shared mic: {box.calls}"
    assert box.sidecar()["mic_capture_mode"] == "shared"


def test_usb_mic_stops_the_service_for_the_capture_and_starts_it_again(box):
    box.env["HAT_TYPE"] = "xvf3800-usb"
    res = box.run("--minutes", "15", "--allow-service-stop")

    assert res.returncode == 0, res.stderr
    assert "the service keeps running" not in res.stdout
    assert "is STOPPED" in res.stdout
    ops = [c.split("systemctl ")[1].split()[0] for c in box.calls]
    assert ops[:2] == ["stop", "start"], box.calls
    assert "is-active" in " ".join(box.calls)  # the restart is verified, not assumed
    side = box.sidecar()
    assert side["mic_capture_mode"] == "exclusive"
    assert side["hat_type"] == "xvf3800-usb"


# --- the stop is never silent ------------------------------------------------


def test_usb_mic_refuses_to_stop_the_service_without_authorisation(box):
    """No flag and no terminal to ask: refuse, and stop nothing."""
    box.env["HAT_TYPE"] = "xvf3800-usb"
    res = box.run("--minutes", "15")

    assert res.returncode != 0
    assert "--allow-service-stop" in res.stderr
    assert box.calls == [], f"stopped the service unasked: {box.calls}"


def test_unknown_hat_type_refuses_rather_than_guessing(box):
    box.env["HAT_TYPE"] = "brand-new-hat"
    res = box.run("--minutes", "15")

    assert res.returncode != 0
    assert "unknown hat_type" in res.stderr
    assert box.calls == []


def test_missing_hat_type_is_an_error_not_a_default(box):
    del box.env["HAT_TYPE"]
    res = box.run("--minutes", "15")

    assert res.returncode != 0
    assert "no hat_type" in res.stderr


# --- the counter-proof: the restart holds on an abort ------------------------


def test_interrupted_capture_still_restarts_the_service(box):
    """Ctrl-C during the capture. Without the trap the room stays deaf — so
    prove the start call actually happens, don't trust the trap's presence."""
    box.env["HAT_TYPE"] = "xvf3800-usb"
    box.env["ARECORD_MODE"] = "hang"

    proc = box.popen("--minutes", "45", "--allow-service-stop")
    deadline = time.time() + 30
    while not box.marker.exists() and time.time() < deadline:
        assert proc.poll() is None, "script exited before the capture started"
        time.sleep(0.1)
    assert box.marker.exists(), "capture never started"

    # Exactly what an operator's Ctrl-C does: SIGINT to the foreground group.
    os.killpg(os.getpgid(proc.pid), signal.SIGINT)
    _out, err = proc.communicate(timeout=30)

    assert proc.returncode != 0, "an interrupted capture must not report success"
    ops = [c.split("systemctl ")[1].split()[0] for c in box.calls]
    assert "stop" in ops, box.calls
    assert ops.index("start") > ops.index("stop"), (
        f"the service was stopped and not started again: {box.calls}\n{err}"
    )
    assert "interrupted" in err


def test_failed_capture_still_restarts_the_service(box):
    """The other abort path: arecord dies, `set -e`/`exit 1` unwinds."""
    box.env["HAT_TYPE"] = "xvf3800-usb"
    box.env["ARECORD_MODE"] = "fail"
    res = box.run("--minutes", "15", "--allow-service-stop")

    assert res.returncode != 0
    ops = [c.split("systemctl ")[1].split()[0] for c in box.calls]
    assert "stop" in ops and "start" in ops, box.calls
    assert ops.index("start") > ops.index("stop"), box.calls


def test_unconfirmed_restart_is_loud_and_fails_the_run(box):
    box.env["HAT_TYPE"] = "xvf3800-usb"
    box.env["START_FAILS"] = "1"
    res = box.run("--minutes", "15", "--allow-service-stop")

    assert res.returncode != 0
    assert "CRITICAL" in res.stderr
    assert "DEAF" in res.stderr
    assert "systemctl start" in res.stderr  # the manual recovery command


# --- the busy device says it is busy ----------------------------------------


def test_busy_device_is_reported_as_busy_not_as_a_config_error(box):
    box.env["HAT_TYPE"] = "2mic"
    box.env["ARECORD_MODE"] = "busy"
    res = box.run("--minutes", "15")

    assert res.returncode != 0
    assert "BUSY" in res.stderr
    assert "--allow-service-stop" in res.stderr
    # Neither the old misleading message nor raw ALSA text.
    assert "could not read the audio config" not in res.stderr
    assert "audio open error" not in res.stderr


def test_busy_device_on_an_exclusive_mic_blames_a_third_party(box):
    box.env["HAT_TYPE"] = "xvf3800-usb"
    box.env["ARECORD_MODE"] = "busy"
    res = box.run("--minutes", "15", "--allow-service-stop")

    assert res.returncode != 0
    assert "BUSY" in res.stderr
    assert "was stopped for this capture" in res.stderr
    ops = [c.split("systemctl ")[1].split()[0] for c in box.calls]
    assert "start" in ops, box.calls


# --- the Esszimmer pod is not an Ansible host -------------------------------


def test_host_outside_the_inventory_names_the_pod_case(box):
    box.env["HOST_IN_INVENTORY"] = "0"
    res = box.run("--minutes", "15")

    assert res.returncode != 0
    assert "no entry in" in res.stderr
    assert "k8s POD" in res.stderr
    assert box.calls == []
