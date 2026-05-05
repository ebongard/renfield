# syntax=docker/dockerfile:1.6
#
# Renfield voice-server — CUDA 13.0 / Ubuntu 24.04 base.
#
# Multi-RUN pip-install layer split is intentional: Harbor's proxy times
# out on >2.5 GB layers (same pattern as the backend image). torch +
# faster-whisper land in their own layer; the rest in another.

FROM nvidia/cuda:13.0.0-runtime-ubuntu24.04 AS runtime

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.12 python3.12-venv python3-pip \
        ffmpeg ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

RUN python3.12 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /app
COPY requirements.txt /app/requirements.txt

# Layer 1: heavy ML deps (torch, faster-whisper, onnxruntime-gpu, speechbrain)
RUN pip install --upgrade pip wheel \
 && pip install --extra-index-url https://download.pytorch.org/whl/cu124 \
        torch>=2.4 torchaudio>=2.4 \
 && pip install \
        faster-whisper>=1.1.0 \
        onnxruntime-gpu>=1.20 \
        speechbrain>=1.0

# Layer 2: web framework + light deps
RUN pip install \
        fastapi>=0.115 \
        "uvicorn[standard]>=0.30" \
        pydantic>=2.9 pydantic-settings>=2.5 \
        "python-jose[cryptography]>=3.3" \
        httpx>=0.27 \
        ffmpeg-python>=0.2 \
        prometheus-client>=0.20 \
        loguru>=0.7 \
        numpy>=1.26 \
        soundfile>=0.12 \
        piper-tts>=1.3.0

COPY voice_server /app/voice_server
COPY scripts /app/scripts

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/health || exit 1

CMD ["uvicorn", "voice_server.main:app", "--host", "0.0.0.0", "--port", "8080", "--log-level", "info"]
