# ============================================================================
# Renfield Backend - Multi-Stage Docker Build
# ============================================================================
# Stage 1: Builder - Compiles Python packages with build dependencies
# Stage 2: Runtime - Minimal image with only runtime dependencies
# ============================================================================

# ============================================================================
# Stage 1: Builder
# ============================================================================
FROM python:3.11-slim AS builder

WORKDIR /build

# Build dependencies (not needed at runtime)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    pkg-config \
    libsndfile1-dev \
    portaudio19-dev \
    libavformat-dev \
    libavcodec-dev \
    libavdevice-dev \
    libavutil-dev \
    libswscale-dev \
    libswresample-dev \
    && rm -rf /var/lib/apt/lists/*

# Create virtual environment for cleaner copying
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Upgrade pip to fix CVE-2025-8869 (tar extraction vulnerability)
RUN pip install --no-cache-dir --upgrade pip>=25.3 setuptools

# Install Python dependencies. Split across multiple RUN layers so each pushed
# layer stays well under Harbor's external proxy timeout (the 2.66 GB monolithic
# layer reliably 504'd on the registry.treehouse.x-idra.de Telekom proxy).
# The constraints file pins torch/torchaudio/torchvision to +cpu wheels from the
# PyTorch CPU index — prevents transitive deps (docling, easyocr, transformers)
# from resolving torch to a full CUDA build (saves ~3 GB of nvidia/* wheels).
COPY requirements.txt constraints.txt ./
RUN grep -v "github.com" requirements.txt > requirements-pypi.txt \
    && (grep "github.com" requirements.txt > requirements-github.txt || true)

# Layer 1: torch ecosystem (~700 MB) — biggest single chunk; isolated so the
# rest of the deps can re-use it from the layer cache on subsequent builds.
RUN pip install --no-cache-dir \
      --extra-index-url https://download.pytorch.org/whl/cpu \
      --constraint constraints.txt \
      torch torchaudio torchvision

# Layer 2: heavy ML/CV deps (~800 MB) — transformers, easyocr, docling, speechbrain, opencv.
RUN pip install --no-cache-dir \
      --extra-index-url https://download.pytorch.org/whl/cpu \
      --constraint constraints.txt \
      transformers easyocr docling docling-core speechbrain opencv-python-headless

# Layer 3: voice/audio stack (~400 MB) — faster-whisper (ctranslate2), piper-tts, librosa.
RUN pip install --no-cache-dir \
      --constraint constraints.txt \
      faster-whisper piper-tts librosa soundfile noisereduce

# Layer 4: remaining pypi reqs (~600 MB) — fastapi, sqlalchemy, ollama, mcp, etc.
RUN pip install --no-cache-dir \
      --extra-index-url https://download.pytorch.org/whl/cpu \
      --constraint constraints.txt \
      -r requirements-pypi.txt

# Layer 5: github archive deps (small, slow to resolve — kept in its own layer).
RUN if [ -s requirements-github.txt ]; then pip install --no-cache-dir --force-reinstall --no-deps -r requirements-github.txt; fi

# Stage the heaviest packages OUTSIDE /opt/venv so the runtime stage can COPY
# them individually. A single `COPY --from=builder /opt/venv /opt/venv` would
# otherwise collapse the split RUN layers into one ~2.65 GB layer that 504s on
# the external Harbor proxy.
RUN cd /opt/venv/lib/python3.11/site-packages \
    && mkdir -p /opt/staging/torch /opt/staging/ml /opt/staging/audio \
    && mv torch torchaudio torchvision /opt/staging/torch/ \
    && mv transformers easyocr docling docling_core docling_ibm_models speechbrain cv2 /opt/staging/ml/ \
    && mv ctranslate2 librosa /opt/staging/audio/


# ============================================================================
# Stage 2: Runtime
# ============================================================================
FROM python:3.11-slim AS runtime

WORKDIR /app

# Runtime dependencies only (no compilers, no dev headers)
# ffmpeg pulls in the correct libav* versions automatically
# wget is installed temporarily for Piper downloads, then removed
# Node.js is required for MCP stdio servers (npx)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libsndfile1 \
    libportaudio2 \
    libgomp1 \
    espeak-ng \
    wget \
    curl \
    ca-certificates \
    gnupg \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Copy Python virtual environment from builder. Split into multiple COPY layers
# so each pushed layer fits under the external Harbor proxy timeout. The heavy
# packages were moved out of /opt/venv in the builder; here we copy them back
# into site-packages individually before the catch-all COPY of the slim venv.
COPY --from=builder /opt/staging/torch/. /opt/venv/lib/python3.11/site-packages/
COPY --from=builder /opt/staging/ml/. /opt/venv/lib/python3.11/site-packages/
COPY --from=builder /opt/staging/audio/. /opt/venv/lib/python3.11/site-packages/
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1

# Piper TTS Binary (architecture-aware)
RUN ARCH=$(uname -m) && \
    if [ "$ARCH" = "aarch64" ]; then \
        PIPER_ARCH="arm64"; \
    elif [ "$ARCH" = "x86_64" ]; then \
        PIPER_ARCH="amd64"; \
    else \
        echo "Unsupported architecture: $ARCH" && exit 1; \
    fi && \
    wget -q https://github.com/rhasspy/piper/releases/download/v1.2.0/piper_${PIPER_ARCH}.tar.gz \
    && tar -xzf piper_${PIPER_ARCH}.tar.gz \
    && mv piper/piper /usr/local/bin/ \
    && mv piper/lib* /usr/local/lib/ 2>/dev/null || true \
    && chmod +x /usr/local/bin/piper \
    && rm -f /usr/lib/x86_64-linux-gnu/libespeak-ng.so.1* \
    && ln -sf /usr/lib/x86_64-linux-gnu/espeak-ng-data /usr/share/espeak-ng-data \
    && ldconfig \
    && rm -rf piper piper_${PIPER_ARCH}.tar.gz

# Piper Voice Model (German - thorsten high quality)
RUN mkdir -p /usr/share/piper/voices \
    && cd /usr/share/piper/voices \
    && wget -q https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/thorsten/high/de_DE-thorsten-high.onnx \
    && wget -q https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/thorsten/high/de_DE-thorsten-high.onnx.json

# Remove wget after downloads complete (security hardening)
RUN apt-get purge -y wget && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

# Pre-install MCP stdio server packages (avoids npx download delay on first use)
RUN npm install -g n8n-mcp@latest

# Application code
COPY . .

# Wake word models — TFLite/ONNX files served to satellites via
# /api/settings/wakeword/models/{id}. On docker-compose these were bind-mounted
# from ./data/wakeword-models; in the image build context they must sit next to
# the backend sources (synced from the repo's data/wakeword-models/ at build time).
COPY wakeword-models /app/wakeword-models

# Create non-root user for security
RUN useradd --create-home --shell /bin/bash appuser \
    && chown -R appuser:appuser /app
# Note: Running as root for now due to volume mount permissions
# USER appuser

# Health check (using Python's built-in urllib, no external tools needed)
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health')"

EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
