# Renfield Satellite Voice Assistant

Raspberry Pi-based satellite voice assistant for the Renfield ecosystem. Enables multi-room voice control with local wake word detection.

## Hardware Requirements

- **Raspberry Pi Zero 2 W** (or any Pi with WiFi)
- **ReSpeaker 2-Mics Pi HAT V2.0**
- MicroSD Card (16GB+)
- 5V/2A Power Supply
- 3.5mm Speaker (optional, for TTS playback)

## Features

- **Auto-discovery**: Automatically finds Renfield backend on the network (no manual URL needed)
- Local wake word detection (openwakeword with ONNX)
- WebSocket streaming to Renfield backend
- Visual feedback via RGB LEDs
- Physical button for manual control
- Auto-reconnection with re-discovery
- Stop word support (cancel ongoing interaction)
- Refractory period (prevent double triggers)
- Processing timeout recovery (auto-recovers from stuck states)
- Low CPU usage (~25% on Pi Zero 2 W)

Architecture inspired by [OHF-Voice/linux-voice-assistant](https://github.com/OHF-Voice/linux-voice-assistant).

## Quick Start

### 1. Flash Raspberry Pi OS

Use Raspberry Pi Imager to flash **Raspberry Pi OS Lite (32-bit)**.

> **Important:** Use 32-bit OS for ReSpeaker HAT compatibility. The ReSpeaker drivers do not work reliably on 64-bit Raspberry Pi OS.

Configure in advanced settings:
- Set hostname (e.g., `satellite-livingroom`)
- Enable SSH
- Configure WiFi
- Set username/password

### 2. Initial Setup

```bash
# SSH into the Pi
ssh pi@satellite-livingroom.local

# Update system
sudo apt update && sudo apt upgrade -y

# Enable SPI (for LEDs)
sudo raspi-config
# Navigate: Interface Options → SPI → Enable

sudo reboot
```

### 3. Install ReSpeaker Drivers

```bash
git clone https://github.com/HinTak/seeed-voicecard.git
cd seeed-voicecard
sudo ./install.sh
sudo reboot
```

> **Note:** We use the [HinTak fork](https://github.com/HinTak/seeed-voicecard) which has better kernel compatibility.

### 4. Verify Audio Hardware

```bash
# Check sound card
arecord -l
# Should show: card 1: seeed2micvoicec [seeed-2mic-voicecard]

# Test recording
arecord -D plughw:1,0 -f S16_LE -r 16000 -c 1 -d 5 test.wav

# Test playback
aplay -D plughw:1,0 test.wav
```

### 5. Configure ALSA Default (Critical!)

This step is **required** for PyAudio to use the ReSpeaker HAT:

```bash
cat > ~/.asoundrc << 'EOF'
pcm.!default {
    type asym
    playback.pcm "plughw:1,0"
    capture.pcm "plughw:1,0"
}
ctl.!default {
    type hw
    card 1
}
EOF
```

> **Important:** Without this configuration, PyAudio will use the wrong audio device (HDMI instead of ReSpeaker).

### 6. Install System Dependencies

```bash
# System packages
sudo apt install -y python3-pip python3-venv python3-dev \
    portaudio19-dev libasound2-dev libatlas-base-dev git \
    libopenblas0 libmpv-dev mpv

# Create installation directory
sudo mkdir -p /opt/renfield-satellite
sudo chown $USER:$USER /opt/renfield-satellite
cd /opt/renfield-satellite

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Upgrade pip
python -m pip install --upgrade pip
```

### 7. Install Python Packages

For **32-bit ARM (armv7l)** - which is required for ReSpeaker compatibility:

```bash
# Check architecture
uname -m
# Should show: armv7l

# Install onnxruntime for 32-bit ARM (unofficial build)
pip install https://github.com/nknytk/built-onnxruntime-for-raspberrypi-linux/raw/master/wheels/bullseye/onnxruntime-1.16.0-cp311-cp311-linux_armv7l.whl

# Install PyAudio (preferred for ALSA/ReSpeaker support)
pip install pyaudio

# Install other dependencies
# Note: numpy<2 is required for onnxruntime compatibility
pip install "numpy<2" openwakeword websockets python-mpv spidev RPi.GPIO pyyaml zeroconf
```

> **Source:** [nknytk/built-onnxruntime-for-raspberrypi-linux](https://github.com/nknytk/built-onnxruntime-for-raspberrypi-linux)

> **Note:** We use PyAudio instead of soundcard because PyAudio uses ALSA directly and respects the `.asoundrc` configuration. The soundcard library uses PipeWire/PulseAudio which may not detect the ReSpeaker HAT.

### 8. Download Wake Word Models

```bash
mkdir -p /opt/renfield-satellite/models
cd /opt/renfield-satellite/models

# Download openwakeword preprocessing models
wget https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/melspectrogram.onnx
wget https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/embedding_model.onnx

# Download wake word model (alexa)
wget https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/alexa_v0.1.onnx

# Copy models to openwakeword's expected location
mkdir -p /opt/renfield-satellite/venv/lib/python3.11/site-packages/openwakeword/resources/models/
cp *.onnx /opt/renfield-satellite/venv/lib/python3.11/site-packages/openwakeword/resources/models/
```

Available wake word models from [openWakeWord](https://github.com/dscripka/openWakeWord):
- `alexa_v0.1.onnx` - "Alexa" (recommended - works on 32-bit)
- `hey_mycroft_v0.1.onnx` - "Hey Mycroft"
- `hey_jarvis_v0.1.onnx` - "Hey Jarvis" (requires HuggingFace authentication)

### 9. Install Satellite Software

```bash
cd /opt/renfield-satellite

# Copy the renfield_satellite package here
# (from the main Renfield repo: renfield-satellite/renfield_satellite/)

# Or clone and install
git clone <renfield-repo-url> /tmp/renfield
cp -r /tmp/renfield/renfield-satellite/renfield_satellite .
```

### 10. Configure

```bash
mkdir -p /opt/renfield-satellite/config

cat > config/satellite.yaml << 'EOF'
satellite:
  id: "sat-livingroom"
  room: "Living Room"
  language: "de"             # Language for STT/TTS (de, en)

# Server connection - auto-discovery is enabled by default
# The satellite will automatically find the Renfield backend on your network
server:
  auto_discover: true           # Find server automatically via zeroconf
  discovery_timeout: 10         # Seconds to wait for discovery
  # url: "ws://renfield.local:8000/ws/satellite"  # Manual URL (optional)

audio:
  sample_rate: 16000
  chunk_size: 1280
  channels: 1
  device: "default"             # Uses ALSA default from .asoundrc
  playback_device: "default"

wakeword:
  model: "alexa"                # Must match downloaded model name
  threshold: 0.5
  refractory_seconds: 2.0
  # stop_words:                 # Words that cancel ongoing interaction
  #   - "stop"
  #   - "cancel"

vad:
  silence_threshold: 500
  silence_duration_ms: 1500
  max_recording_seconds: 15
EOF
```

**Note:** With auto-discovery enabled (default), the satellite will automatically find and connect to the Renfield backend on your local network. No manual URL configuration is needed. The backend advertises itself using zeroconf/mDNS (`_renfield._tcp.local.`).

### 11. Test Manual Run

```bash
cd /opt/renfield-satellite
source venv/bin/activate
python -m renfield_satellite config/satellite.yaml
```

You should see:
```
Audio backend: PyAudio (ALSA)
...
Loaded wake word: alexa (onnx)
Connected to server
Entering main loop...
Using default microphone: default (index X)
Audio capture started: default
```

> **Note:** ALSA warning messages about "Unknown PCM" devices are harmless and can be ignored.

Say "Alexa" to trigger wake word detection.

### 12. Install Systemd Service

```bash
sudo cat > /etc/systemd/system/renfield-satellite.service << 'EOF'
[Unit]
Description=Renfield Satellite Voice Assistant
After=network-online.target sound.target
Wants=network-online.target

[Service]
Type=simple
User=pi
Group=audio
WorkingDirectory=/opt/renfield-satellite
ExecStart=/opt/renfield-satellite/venv/bin/python -m renfield_satellite config/satellite.yaml
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1
SupplementaryGroups=spi gpio i2c

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable renfield-satellite
sudo systemctl start renfield-satellite
```

### 13. Check Logs

```bash
journalctl -u renfield-satellite -f
```

## Usage

### LED Patterns

| State | Pattern | Color |
|-------|---------|-------|
| Idle | Dim pulse | Blue |
| Listening | Solid | Green |
| Processing | Chase | Yellow |
| Speaking | Breathe | Cyan |
| Error | Blink | Red |

### Button

- **Single press**: Start listening (manual wake word)
- **Long press (>1s)**: Stop service

> **Note:** Button requires user to be in the `gpio` group. If you see "Failed to add edge detection", add yourself to the group: `sudo usermod -aG gpio $USER` and log out/in.

## Troubleshooting

### Wrong microphone (Built-in Audio instead of ReSpeaker)

If you see "Using default microphone: Built-in Audio Stereo" instead of the ReSpeaker:

1. **Check `.asoundrc` exists and is correct:**
   ```bash
   cat ~/.asoundrc
   ```

   It should contain:
   ```
   pcm.!default {
       type asym
       playback.pcm "plughw:1,0"
       capture.pcm "plughw:1,0"
   }
   ```

2. **Verify ReSpeaker is detected:**
   ```bash
   arecord -l
   # Should show: seeed-2mic-voicecard
   ```

3. **Make sure PyAudio is installed (not just soundcard):**
   ```bash
   pip install pyaudio
   ```

4. **Test ALSA default device:**
   ```bash
   arecord -d 3 /tmp/test.wav
   aplay /tmp/test.wav
   ```

### Garbled transcription

If the server transcribes gibberish (e.g., "Dorflock!!!" instead of your words):

1. The wrong microphone is being used (see above)
2. Sample rate mismatch - ensure ReSpeaker is configured for 16kHz
3. Test audio recording:
   ```bash
   arecord -D plughw:1,0 -f S16_LE -r 16000 -c 1 -d 5 test.wav
   aplay test.wav
   ```

### NumPy version error

If you see `A module that was compiled using NumPy 1.x cannot be run in NumPy 2.x`:

```bash
pip install "numpy<2"
```

### Missing libopenblas

If you see `libopenblas.so.0: cannot open shared object file`:

```bash
sudo apt install libopenblas0
```

### Missing libmpv

If you see `Cannot find libmpv`:

```bash
sudo apt install libmpv-dev
```

### No audio input

```bash
# Check if ReSpeaker is detected
arecord -l

# If not detected, reinstall the driver
cd ~
git clone https://github.com/HinTak/seeed-voicecard.git
cd seeed-voicecard
sudo ./install.sh
sudo reboot
```

### Auto-discovery not finding server

```bash
# Check if zeroconf is installed
pip show zeroconf

# Test mDNS discovery manually
python3 -c "
from zeroconf import Zeroconf, ServiceBrowser, ServiceListener
class L(ServiceListener):
    def add_service(self, zc, t, n): print(f'Found: {n}')
    def remove_service(self, *a): pass
    def update_service(self, *a): pass
zc = Zeroconf()
ServiceBrowser(zc, '_renfield._tcp.local.', L())
import time; time.sleep(5)
zc.close()
"

# If no server found:
# 1. Check backend is running: docker compose logs backend | grep zeroconf
# 2. Ensure ADVERTISE_HOST is set in backend .env to your server hostname
# 3. Or use manual URL in satellite config instead
```

### Connection issues

```bash
# Test server reachability
curl http://renfield.local:8000/health

# Check WebSocket endpoint
pip install websocket-client
python -c "
import websocket
ws = websocket.create_connection('ws://renfield.local:8000/ws/satellite')
print('Connected!')
ws.close()
"
```

### Wake word not detecting

1. Check model files exist:
   ```bash
   ls -la /opt/renfield-satellite/models/
   ls -la /opt/renfield-satellite/venv/lib/python3.11/site-packages/openwakeword/resources/models/
   ```

2. Verify the model name in config matches the file (without `_v0.1.onnx` suffix)

3. Try lowering threshold to 0.3

4. Check audio input is working:
   ```bash
   arecord -D plughw:1,0 -f S16_LE -r 16000 -c 1 -d 5 test.wav
   aplay test.wav
   ```

### LED not working

```bash
# Check SPI is enabled
ls /dev/spidev*
# Should show /dev/spidev0.0

# If not, enable SPI via raspi-config
sudo raspi-config
# Interface Options → SPI → Enable
```

### Button not working (GPIO edge detection failed)

This is usually a permissions issue:

```bash
# Add user to gpio group
sudo usermod -aG gpio $USER
# Logout and login again

# Or run as root for testing
sudo /opt/renfield-satellite/venv/bin/python -m renfield_satellite config/satellite.yaml
```

> **Note:** The satellite will work without button control - this is not a critical error.

### Processing timeout

If the satellite gets stuck in "processing" state:

The satellite now has automatic timeout recovery (30 seconds). If the server doesn't respond, the satellite will automatically return to idle state.

Check server logs for errors:
```bash
docker compose logs -f backend
```

## Backend Configuration

### Zeroconf Service Advertisement

The backend advertises itself on the network for satellite auto-discovery. Configure in your backend `.env`:

```bash
# Option 1: Use hostname (recommended for Docker)
ADVERTISE_HOST=renfield

# Option 2: Use specific IP
ADVERTISE_IP=192.168.1.100
```

### Wake Word Configuration

The default wake word is "alexa". To change it, set in backend `.env`:

```bash
WAKE_WORD_DEFAULT=alexa
WAKE_WORD_THRESHOLD=0.5
```

## Performance

On Raspberry Pi Zero 2 W (32-bit):

| Component | CPU | Memory |
|-----------|-----|--------|
| OpenWakeWord (ONNX) | 15-20% | ~100MB |
| Audio capture | 5% | ~10MB |
| WebSocket | 2% | ~20MB |
| LEDs | <1% | <5MB |
| **Total** | **~25%** | **~135MB** |

## Architecture Notes

### Why 32-bit OS?

The ReSpeaker 2-Mics Pi HAT drivers have compatibility issues with 64-bit Raspberry Pi OS. While the Pi Zero 2 W supports 64-bit, using 32-bit OS ensures reliable audio hardware operation.

### Why openwakeword instead of pymicro-wakeword?

- `pymicro-wakeword` includes pre-compiled TensorFlow Lite libraries that are 64-bit only
- `openwakeword` with ONNX runtime has community-built wheels for 32-bit ARM
- Both provide similar wake word detection accuracy

### Why PyAudio instead of soundcard?

- `soundcard` library uses PipeWire/PulseAudio which may not detect the ReSpeaker HAT
- `PyAudio` uses ALSA directly and respects the `.asoundrc` configuration
- This ensures the ReSpeaker microphone is used instead of HDMI audio

### Thread Safety

The satellite uses `asyncio.run_coroutine_threadsafe()` to safely schedule async operations from the audio capture thread, which runs separately from the main event loop.

## License

MIT
