"""`bin/_ambient_inspect.py` records where a capture's gain came from (#1397).

An XVF3800 has no ALSA capture control: `amixer` found none of the HAT codec
controls, so every USB-mic sidecar carried `"mixer": {}` — no gain provenance,
although the commissioning doc (§2) calls a negative captured at an unknown gain
worthless. The helper now reads the gain chain off the chip with `xvf_host`.

The fake `xvf_host` and `amixer` here are real executables, run through the
helper's real `subprocess` call — including the working directory xvf_host needs
to find its shared libraries.
"""

import importlib.util
import json
import stat
import wave
from pathlib import Path

import pytest

pytestmark = pytest.mark.satellite

INSPECT = Path(__file__).resolve().parents[2] / "bin" / "_ambient_inspect.py"

SATELLITE_YAML = """\
audio:
  device: "default"
  channels: 2
  sample_rate: 16000
  combine: "select"
  select_channel: 0
beamforming:
  enabled: false
led:
  type: "xvf3800"
  xvf_host_path: "{xvf}"
"""


def _load():
    spec = importlib.util.spec_from_file_location("ambient_inspect_under_test", INSPECT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _exec(path: Path, body: str) -> Path:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


@pytest.fixture
def mod():
    return _load()


@pytest.fixture
def fake_xvf(tmp_path):
    """xvf_host that only works from its own dir (like the real one) and fails one param."""
    bindir = tmp_path / "xvfbin"
    bindir.mkdir()
    (bindir / "libcommand_map.so").write_text("")
    return _exec(
        bindir / "xvf_host",
        "#!/bin/sh\n"
        '[ -f ./libcommand_map.so ] || { echo "cannot load libcommand_map.so" >&2; exit 1; }\n'
        'case "$1" in\n'
        '  PP_AGCDESIREDLEVEL) echo "PP_AGCDESIREDLEVEL 0.015" ;;\n'
        '  PP_AGCONOFF) echo "PP_AGCONOFF 1" ;;\n'
        '  PP_MIN_NS) echo "device busy" >&2; exit 2 ;;\n'
        "  AEC_ASROUTONOFF) exit 0 ;;\n"  # rc 0 but no output: no value
        '  *) echo "$1 7" ;;\n'
        "esac\n",
    )


def _config(tmp_path, mod, monkeypatch, xvf):
    cfg = tmp_path / "satellite.yaml"
    cfg.write_text(SATELLITE_YAML.format(xvf=xvf))
    monkeypatch.setattr(mod, "CONFIG_PATH", str(cfg))
    return cfg


def test_usb_probe_reads_the_gain_from_the_chip(tmp_path, mod, monkeypatch, fake_xvf):
    _config(tmp_path, mod, monkeypatch, fake_xvf)
    out = mod.probe("xvf3800-usb")

    assert out["gain_source"] == "xvf3800_chip"
    assert out["mixer"]["PP_AGCDESIREDLEVEL"] == "PP_AGCDESIREDLEVEL 0.015"
    assert out["mixer"]["PP_AGCONOFF"] == "PP_AGCONOFF 1"
    # A param that fails or answers nothing is left out, not recorded as "".
    assert "PP_MIN_NS" not in out["mixer"]
    assert "AEC_ASROUTONOFF" not in out["mixer"]
    # The rest of the probe is unchanged.
    assert out["device"] == "default" and out["channels"] == 2 and out["combine"] == "select"


def test_usb_probe_falls_back_to_the_fleet_path(tmp_path, mod, monkeypatch, fake_xvf):
    cfg = tmp_path / "satellite.yaml"
    cfg.write_text('audio:\n  device: "default"\n')
    monkeypatch.setattr(mod, "CONFIG_PATH", str(cfg))
    monkeypatch.setattr(mod, "XVF_HOST_DEFAULT", str(fake_xvf))
    assert mod.xvf_host_path(cfg.read_text()) == str(fake_xvf)
    assert mod.probe("xvf3800-usb")["gain_source"] == "xvf3800_chip"


def test_usb_probe_without_xvf_host_says_none(tmp_path, mod, monkeypatch):
    _config(tmp_path, mod, monkeypatch, tmp_path / "missing" / "xvf_host")
    out = mod.probe("xvf3800-usb")
    assert out["gain_source"] == "none"
    assert out["mixer"] == {}


def test_usb_probe_never_asks_amixer(tmp_path, mod, monkeypatch, fake_xvf):
    _config(tmp_path, mod, monkeypatch, fake_xvf)
    monkeypatch.setattr(mod, "read_mixer", lambda: pytest.fail("amixer asked on a USB mic"))
    assert mod.probe("xvf3800-usb")["gain_source"] == "xvf3800_chip"


def test_xvf_host_that_cannot_start_is_skipped(tmp_path, mod):
    broken = tmp_path / "xvf_host"
    broken.write_text("not executable")  # exists, but exec fails (OSError)
    assert mod.read_xvf(str(broken)) == {}


def test_hat_probe_reads_alsa_mixer(tmp_path, mod, monkeypatch, fake_xvf):
    _config(tmp_path, mod, monkeypatch, fake_xvf)
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    _exec(
        stubs / "amixer",
        "#!/bin/sh\n"
        'case "$4" in\n'
        "  PGA) printf \"Simple mixer control 'PGA',0\\n  Mono: 50 [78%%]\\n\" ;;\n"
        "  *) exit 1 ;;\n"
        "esac\n",
    )
    monkeypatch.setenv("PATH", f"{stubs}:/usr/bin:/bin")
    out = mod.probe("2mic")
    assert out["gain_source"] == "alsa_mixer"
    assert out["mixer"] == {"PGA": "Mono: 50 [78%]"}


def test_hat_probe_without_controls_says_none(tmp_path, mod, monkeypatch, fake_xvf):
    _config(tmp_path, mod, monkeypatch, fake_xvf)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))  # no amixer at all
    out = mod.probe("2mic")
    assert out == {**out, "gain_source": "none", "mixer": {}}


def test_unreadable_config_is_an_error(tmp_path, mod, monkeypatch):
    monkeypatch.setattr(mod, "CONFIG_PATH", str(tmp_path / "nope.yaml"))
    assert "cannot read" in mod.probe("xvf3800-usb")["error"]


def _wav(path: Path):
    import numpy as np

    data = (np.sin(np.arange(16000) / 5.0) * 3000).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(np.repeat(data[:, None], 2, axis=1).tobytes())


def test_gate_records_chip_provenance(tmp_path, mod, monkeypatch, fake_xvf):
    pytest.importorskip("numpy")
    _config(tmp_path, mod, monkeypatch, fake_xvf)
    wav = tmp_path / "c.wav"
    _wav(wav)
    out = mod.gate(str(wav), "xvf3800-usb")
    assert out["gain_source"] == "xvf3800_chip"
    assert "PP_AGCDESIREDLEVEL" in out["mixer"]
    assert out["channels"] == 2 and len(out["per_channel"]) == 2


def test_gate_without_config_still_reads_the_default_path(tmp_path, mod, monkeypatch, fake_xvf):
    pytest.importorskip("numpy")
    monkeypatch.setattr(mod, "CONFIG_PATH", str(tmp_path / "nope.yaml"))
    monkeypatch.setattr(mod, "XVF_HOST_DEFAULT", str(fake_xvf))
    wav = tmp_path / "c.wav"
    _wav(wav)
    assert mod.gate(str(wav), "xvf3800-usb")["gain_source"] == "xvf3800_chip"


def test_main_passes_the_hat_type(tmp_path, mod, monkeypatch, capsys, fake_xvf):
    _config(tmp_path, mod, monkeypatch, fake_xvf)
    monkeypatch.setattr("sys.argv", ["_inspect.py", "probe", "xvf3800-usb"])
    assert mod.main() == 0
    line = capsys.readouterr().out.strip()
    assert line.startswith("AMBIENT_JSON:")
    assert json.loads(line[len("AMBIENT_JSON:") :])["gain_source"] == "xvf3800_chip"


def test_main_without_hat_type_keeps_the_alsa_path(tmp_path, mod, monkeypatch, capsys, fake_xvf):
    """An older caller (no hat argument) gets the previous behaviour."""
    _config(tmp_path, mod, monkeypatch, fake_xvf)
    monkeypatch.setattr(mod, "read_mixer", lambda: {"PGA": "x"})
    monkeypatch.setattr("sys.argv", ["_inspect.py", "probe"])
    assert mod.main() == 0
    assert json.loads(capsys.readouterr().out.split(":", 1)[1])["gain_source"] == "alsa_mixer"


def test_main_gate_argument_forms(tmp_path, mod, monkeypatch, capsys, fake_xvf):
    pytest.importorskip("numpy")
    _config(tmp_path, mod, monkeypatch, fake_xvf)
    wav = tmp_path / "c.wav"
    _wav(wav)
    monkeypatch.setattr("sys.argv", ["_inspect.py", "gate", str(wav), "xvf3800-usb"])
    assert mod.main() == 0
    assert json.loads(capsys.readouterr().out.split(":", 1)[1])["gain_source"] == "xvf3800_chip"
    monkeypatch.setattr("sys.argv", ["_inspect.py", "gate"])
    assert mod.main() == 2
