FROM python:3.11-slim

WORKDIR /app

# System-Dependencies für Audio und OpenCV
RUN apt-get update && apt-get install -y \
    ffmpeg \
    libsndfile1 \
    portaudio19-dev \
    pkg-config \
    libavformat-dev \
    libavcodec-dev \
    libavdevice-dev \
    libavutil-dev \
    libswscale-dev \
    libswresample-dev \
    gcc \
    g++ \
    wget \
    espeak-ng \
    && rm -rf /var/lib/apt/lists/*

# Piper TTS Binary installieren (standalone - vermeidet PyAV Dependency Issues)
# Automatische Architektur-Erkennung für ARM64 und AMD64
RUN ARCH=$(uname -m) && \
    if [ "$ARCH" = "aarch64" ]; then \
        PIPER_ARCH="arm64"; \
    elif [ "$ARCH" = "x86_64" ]; then \
        PIPER_ARCH="amd64"; \
    else \
        echo "Unsupported architecture: $ARCH" && exit 1; \
    fi && \
    wget https://github.com/rhasspy/piper/releases/download/v1.2.0/piper_${PIPER_ARCH}.tar.gz \
    && tar -xzf piper_${PIPER_ARCH}.tar.gz \
    && mv piper/piper /usr/local/bin/ \
    && mv piper/lib* /usr/local/lib/ \
    && chmod +x /usr/local/bin/piper \
    && ldconfig \
    && rm -rf piper piper_${PIPER_ARCH}.tar.gz

# Piper Voice Model herunterladen (de_DE-thorsten-high)
RUN mkdir -p /usr/share/piper/voices \
    && cd /usr/share/piper/voices \
    && wget https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/thorsten/high/de_DE-thorsten-high.onnx \
    && wget https://huggingface.co/rhasspy/piper-voices/resolve/main/de/de_DE/thorsten/high/de_DE-thorsten-high.onnx.json

# Python Dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App Code
COPY . .

# Whisper Modelle vorab herunterladen (optional)
# RUN python -c "import whisper; whisper.load_model('base')"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]
