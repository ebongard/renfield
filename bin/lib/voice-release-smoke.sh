# shellcheck shell=bash
#
# The post-build smoke of a voice-server release image. Sourced by
# bin/release-voice-server.sh; a function of its own so it can be tested
# against stub ssh/docker (tests/backend/test_voice_release_smoke.py).
#
#   voice_release_smoke <build-host> <image-ref> <version>
#
# Runs a Python check INSIDE the built image (the artifact, not the source):
# version == tag, the whole ASGI app imports, opus decode available, the
# cu128 torch/torchaudio build, pyannote importable.
#
# Two guarantees (#1645 - until then the step had never checked anything):
#   1. `docker run -i`. Without -i docker attaches no stdin, so `python -`
#      reads an EMPTY program, exits 0, and the step "passes" while asserting
#      nothing. That is exactly how every release up to v0.3.12 was smoked.
#   2. Fail closed on SILENCE. The step passes only on the exact line
#      `smoke ok: <version>`. No output at all fails it, so a future loss of
#      stdin (or a refactor that drops -i again) breaks the release instead
#      of quietly passing it.
#
# Returns 0 on a passed smoke, 1 otherwise; the caller decides how to fail.

voice_release_smoke() {
  local host="$1" ref="$2" version="$3" out
  out=$(ssh "$host" "docker run -i --rm -e AUTH_REQUIRED=false $ref python - <<'PY'
from voice_server import __version__
from voice_server.services.opus_decode import OPUSLIB_AVAILABLE
from voice_server.main import app  # import-smokes the whole ASGI surface
import torch, torchaudio  # the meeting stack must match the pinned cu128 build
assert torch.__version__.endswith(\"+cu128\"), f\"torch {torch.__version__}, expected +cu128\"
assert torchaudio.__version__.endswith(\"+cu128\"), f\"torchaudio {torchaudio.__version__}\"
from pyannote.audio import Pipeline  # noqa: F401 -- startup imports it (v0.3.8)
assert __version__ == \"$version\", f\"image reports {__version__}, expected $version\"
assert OPUSLIB_AVAILABLE, \"opuslib/libopus missing — satellites would 503 (silent skew)\"
print(\"smoke ok:\", __version__)
PY") || {
    printf '%s\n' "$out"
    echo "smoke: the check inside the image failed" >&2
    return 1
  }
  printf '%s\n' "$out"
  if ! grep -qx "smoke ok: $version" <<<"$out"; then
    echo "smoke: no 'smoke ok: $version' line - the check did not run (or ran against the wrong version)" >&2
    return 1
  fi
}
