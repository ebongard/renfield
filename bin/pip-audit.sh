#!/usr/bin/env bash
#
# Supply-chain audit (#684) — run pip-audit against a BUILT backend image (what
# actually ships), on the .159 build box.
#
# Posture (chosen 2026-08-24): the backend deliberately keeps >=-version ranges
# for its PyPI deps — a rebuild floats them up to the latest fix (verified: the
# 2026-08-24 image was already on the patched pyjwt/pillow/starlette/multipart/
# etc.). The mutable-git-ref supply-chain hole from #684 is separately closed:
# the renfield-mcp-* packages are pinned to immutable archive/<sha> tarballs
# (requirements.txt). The full `pip-compile --generate-hashes` lockfile was
# deliberately NOT adopted — it would freeze versions and forfeit the auto-
# patching, a poor trade for a self-hosted LAN deploy with heavy ML deps.
# THIS SCRIPT is the safety net that makes the ranges honest: it catches when a
# build lands on a version carrying a KNOWN advisory, so the operator reviews
# before shipping. Run it as part of every release. See docs/SECURITY.md.
#
# Usage:
#   bin/pip-audit.sh                 # audit the current :latest backend image
#   bin/pip-audit.sh <image-ref>     # audit a specific built image tag
#
# Exits non-zero if any NON-accepted advisory is found.
set -euo pipefail

BUILD_HOST="${RENFIELD_BUILD_HOST:-evdb@192.168.1.159}"
REGISTRY="${RENFIELD_REGISTRY:-registry.treehouse.x-idra.de/renfield}"
IMAGE="${1:-$REGISTRY/backend:latest}"

# --- Accepted advisories (reviewed, no real exposure) -----------------------
# The gate ignores these so it only alarms on something NEW. Re-review each on
# every dependency bump / whenever the constraint that blocks the fix changes.
#
#   transformers 4.57.x (PYSEC-2025-217, PYSEC-2026-2288/2289/2290): the fixes
#     ship only in transformers 5.x, but `transformers<5` is pinned in
#     requirements.txt (5.x needs torch>=2.7; prod CPU torch is 2.6). Renfield
#     loads its OWN models (docling / rt_detr_v2), never a user-supplied model
#     file, so the model-deserialization attack surface is not reachable. Drop
#     these ignores once the torch/transformers floor can move.
#   transformers 4.57.x (PYSEC-2026-3929 / CVE-2026-9856, reviewed 2026-09-14):
#     path traversal in PreTrainedTokenizerBase/ProcessorMixin.save_pretrained()
#     — chat_template dict keys become filenames. Fixed only in 5.10.0 (commit
#     eaaaf849, no 4.x backport; 4.57.6 is the last 4.x) → blocked by the same
#     `transformers<5` pin. Not reachable: the sink is SAVING a tokenizer or
#     processor, and nothing in the image calls save_pretrained() on one —
#     renfield/docling/docling-ibm-models/easyocr/speechbrain have zero
#     save_pretrained/save_chat_templates callers (only onnxruntime's unimported
#     offline tooling scripts do). Lift together with the ignores above.
#   accelerate 1.14.0 (PYSEC-2026-3804 / CVE-2026-69112, reviewed 2026-09-14):
#     path traversal / DoS via sharded-checkpoint weight_map entries in
#     load_checkpoint_in_model / load_checkpoint_and_dispatch. NO patched
#     release exists: OSV/GHSA list "<=1.14.0" with no fixed version, the fix
#     PRs (huggingface/accelerate#4070, #4138) are unmerged, and 1.15.0's
#     utils/modeling.py still joins weight_map entries unchecked — so bumping
#     to 1.15.0 would only silence the scanner, not fix anything. Not reachable:
#     accelerate is only pulled in via docling's extras; renfield never imports
#     it, no package outside accelerate/transformers imports it, and transformers
#     4.57.6 never calls either sink. Local-vector (AV:L) and needs an attacker
#     crafted checkpoint dir; renfield only loads docling's own model artifacts.
#     Lift once a release containing the fix ships (it then floats in on rebuild).
#   ecdsa 0.19.x (PYSEC-2026-1325): renfield signs+verifies JWTs with HS256
#     (HMAC) only — auth_service.ALGORITHM = "HS256" — so the ECDSA side-channel
#     is never exercised. ecdsa is a transitive dep of python-jose.
IGNORE="--ignore-vuln PYSEC-2025-217 --ignore-vuln PYSEC-2026-2288 --ignore-vuln PYSEC-2026-2289 --ignore-vuln PYSEC-2026-2290 --ignore-vuln PYSEC-2026-3929 --ignore-vuln PYSEC-2026-3804 --ignore-vuln PYSEC-2026-1325"

echo "pip-audit → $IMAGE (ignoring reviewed-accepted advisories; see script header)"
# The local github-archive MCP packages and the +cpu torch wheels are not on
# PyPI; pip-audit reports them as "skipped", which is not a failure.
ssh "$BUILD_HOST" "docker run --rm --entrypoint sh '$IMAGE' -c 'pip install -q pip-audit && pip-audit --progress-spinner off $IGNORE'"
