# syntax=docker/dockerfile:1.6
#
# Renfield voice-server — CUDA 13.0 / Ubuntu 24.04 base.
#
# Multi-RUN pip-install layer split is intentional: Harbor's proxy times
# out on >2.5 GB layers (same pattern as the backend image). torch +
# faster-whisper land in their own layer; the rest in another.

# CUDA 12.6 chosen to match the Speaches container we deployed successfully
# on k8s-gpu-3 (RTX 4060 Ti, sm_89). The host driver (580.142) supports both
# 12.x and 13.x runtimes via forward-compat, but pytorch's prebuilt wheels
# track 12.x, so 12.6 is the path with the fewest moving parts.
FROM nvidia/cuda:12.6.3-runtime-ubuntu24.04 AS runtime

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# nvidia/cuda:12.6.3-runtime-ubuntu24.04 ships with a libgcrypt/gnupg
# combination that rejects the current Ubuntu noble apt InRelease
# signatures ("At least one invalid signature"). All Ubuntu repos +
# the bundled cuda repo fail the same way. Bypass: accept insecure
# repos AND skip authentication on install. Acceptable because the
# base image is pulled from Docker Hub via the nvidia/cuda
# digest-pinned reference; the apt install of well-known Ubuntu
# packages over HTTPS is a much smaller risk surface than tampering
# with the base image we already trust.
RUN apt-get -o Acquire::AllowInsecureRepositories=true \
            -o Acquire::AllowDowngradeToInsecureRepositories=true \
            update \
 && apt-get install -y --allow-unauthenticated --no-install-recommends \
        python3.12 python3.12-venv python3-pip \
        ffmpeg ca-certificates curl \
        libopus0 \
 && rm -rf /var/lib/apt/lists/*

RUN python3.12 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# onnxruntime-gpu's CUDAExecutionProvider dlopens libcudart + libcublas
# from /usr/local/cuda/lib64 (base image) and libcudnn from the
# nvidia-cudnn-cu12 pip wheel. The pip wheel installs at
# /opt/venv/lib/python3.12/site-packages/nvidia/cudnn/lib — onnxruntime
# adds that automatically via its preload code, but we set
# LD_LIBRARY_PATH explicitly so any other consumer (and ldconfig) sees
# the same view.
ENV LD_LIBRARY_PATH=/usr/local/cuda/lib64:/opt/venv/lib/python3.12/site-packages/nvidia/cudnn/lib:${LD_LIBRARY_PATH:-}

WORKDIR /app
COPY requirements.txt /app/requirements.txt

RUN pip install --upgrade pip wheel

# GPU torch (cu128). Historically this was CPU-only torch (~150 MB) because torch
# served only speechbrain's CPU ECAPA preproc. §2 meeting diarization runs
# pyannote's neural pipeline ON THE GPU, which needs GPU torch. cu128 runs on
# both the gpu-3 Ada card (sm_89) and Blackwell (sm_120) — one pyannote
# integration for §2 + the planned streaming-diarization feature. This is a
# ~6 GB layer (over Harbor's ~2.5 GB comfort limit, same as the backend image's
# large layers — the push is slow but works); kept in its OWN layer.
RUN pip install --index-url https://download.pytorch.org/whl/cu128 \
        torch==2.7.1 torchaudio==2.7.1

RUN pip install \
        numpy>=1.26 \
        soundfile>=0.12 \
        faster-whisper>=1.1.0

# speechbrain pulls in plain `onnxruntime` (CPU) as a transitive. Install
# it BEFORE onnxruntime-gpu so the GPU wheel wins the final import path.
RUN pip install speechbrain>=1.0

# pyannote.audio for §2 meeting diarization. Pin huggingface_hub<0.26: pyannote
# 3.x internally calls hf_hub_download(use_auth_token=...) which hub 0.26+
# removed (validated in the spike). Own layer, BEFORE the final onnxruntime-gpu
# cleanup because pyannote transitively pulls CPU onnxruntime.
RUN pip install "pyannote.audio>=3.1,<4" "huggingface_hub<0.26"

# Bake the gated pyannote diarization model into the image (offline-first). Uses
# a BuildKit secret so the HF token never lands in image history. Skipped (with
# a loud echo) when no secret is passed — the runtime then downloads on warmup
# if MEETING_ENABLED + HF_TOKEN are set and the cache is cold.
ENV HF_HOME=/opt/hf-cache
RUN --mount=type=secret,id=hf_token \
    HF_TOKEN="$(cat /run/secrets/hf_token 2>/dev/null || true)"; \
    if [ -n "$HF_TOKEN" ]; then \
      python - "$HF_TOKEN" <<'PY' || echo "[bake] pyannote model bake FAILED — will download on warmup"; \
import sys, torch
_o = torch.load
torch.load = lambda *a, **k: _o(*a, **{**k, "weights_only": False})
from pyannote.audio import Pipeline
p = Pipeline.from_pretrained("pyannote/speaker-diarization-3.1", use_auth_token=sys.argv[1])
print("[bake] pyannote model cached:", p is not None)
PY
    else echo "[bake] no hf_token build secret — pyannote model not baked"; fi

# cuDNN is required by onnxruntime-gpu's CUDAExecutionProvider but is
# NOT included in nvidia/cuda:12.6-runtime base. Pull it via pip wheel.
# Pin >=9.1 — onnxruntime-gpu>=1.20 dlopens libcudnn.so.9; an
# unconstrained install today resolves to 9.x but a future resolver
# could downgrade to 8.x and silently break GPU dispatch.
RUN pip install "nvidia-cudnn-cu12>=9.1"

# Web framework + light deps — including piper-tts. piper-tts depends
# on `onnxruntime` (CPU). Install it BEFORE the final onnxruntime-gpu
# cleanup, otherwise piper's transitive overwrites our GPU build.
RUN pip install \
        fastapi>=0.115 \
        "uvicorn[standard]>=0.30" \
        pydantic>=2.9 pydantic-settings>=2.5 \
        "python-jose[cryptography]>=3.3" \
        "python-multipart>=0.0.18" \
        httpx>=0.27 \
        ffmpeg-python>=0.2 \
        opuslib>=3.0.1 \
        prometheus-client>=0.20 \
        loguru>=0.7 \
        piper-tts>=1.3.0

# ABSOLUTE LAST pip step. faster-whisper, speechbrain, and piper-tts
# all transitively pull `onnxruntime` (CPU) which shares the import
# path with onnxruntime-gpu. Co-installed, the CUDA EP gets masked
# at import time and only Azure+CPU providers show up. Empirically the
# only reliable fix is to nuke the onnxruntime/* directory and the
# onnxruntime{,_gpu}-*.dist-info dirs (pip uninstall is conservative
# and leaves dist-info in place when files are co-owned), then do a
# clean onnxruntime-gpu install. After this, get_available_providers
# includes CUDAExecutionProvider. Verified by speaker_service warmup
# log on every boot.
# NOTE: this approach is load-bearing on the two-package split between
# `onnxruntime` (CPU) and `onnxruntime-gpu` on PyPI, with the dist-info
# names `onnxruntime-X.Y.Z.dist-info` and `onnxruntime_gpu-X.Y.Z.dist-info`.
# If onnxruntime ever consolidates into a single auto-detect package (under
# discussion upstream), this Dockerfile breaks and the right fix becomes
# pinning the GPU package via a pip --constraint file rather than nuke-and-
# reinstall. Adjust the globs below if the dist-info naming changes.
RUN rm -rf /opt/venv/lib/python3.12/site-packages/onnxruntime \
           /opt/venv/lib/python3.12/site-packages/onnxruntime-*.dist-info \
           /opt/venv/lib/python3.12/site-packages/onnxruntime_gpu-*.dist-info \
 && pip install --no-cache-dir "onnxruntime-gpu>=1.20,<1.27"
# ^ Cap <1.27: 1.27.0 moved the PyPI wheel to CUDA 13 (libcudart.so.13); the
#   base is nvidia/cuda:12.6.3 (libcudart.so.12). Keep both this reinstall and
#   requirements.txt on the CUDA-12 line (1.26.x) or warmup CrashLoops.

COPY voice_server /app/voice_server
COPY scripts /app/scripts

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/health || exit 1

CMD ["uvicorn", "voice_server.main:app", "--host", "0.0.0.0", "--port", "8080", "--log-level", "info"]
