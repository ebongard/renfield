"""The voice-server release smoke really runs and fails closed (#1645).

Until #1645 the smoke in ``bin/release-voice-server.sh`` was
``docker run --rm … python - <<'PY'``. Without ``-i`` docker attaches no
stdin, so ``python -`` ran an EMPTY program, exited 0, and the step passed
having asserted nothing, in every release up to v0.3.12.

The step is the function ``voice_release_smoke`` in
``bin/lib/voice-release-smoke.sh``. It is run here against two stubs:
- a stub ``ssh`` runs the remote command locally;
- a stub ``docker`` behaves like the real one where it matters: it reads stdin
  ONLY when ``-i`` is given. With stdin it plays the image's Python. The
  program it received must contain the version assert, and then it prints what
  the test tells it to.

So the passing case proves the program reaches the "image", and the other cases
prove that silence, a wrong version and a failing check each fail the step.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit]

_REPO = Path(__file__).resolve().parents[2]
_LIB = _REPO / "bin" / "lib" / "voice-release-smoke.sh"

_SSH = """#!/bin/sh
# ssh <host> <command>: run the command locally, the way the build host would.
shift
exec /bin/sh -c "$1"
"""

# Real docker semantics for this purpose: no -i -> stdin is NOT read.
_DOCKER = """#!/bin/sh
interactive=0
for a in "$@"; do [ "$a" = "-i" ] && interactive=1; done
if [ "$interactive" = 1 ]; then
  program=$(cat)
  case "$program" in
    *"assert __version__ == \\"$EXPECT_VERSION\\""*) ;;
    *) echo "stub: the program did not carry the version assert" >&2; exit 3 ;;
  esac
  [ -n "$STUB_PRINT" ] && printf '%s\\n' "$STUB_PRINT"
fi
exit "${STUB_EXIT:-0}"
"""


def _smoke(tmp_path: Path, *, version: str = "1.2.3", stub_print: str, stub_exit: int = 0,
           lib_text: str | None = None) -> subprocess.CompletedProcess:
    if shutil.which("bash") is None:  # pragma: no cover - environment guard
        pytest.skip("bash not available")
    assert _LIB.is_file(), f"incomplete copy: {_LIB} is missing (rsync bin/ too)"
    stubs = tmp_path / "bin"
    stubs.mkdir()
    for name, body in (("ssh", _SSH), ("docker", _DOCKER)):
        p = stubs / name
        p.write_text(body)
        p.chmod(0o755)
    lib = _LIB
    if lib_text is not None:
        lib = tmp_path / "lib.sh"
        lib.write_text(lib_text)
    env = {
        "PATH": f"{stubs}:/usr/bin:/bin",
        "EXPECT_VERSION": version,
        "STUB_PRINT": stub_print,
        "STUB_EXIT": str(stub_exit),
    }
    return subprocess.run(
        ["bash", "-c", f'source "{lib}"; voice_release_smoke buildhost reg/voice-server:v{version} {version}'],
        capture_output=True, text=True, env=env, timeout=30, stdin=subprocess.DEVNULL,
        cwd=os.fspath(tmp_path),
    )


def test_a_real_smoke_passes(tmp_path):
    proc = _smoke(tmp_path, stub_print="NVIDIA banner\nsmoke ok: 1.2.3")
    assert proc.returncode == 0, proc.stderr
    assert "smoke ok: 1.2.3" in proc.stdout


def test_silence_fails_closed(tmp_path):
    """The pre-#1645 symptom: the image prints nothing and exits 0."""
    proc = _smoke(tmp_path, stub_print="")
    assert proc.returncode == 1
    assert "did not run" in proc.stderr


def test_a_wrong_version_fails(tmp_path):
    proc = _smoke(tmp_path, stub_print="smoke ok: 9.9.9")
    assert proc.returncode == 1


def test_a_failing_check_in_the_image_fails(tmp_path):
    proc = _smoke(tmp_path, stub_print="AssertionError: torch 2.15, expected +cu128", stub_exit=1)
    assert proc.returncode == 1
    assert "failed" in proc.stderr


def test_NEGATIVE_without_dash_i_the_program_never_arrives(tmp_path):
    """The stub is faithful: drop `-i` from the real lib and the smoke must FAIL.

    Before #1645 this was the shipped code, and it passed. Here it fails,
    because the stub reads no stdin without -i and so prints nothing.
    """
    text = _LIB.read_text()
    assert "docker run -i --rm" in text, "the lib no longer passes -i the way this test edits it"
    proc = _smoke(tmp_path, stub_print="smoke ok: 1.2.3",
                  lib_text=text.replace("docker run -i --rm", "docker run --rm"))
    assert proc.returncode == 1


def test_the_release_script_uses_the_lib():
    """The release script must call the tested function, not an inline copy."""
    script = (_REPO / "bin" / "release-voice-server.sh").read_text()
    code = [ln for ln in script.splitlines() if not ln.lstrip().startswith("#")]
    assert any('source "$REPO_ROOT/bin/lib/voice-release-smoke.sh"' in ln for ln in code)
    assert any(ln.strip().startswith('voice_release_smoke "$BUILD_HOST"') for ln in code)
    assert not any("python - <<'PY'" in ln for ln in code), "an inline smoke copy is back"
