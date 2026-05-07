"""B.5 spike — predownload XTTS-v2 weights to the HF cache PVC.

Run once after the spike pod comes up (or as a postStart hook) so the
first benchmark request doesn't pay the ~3-5 min download cost. The
script is idempotent: subsequent runs see a cached snapshot and exit
fast.

Storage: lands at $HF_HUB_CACHE/hub/models--coqui--XTTS-v2/ — the
voice-server-hf-cache Longhorn PVC, same volume that holds whisper-medium.
~1.8 GB on disk after extraction.

License: XTTS-v2 is CPML 1.0.0 (non-commercial use only). The Dockerfile
sets COQUI_TOS_AGREED=1 to pre-accept on Coqui-TTS' behalf in
non-interactive contexts. See docs/B5_LICENSE_NOTE.md for the full
license note and the rationale for accepting under the testing/evaluation
clause.

Usage:
    python -m scripts.predownload_xtts
or from inside the spike pod:
    kubectl exec deploy/voice-server-spike -- python /app/scripts/predownload_xtts.py
"""

from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("predownload_xtts")

XTTS_REPO_ID = "coqui/XTTS-v2"

# These four files are the minimum XTTS-v2 needs for inference. Skipping
# the README/samples/training-state cuts ~50 MB off the snapshot.
XTTS_REQUIRED_FILES = [
    "model.pth",
    "config.json",
    "vocab.json",
    "speakers_xtts.pth",  # Default-speaker embeddings
]


def main() -> int:
    cache_root = os.environ.get("HF_HUB_CACHE", "/cache/huggingface")
    cache_path = Path(cache_root)

    if not cache_path.parent.exists():
        log.error("HF cache parent %s does not exist — PVC mount missing?", cache_path.parent)
        return 2

    cache_path.mkdir(parents=True, exist_ok=True)
    log.info("HF_HUB_CACHE=%s", cache_root)

    # Lazy import — keeps the script importable for tooling that just
    # wants to inspect the constants above.
    try:
        from huggingface_hub import snapshot_download
    except ImportError as e:
        log.error("huggingface_hub not installed: %s", e)
        return 3

    log.info("Downloading %s required files (%s)...", XTTS_REPO_ID, XTTS_REQUIRED_FILES)
    started = time.monotonic()

    snapshot_path = snapshot_download(
        repo_id=XTTS_REPO_ID,
        cache_dir=cache_root,
        allow_patterns=XTTS_REQUIRED_FILES,
    )

    elapsed = time.monotonic() - started
    log.info("Snapshot at %s (took %.1f s)", snapshot_path, elapsed)

    snapshot = Path(snapshot_path)
    missing = [f for f in XTTS_REQUIRED_FILES if not (snapshot / f).is_file()]
    if missing:
        log.error("Missing files after download: %s", missing)
        return 4

    sizes_mb = {f: (snapshot / f).stat().st_size / 1024 / 1024 for f in XTTS_REQUIRED_FILES}
    log.info("File sizes: %s", {k: f"{v:.1f} MB" for k, v in sizes_mb.items()})

    return 0


if __name__ == "__main__":
    sys.exit(main())
