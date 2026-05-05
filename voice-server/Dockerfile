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
 && rm -rf /var/lib/apt/lists/*

RUN python3.12 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app
COPY requirements.txt /app/requirements.txt

# torch is used ONLY for speechbrain's compute_features +
# mean_var_norm CPU preproc (per design § B.1.0 — ECAPA features
# run on CPU before the ONNX call). STT uses CTranslate2; ECAPA
# inference uses onnxruntime-gpu. So we install CPU torch
# (~150 MB) instead of the cu124 wheel (~5 GB) — keeps every
# layer well below Harbor's ~2.5 GB practical limit.
RUN pip install --upgrade pip wheel

RUN pip install --index-url https://download.pytorch.org/whl/cpu \
        torch>=2.4 torchaudio>=2.4

RUN pip install \
        numpy>=1.26 \
        soundfile>=0.12 \
        onnxruntime-gpu>=1.20 \
        faster-whisper>=1.1.0

RUN pip install speechbrain>=1.0

RUN pip install \
        fastapi>=0.115 \
        "uvicorn[standard]>=0.30" \
        pydantic>=2.9 pydantic-settings>=2.5 \
        "python-jose[cryptography]>=3.3" \
        "python-multipart>=0.0.18" \
        httpx>=0.27 \
        ffmpeg-python>=0.2 \
        prometheus-client>=0.20 \
        loguru>=0.7 \
        piper-tts>=1.3.0

COPY voice_server /app/voice_server
COPY scripts /app/scripts

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/health || exit 1

CMD ["uvicorn", "voice_server.main:app", "--host", "0.0.0.0", "--port", "8080", "--log-level", "info"]
