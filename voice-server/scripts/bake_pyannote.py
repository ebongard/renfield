"""Build-time bake of the gated pyannote diarization model (§2, offline-first).

Run in the Dockerfile with the HF token in the HF_TOKEN env (from a BuildKit
secret). No token → skip cleanly (exit 0); the runtime then downloads on warmup
via HF_TOKEN when MEETING_ENABLED. Kept as a script (not a heredoc in a RUN) so
the Dockerfile has no line-continuation parse hazard.
"""
import os
import sys

token = os.environ.get("HF_TOKEN")
if not token:
    print("[bake] no HF_TOKEN — pyannote model NOT baked (downloads on warmup)")
    sys.exit(0)

import torch  # noqa: E402

# torch 2.6+ defaults torch.load to weights_only=True; the pyannote checkpoint
# carries non-tensor globals. Force False — trusted official model.
_orig = torch.load
torch.load = lambda *a, **k: _orig(*a, **{**k, "weights_only": False})

from pyannote.audio import Pipeline  # noqa: E402

try:
    pipeline = Pipeline.from_pretrained(
        "pyannote/speaker-diarization-3.1", use_auth_token=token
    )
except TypeError:
    pipeline = Pipeline.from_pretrained(
        "pyannote/speaker-diarization-3.1", token=token
    )
print("[bake] pyannote model cached:", pipeline is not None)
