# Renfield Satellite — container image (aarch64)
#
# Built for the Orange Pi Zero 3W satellite running as a node-pinned pod in the
# private k8s cluster. Hardware config = ReSpeaker XVF3800 4-Mic Array (USB):
#   - audio capture/playback via ALSA over USB  (needs /dev/snd)
#   - LED ring via the xvf_host subprocess over USB (needs /dev/bus/usb + libusb)
#   - BLE presence via bleak → host BlueZ over D-Bus (needs /run/dbus mounted)
#   - GPIO button/SPI LEDs are NOT used (import-guarded, degrade to no-op)
#
# Build natively on the Orange Pi (arm64), then import into the node containerd:
#   docker build -t renfield/satellite:esszimmer-v1 .
#   docker save renfield/satellite:esszimmer-v1 | ctr -n k8s.io images import -
FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    SATELLITE_HOME=/opt/renfield-satellite

# --- System deps ---
#  build: gcc + portaudio19-dev (pyaudio), python3-dev
#  audio runtime: libasound2(+plugins), libportaudio2, mpv/libmpv2, libsamplerate0
#  numeric: libopenblas0 (numpy/onnxruntime)
#  xvf_host: libusb-1.0-0
#  ble: bluez tooling not needed in-container (uses host BlueZ via D-Bus), but
#       dbus client libs come with python deps
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-venv python3-dev \
        gcc g++ \
        portaudio19-dev libportaudio2 \
        libasound2 libasound2-plugins alsa-utils \
        mpv libmpv2 \
        libsamplerate0 libopenblas0 \
        libopus0 \
        libusb-1.0-0 \
        bluez bluez-tools \
        ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR ${SATELLITE_HOME}

# --- Python deps (mirrors provisioning group_vars, minus Pi-only GPIO/SPI libs) ---
# GPIO libs (RPi.GPIO/lgpio/gpiozero/spidev) intentionally omitted: the xvf3800
# LED path is subprocess-only and the button degrades to a no-op without them.
RUN pip3 install --break-system-packages \
        "setuptools<81" \
        numpy \
        websockets \
        aiohttp \
        requests \
        pyyaml \
        zeroconf \
        psutil \
        pyaudio \
        python-mpv \
        webrtcvad \
        onnxruntime \
        tflite-runtime \
        pymicro-wakeword \
        pyopen-wakeword \
        noisereduce \
        scikit-learn \
        bleak \
        cryptography \
        Pillow \
        "opuslib>=3.0.1" \
        spidev \
        python-periphery \
    && pip3 install --break-system-packages --no-deps openwakeword
# spidev + python-periphery: SPI TFT display support on the A733 (sunxi gpio_backend
# drives DC/RST/BL via libgpiod/periphery, not gpiozero/BCM). No-op unless a display is
# configured + wired. See docs/design/a733-satellite-display.md.

# --- Wake word + VAD models (baked) ---
# openWakeWord base feature models + a builtin keyword, plus Silero VAD.
# NOTE: the household's custom "hey_renfield.onnx" is NOT bundled here (not in
# the repo); mount or add it before go-live to match the rest of the fleet.
RUN mkdir -p ${SATELLITE_HOME}/models \
    && OWW=https://github.com/dscripka/openWakeWord/releases/download/v0.5.1 \
    && curl -fsSL -o ${SATELLITE_HOME}/models/melspectrogram.onnx   $OWW/melspectrogram.onnx \
    && curl -fsSL -o ${SATELLITE_HOME}/models/embedding_model.onnx  $OWW/embedding_model.onnx \
    && curl -fsSL -o ${SATELLITE_HOME}/models/alexa_v0.1.onnx       $OWW/alexa_v0.1.onnx \
    && curl -fsSL -o ${SATELLITE_HOME}/models/silero_vad.onnx \
        https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad.onnx

# Household custom wake word "hey_renfield" (not in the repo — fetched from an
# existing satellite into the build context at models/hey_renfield.onnx).
COPY models/hey_renfield.onnx ${SATELLITE_HOME}/models/hey_renfield.onnx

# openWakeWord loads its base feature models (melspectrogram/embedding) from its
# OWN package resources/models dir — not models_path. Mirror the Ansible step.
RUN OWW_DIR=$(python3 -c "import openwakeword,os;print(os.path.join(os.path.dirname(openwakeword.__file__),'resources','models'))") \
    && mkdir -p "$OWW_DIR" \
    && cp ${SATELLITE_HOME}/models/melspectrogram.onnx ${SATELLITE_HOME}/models/embedding_model.onnx \
          ${SATELLITE_HOME}/models/alexa_v0.1.onnx "$OWW_DIR/"

# ALSA: make the PortAudio "default" device resolve to the XVF3800 (USB), since
# the container has no per-user ~/.asoundrc like the bare-metal satellites.
COPY config/asound.conf /etc/asound.conf

# --- XVF3800 LED control binary + libs ---
COPY hardware/xvf3800/ ${SATELLITE_HOME}/bin/
RUN chmod +x ${SATELLITE_HOME}/bin/xvf_host

# --- Application code ---
COPY renfield_satellite/ ${SATELLITE_HOME}/renfield_satellite/

# Config is provided at runtime via the mounted ConfigMap at
# ${SATELLITE_HOME}/config/satellite.yaml
# Entrypoint takes the config path POSITIONALLY (main.run reads sys.argv[1]).
# Per-deployment values (id/room/server url/ble) can also be overridden via
# RENFIELD_* env vars in the pod spec (see config.py).
ENV PYTHONPATH=${SATELLITE_HOME}
CMD ["python3", "-m", "renfield_satellite", "/opt/renfield-satellite/config/satellite.yaml"]
