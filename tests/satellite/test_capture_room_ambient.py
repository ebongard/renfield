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
import fnmatch
import json
import os
import pathlib
import re
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
    restart_begun = tmp_path / "restart-begun"
    restart_finished = tmp_path / "restart-finished"
    start_tries = tmp_path / "start-tries"

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
        # START_FAIL_TIMES=N: only the first N attempts fail — a flaky link,
        # which is the normal Pi Zero 2 W 2.4 GHz Wi-Fi failure mode.
        '        if [ "${START_FAIL_TIMES:-0}" != "0" ]; then\n'
        '          n=$(( $(cat "$START_TRIES" 2>/dev/null || echo 0) + 1 ))\n'
        '          echo "$n" > "$START_TRIES"\n'
        '          if [ "$n" -le "${START_FAIL_TIMES}" ]; then\n'
        '            echo "Failed to start unit" >&2; exit 2\n'
        "          fi\n"
        "        fi\n"
        # RESTART_DELAY makes the restart slow (it IS slow over ssh to a Pi
        # Zero). The two markers distinguish "the restart was started" from
        # "the restart ran to completion" — a signal that kills it mid-flight
        # leaves the first and not the second.
        '        : > "$RESTART_BEGUN"\n'
        '        [ "${RESTART_DELAY:-0}" != "0" ] && sleep "$RESTART_DELAY"\n'
        '        : > "$RESTART_FINISHED"\n'
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
            "RESTART_BEGUN": str(restart_begun),
            "RESTART_FINISHED": str(restart_finished),
            "START_TRIES": str(start_tries),
            # Keep the retry pause real but short; production uses 5 s.
            "AMBIENT_START_RETRY_SECONDS": "0.2",
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
            self.restart_begun = restart_begun
            self.restart_finished = restart_finished
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

        def wait_for(self, path, proc=None, timeout=30, what="marker"):
            deadline = time.time() + timeout
            while not path.exists() and time.time() < deadline:
                if proc is not None and proc.poll() is not None:
                    raise AssertionError(f"script exited before the {what} appeared")
                time.sleep(0.05)
            assert path.exists(), f"the {what} never appeared"

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


@pytest.mark.parametrize("hat", ["2mic", "2mic-v2", "whisplay"])
def test_dsnoop_mic_captures_concurrently_and_never_touches_the_service(box, hat):
    """2mic, 2mic-v2 and whisplay resolve capture to a `type dsnoop` PCM — these
    are the hats where "the service keeps running" is true. NOT 4mic: it is a
    HAT too, and its template routes capture straight to plughw:."""
    box.env["HAT_TYPE"] = hat
    res = box.run("--minutes", "15")

    assert res.returncode == 0, res.stderr
    assert "the service keeps running" in res.stdout
    assert box.calls == [], f"the service was touched on a shared mic: {box.calls}"
    assert box.sidecar()["mic_capture_mode"] == "shared"


def test_4mic_is_exclusive_although_it_is_a_hat(box):
    """The AC108 4-mic array forbids dsnoop/dmix outright ("they crash the
    kernel driver"), so `arecord` and the service cannot share it. Classified
    off the template, not off the "HAT mics are concurrent" prose."""
    box.env["HAT_TYPE"] = "4mic"

    refused = box.run("--minutes", "15")
    assert refused.returncode != 0, "4mic was treated as a shared mic"
    assert "--allow-service-stop" in refused.stderr
    assert box.calls == []

    res = box.run("--minutes", "15", "--allow-service-stop")
    assert res.returncode == 0, res.stderr
    assert "the service keeps running" not in res.stdout
    ops = [c.split("systemctl ")[1].split()[0] for c in box.calls]
    assert ops[:2] == ["stop", "start"], box.calls
    assert box.sidecar()["mic_capture_mode"] == "exclusive"


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
    box.wait_for(box.marker, proc, what="capture")

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


def test_a_second_interrupt_does_not_abort_the_running_restart(box):
    """Ctrl-C goes to the whole foreground process group, so a second one hits
    the `ansible systemctl start` child as well — and the first Ctrl-C looks
    like a hang precisely because the restart over ssh to a Pi Zero takes
    seconds. `on_exit` therefore sets SIG_IGN for INT/TERM before touching the
    service: bash passes SIG_IGN on to children across exec, so the restart is
    shielded too."""
    box.env["HAT_TYPE"] = "xvf3800-usb"
    box.env["ARECORD_MODE"] = "hang"
    box.env["RESTART_DELAY"] = "3"

    proc = box.popen("--minutes", "45", "--allow-service-stop")
    box.wait_for(box.marker, proc, what="capture")

    os.killpg(os.getpgid(proc.pid), signal.SIGINT)        # the operator aborts
    box.wait_for(box.restart_begun, proc, what="restart")
    os.killpg(os.getpgid(proc.pid), signal.SIGINT)        # "it hangs!" — again
    _out, err = proc.communicate(timeout=60)

    assert box.restart_finished.exists(), (
        "the second interrupt killed the running restart — the room stays deaf\n"
        f"calls={box.calls}\n{err}"
    )
    ops = [c.split("systemctl ")[1].split()[0] for c in box.calls]
    assert "is-active" in ops, f"the restart was never confirmed: {box.calls}"
    assert proc.returncode != 0


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


def test_a_transient_start_failure_is_retried_instead_of_judged(box):
    """The fleet's own notes call this Wi-Fi "marginal" — one dropped packet
    must not turn into a deaf room."""
    box.env["HAT_TYPE"] = "xvf3800-usb"
    box.env["START_FAIL_TIMES"] = "1"  # first attempt fails, second works
    res = box.run("--minutes", "15", "--allow-service-stop")

    assert res.returncode == 0, res.stderr
    assert "CRITICAL" not in res.stderr
    assert "DEAF" not in res.stderr
    starts = [c for c in box.calls if "systemctl start" in c]
    assert len(starts) == 2, f"no second attempt: {box.calls}"
    assert "is active (attempt 2/3)" in res.stdout


def test_the_deaf_verdict_comes_only_after_every_attempt(box):
    box.env["HAT_TYPE"] = "xvf3800-usb"
    box.env["START_FAILS"] = "1"  # every attempt fails
    started = time.monotonic()
    res = box.run("--minutes", "15", "--allow-service-stop")
    elapsed = time.monotonic() - started

    assert res.returncode != 0
    starts = [c for c in box.calls if "systemctl start" in c]
    assert len(starts) == 3, f"expected 3 attempts, got {box.calls}"
    assert res.stderr.count("CRITICAL") == 1, "one verdict, not one per attempt"
    assert "after 3 attempts" in res.stderr
    # The pause between attempts really runs (2 x AMBIENT_START_RETRY_SECONDS).
    assert elapsed >= 0.4, f"the retry pause was skipped ({elapsed:.2f}s)"


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


def test_the_busy_advice_on_a_shared_mic_is_followable(box):
    """The BUSY message on a mic classified as shared says "re-run with
    --allow-service-stop". The first version of #1388 printed that while the
    switch only lifted `unknown` -> `exclusive`: byte-identical output with and
    without it, zero systemctl calls, no way out but editing the script. This
    test holds the INTENT the message states — the advice has to work.
    """
    box.env["HAT_TYPE"] = "2mic"  # the table says shared …
    box.env["ARECORD_MODE"] = "busy"  # … and the device says otherwise

    refused = box.run("--minutes", "15")
    assert refused.returncode != 0
    assert "--allow-service-stop" in refused.stderr, "the message must name the way out"
    assert box.calls == []

    # Exactly what the message tells the operator to do.
    box.env["ARECORD_MODE"] = "ok"  # with the service out of the way it records
    followed = box.run("--minutes", "15", "--allow-service-stop")

    assert followed.returncode == 0, followed.stderr
    ops = [c.split("systemctl ")[1].split()[0] for c in box.calls]
    assert ops[:2] == ["stop", "start"], (
        f"the switch did not force the exclusive path on a 'shared' mic: {box.calls}"
    )
    assert box.sidecar()["mic_capture_mode"] == "exclusive"


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


# --- the table is derived from the templates, not from prose ----------------
# #1388 shipped with `4mic` in the shared column because the classification was
# written against the fleet's prose ("HAT mics allow concurrent capture") while
# asoundrc-4mic.j2 says the opposite. These two helpers re-derive the truth from
# the templates so the next drifting comment cannot move the behaviour.

TEMPLATES = REPO / "src/satellite/provisioning/templates"


def _pcm_blocks(text: str) -> dict:
    """{name: body} for every `pcm.<name> { ... }` definition, comments stripped."""
    text = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
    blocks, i = {}, 0
    while True:
        m = re.compile(r"pcm\.(!?[\w:.-]+)\s*\{").search(text, i)
        if not m:
            return blocks
        depth, j = 1, m.end()
        while depth and j < len(text):
            depth += {"{": 1, "}": -1}.get(text[j], 0)
            j += 1
        blocks[m.group(1)] = text[m.end() : j - 1]
        i = m.end()


def _slave_pcm(body: str) -> str:
    """The PCM a block feeds from: `slave.pcm "x"` or `slave { pcm "x" ... }`."""
    m = re.search(r'slave\.pcm\s*"([^"]+)"', body) or re.search(
        r'slave\s*\{[^}]*?\bpcm\s*"([^"]+)"', body, re.S
    )
    return m.group(1) if m else ""


def capture_mode_of_template(path: pathlib.Path) -> str:
    """Follow !default's capture chain: a `type dsnoop` on the way = shareable,
    a hw:/plughw: terminal = exclusive."""
    blocks = _pcm_blocks(path.read_text())
    default = blocks["!default"]
    inline = re.search(r"capture\.pcm\s*\{(.*?)\}", default, re.S)
    named = re.search(r'capture\.pcm\s*"([^"]+)"', default)
    ref = _slave_pcm(inline.group(1)) if inline else (named.group(1) if named else "")
    assert ref, f"{path.name}: no capture PCM found"

    for _ in range(10):
        if ref.startswith(("hw:", "plughw:", "dmix:", "default:")):
            return "exclusive"          # a direct device open
        body = blocks.get(ref)
        if body is None:
            return "exclusive"          # an unresolvable name is a device name
        if re.search(r"\btype\s+dsnoop\b", body):
            return "shared"
        ref = _slave_pcm(body)
        assert ref, f"{path.name}: capture chain dead-ends in {body!r}"
    raise AssertionError(f"{path.name}: capture chain does not terminate")


def capture_mode_of_script(hat_type: str) -> str:
    """Classify `hat_type` exactly as the script's case table does."""
    body = SCRIPT_SRC.read_text()
    table = re.search(r'case "\$HAT_TYPE" in(.*?)\nesac', body, re.S)
    assert table, "the case table moved — this guard needs updating"
    for arm in re.finditer(
        r'^\s*([^\s)]+)\)\s*CAPTURE_MODE="(\w+)"', table.group(1), re.M
    ):
        patterns, mode = arm.group(1).split("|"), arm.group(2)
        if any(fnmatch.fnmatchcase(hat_type, pat) for pat in patterns):
            return mode
    raise AssertionError(f"no arm matched {hat_type}")


def test_every_asoundrc_template_has_a_matching_case_table_entry():
    templates = sorted(TEMPLATES.glob("asoundrc-*.j2"))
    assert len(templates) >= 5, templates
    # provision.yml deploys `asoundrc-{{ hat_type }}.j2`, so the filename IS the
    # hat_type — the mapping is mechanical, not a convention to be remembered.
    mismatched = {}
    for tpl in templates:
        hat = tpl.name[len("asoundrc-") : -len(".j2")]
        from_template = capture_mode_of_template(tpl)
        from_script = capture_mode_of_script(hat)
        if from_template != from_script:
            mismatched[hat] = (from_template, from_script)
    assert not mismatched, (
        "the case table contradicts the asoundrc templates "
        "(hat: template says / script says): " + repr(mismatched)
    )


def test_the_template_resolver_reads_the_known_profiles_correctly():
    """A guard whose resolver is wrong proves nothing — pin it to the four
    profiles whose templates were read by hand for #1388."""
    modes = {
        tpl.name: capture_mode_of_template(tpl)
        for tpl in sorted(TEMPLATES.glob("asoundrc-*.j2"))
    }
    assert modes == {
        "asoundrc-2mic-v2.j2": "shared",
        "asoundrc-2mic.j2": "shared",
        "asoundrc-4mic.j2": "exclusive",
        "asoundrc-whisplay.j2": "shared",
        "asoundrc-xvf3800-usb.j2": "exclusive",
    }, modes
