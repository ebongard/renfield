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
# setuptools required for openai-whisper (uses pkg_resources)
RUN pip install --no-cache-dir --upgrade pip>=25.3 setuptools

# Install PyTorch CPU-only first (saves ~500MB vs CUDA version)
# ARM64 doesn't use +cpu suffix, x86_64 does
RUN ARCH=$(uname -m) && \
    if [ "$ARCH" = "x86_64" ]; then \
        pip install --no-cache-dir torch==2.1.2+cpu torchaudio==2.1.2+cpu \
            --index-url https://download.pytorch.org/whl/cpu; \
    else \
        pip install --no-cache-dir torch==2.1.2 torchaudio==2.1.2; \
    fi

# Install remaining Python dependencies
COPY requirements.txt ./
# Remove torch/torchaudio (already installed CPU-only) and openai-whisper (needs special handling)
# Separate GitHub-hosted MCP packages (need --no-cache-dir to always fetch latest)
RUN grep -v "^torch" requirements.txt | grep -v "^torchaudio" | grep -v "github.com" | grep -v "^openai-whisper" > requirements-filtered.txt \
    && grep "github.com" requirements.txt > requirements-github.txt || true \
    && pip install --no-cache-dir -r requirements-filtered.txt \
    && pip install --no-cache-dir --no-deps openai-whisper \
    && pip install --no-cache-dir tiktoken more-itertools \
    && if [ -s requirements-github.txt ]; then pip install --no-cache-dir --force-reinstall --no-deps -r requirements-github.txt; fi


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

# Copy Python virtual environment from builder
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
