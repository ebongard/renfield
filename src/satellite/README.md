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
- **OTA Updates**: Over-the-air software updates via Web-UI (see [docs/SATELLITE_OTA_UPDATES.md](../../docs/SATELLITE_OTA_UPDATES.md))
- Local wake word detection (openwakeword with ONNX)
- WebSocket streaming to Renfield backend
- Visual feedback via RGB LEDs
- Physical button for manual control
- Auto-reconnection with re-discovery
- Stop word support (cancel ongoing interaction)
- Refractory period (prevent double triggers)
- Processing timeout recovery (auto-recovers from stuck states)
- Low CPU usage (~25% on Pi Zero 2 W)
- **Beamforming** (optional): Delay-and-Sum beamforming with stereo microphones for improved noise rejection

Architecture inspired by [OHF-Voice/linux-voice-assistant](https://github.com/OHF-Voice/linux-voice-assistant).

## Quick Start

### 1. Flash Raspberry Pi OS

Use Raspberry Pi Imager to flash **Raspberry Pi OS Lite (64-bit)**.

> **Note:** 64-bit OS is now recommended for the Pi Zero 2 W. It enables ONNX Runtime for Silero VAD and better performance. The ReSpeaker HAT requires a custom MCLK overlay (see step 3).

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

The Renfield repo includes a custom DTS overlay and install script for the ReSpeaker 2-Mics HAT on 64-bit OS.

```bash
# Copy hardware files from the Renfield repo to the satellite
rsync -avz src/satellite/hardware/ user@satellite.local:/tmp/renfield-hardware/

# Compile and install the overlay
ssh user@satellite.local "sudo /tmp/renfield-hardware/install-overlay.sh simple"

# Disable onboard audio (optional, prevents confusion with HDMI audio)
ssh user@satellite.local "sudo sed -i 's/^dtparam=audio=on/dtparam=audio=off/' /boot/firmware/config.txt"

sudo reboot
```

> **Important: GPIO4 Conflict!** The GPCLK0 overlay uses GPIO4 for the MCLK signal. If you have `dtoverlay=w1-gpio` (1-Wire) enabled in `/boot/firmware/config.txt`, you **must** disable it — it uses GPIO4 by default and will prevent the ReSpeaker from initializing. Comment it out:
> ```bash
> sudo sed -i 's/^dtoverlay=w1-gpio/#dtoverlay=w1-gpio  # conflicts with GPCLK0 on GPIO4/' /boot/firmware/config.txt
> ```

After reboot, verify the sound card and MCLK:
```bash
# Should show: seeed2micvoicec
cat /proc/asound/cards

# Should show: gp0 ... 12288000
cat /sys/kernel/debug/clk/clk_summary | grep gp0
```

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
# Copy from the Renfield repo
scp src/satellite/config/asoundrc user@satellite.local:~/.asoundrc
```

Or create manually — see `src/satellite/config/asoundrc` in the repo for the full content.

> **Important:** Without this configuration, PyAudio will use the wrong audio device (HDMI instead of ReSpeaker).

**Note:** The `array16k` device is required for beamforming with stereo capture at 16kHz sample rate.

### 6. Install System Dependencies

```bash
# System packages
sudo apt install -y python3-pip python3-venv python3-dev \
    portaudio19-dev libasound2-dev libopenblas0 \
    libmpv-dev mpv libsamplerate0 \
    swig liblgpio-dev

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

> **Note (Debian Trixie / Bookworm):** The package `libatlas-base-dev` has been removed. Use `liblapack-dev` if you need LAPACK/BLAS headers. The `libopenblas0` package provides the runtime library.

> **Note:** `swig` and `liblgpio-dev` are required to build the `lgpio` Python package from source.

### 7. Install Python Packages

For **64-bit ARM (aarch64)** on Pi Zero 2 W:

```bash
# Check architecture
uname -m
# Should show: aarch64

# Install all dependencies
pip install onnxruntime pyaudio numpy websockets python-mpv \
    spidev lgpio pyyaml zeroconf webrtcvad psutil scikit-learn

# Install openwakeword (may need --no-deps on Python 3.13+, see note below)
pip install openwakeword
```

> **Note:** We use PyAudio instead of soundcard because PyAudio uses ALSA directly and respects the `.asoundrc` configuration. The soundcard library uses PipeWire/PulseAudio which may not detect the ReSpeaker HAT.

> **Note:** On 64-bit OS, use `lgpio` instead of `RPi.GPIO` for GPIO control (button).

> **Python 3.13+ Compatibility:** The `openwakeword` package declares `tflite-runtime` as a dependency, but `tflite-runtime` has no wheels for Python 3.13. Since we use ONNX (not TFLite), install with `--no-deps` and then install the actual dependencies manually:
> ```bash
> pip install --no-deps openwakeword
> pip install requests  # required by openwakeword
> ```

### 8. Download Wake Word Models

```bash
mkdir -p /opt/renfield-satellite/models
cd /opt/renfield-satellite/models

# Download openwakeword preprocessing models
wget https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/melspectrogram.onnx
wget https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/embedding_model.onnx

# Download wake word model (alexa)
wget https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/alexa_v0.1.onnx

# Download Silero VAD model
wget -O silero_vad.onnx https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad.onnx

# Copy models to openwakeword's expected location (use correct Python version)
PYVER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
OWW_DIR=/opt/renfield-satellite/venv/lib/python${PYVER}/site-packages/openwakeword/resources/models
mkdir -p $OWW_DIR
cp *.onnx $OWW_DIR/
```

> **Important:** When upgrading the `openwakeword` package, the `resources/models/` directory is deleted. You must re-copy the ONNX models after any openwakeword upgrade.

Available wake word models from [openWakeWord](https://github.com/dscripka/openWakeWord):
- `alexa_v0.1.onnx` - "Alexa" (recommended - works on 32-bit)
- `hey_mycroft_v0.1.onnx` - "Hey Mycroft"
- `hey_jarvis_v0.1.onnx` - "Hey Jarvis" (requires HuggingFace authentication)

### 9. Install Satellite Software

```bash
# Deploy from the Renfield repo using rsync
rsync -avz src/satellite/renfield_satellite/ \
    user@satellite.local:/opt/renfield-satellite/renfield_satellite/

# Or use the deploy script
./bin/deploy-satellite.sh satellite.local user
```

### 10. Configure

```bash
# Copy the example config from the repo
scp src/satellite/config/satellite.yaml \
    user@satellite.local:/opt/renfield-satellite/config/satellite.yaml

# Edit satellite ID and room name
ssh user@satellite.local "sed -i 's/sat-livingroom/sat-kitchen/; s/Living Room/Kitchen/' \
    /opt/renfield-satellite/config/satellite.yaml"
```

See `src/satellite/config/satellite.yaml` in the repo for all configuration options including audio, wake word, VAD, LED, and button settings.

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
# Copy the service file from the repo
scp src/satellite/systemd/renfield-satellite.service \
    user@satellite.local:/tmp/renfield-satellite.service
ssh user@satellite.local "sudo cp /tmp/renfield-satellite.service /etc/systemd/system/"

# Enable and start
ssh user@satellite.local "sudo systemctl daemon-reload && \
    sudo systemctl enable renfield-satellite && \
    sudo systemctl start renfield-satellite"
```

> **Note:** The service file uses `User=pi` by default. Edit it if your satellite user is different.

### 13. Check Logs

```bash
journalctl -u renfield-satellite -f
```

## Monitoring & Debugging

### Web Dashboard

When the satellite is running and connected, you can monitor it via the Renfield web interface:

1. Navigate to **Admin → Satellites** in the web UI
2. View real-time metrics:
   - Connection status and uptime
   - Audio levels (RMS, dB) with visual bars
   - Voice Activity Detection (VAD) status
   - CPU, memory, and temperature
   - Wake word detection history
   - Session statistics

The dashboard auto-refreshes every 5 seconds.

### CLI Monitor Tool

For debugging audio setup **before** starting the satellite service, use the CLI monitor:

```bash
# Stop the satellite service first (it holds the audio device exclusively)
sudo systemctl stop renfield-satellite

# Run the monitor
cd /opt/renfield-satellite
source venv/bin/activate
python -m renfield_satellite.cli.monitor
```

The monitor displays:
```
╔══════════════════════════════════════════════════════════════════════════════╗
║                         RENFIELD SATELLITE MONITOR                          ║
╚══════════════════════════════════════════════════════════════════════════════╝

 CONNECTION
 ────────────────────────────────────────
 Status: ○ STANDALONE
 State:  MONITORING

 AUDIO LEVELS
 ────────────────────────────────────────
 RMS:  [███████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░] 1433
 dB:   [███████████████████████████████████░░░░░░░░░░░░░░░] -27.2 dB
 VAD:  SPEECH

 SYSTEM
 ────────────────────────────────────────
 CPU:    3.7%
 Mem:   36.6%
 Temp:  54.8°C
```

Press `Ctrl+C` or `q` to quit.

> **Note:** The CLI monitor requires `psutil` for system metrics. Install with:
> ```bash
> /opt/renfield-satellite/venv/bin/pip install psutil
> ```

### When to Use Which Tool

| Scenario | Tool |
|----------|------|
| Satellite running, check live status | Web Dashboard |
| Debug audio/microphone setup | CLI Monitor (service stopped) |
| Verify ReSpeaker configuration | CLI Monitor |
| Monitor multiple satellites | Web Dashboard |

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
   # Should contain pcm.!default with capture.pcm "capture"
   # See src/satellite/config/asoundrc in the repo for the correct content
   ```

2. **Verify ReSpeaker is detected:**
   ```bash
   cat /proc/asound/cards
   # Should show: seeed2micvoicec
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

### GPIO4 conflict (ReSpeaker not initializing)

If `cat /proc/asound/cards` does not show `seeed2micvoicec` and `dmesg` shows:
```
pinctrl-bcm2835: error -EINVAL: pin-4 ... could not request pin 4
wm8960 1-001a: Error applying setting, reverse things back
```

This means GPIO4 is already in use by another overlay (typically `w1-gpio` for 1-Wire sensors). The ReSpeaker GPCLK0 overlay needs GPIO4 for the MCLK signal. Disable the conflicting overlay:

```bash
sudo sed -i 's/^dtoverlay=w1-gpio/#dtoverlay=w1-gpio/' /boot/firmware/config.txt
sudo reboot
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

### lgpio build fails

If `pip install lgpio` fails with `error: command 'swig' failed` or `cannot find -llgpio`:

```bash
# Install build dependencies
sudo apt install -y swig liblgpio-dev
# Then retry
pip install lgpio
```

### No audio input

```bash
# Check if ReSpeaker is detected
cat /proc/asound/cards
# Should show: seeed2micvoicec

# If not detected, re-run the overlay installer (see step 3)
# Check for GPIO4 conflicts (see "GPIO4 conflict" above)
# Verify /boot/firmware/config.txt contains: dtoverlay=seeed-2mic-gpclk-simple
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

1. Check model files exist in **both** locations:
   ```bash
   ls -la /opt/renfield-satellite/models/
   # Must contain: melspectrogram.onnx, embedding_model.onnx, alexa_v0.1.onnx

   PYVER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
   ls -la /opt/renfield-satellite/venv/lib/python${PYVER}/site-packages/openwakeword/resources/models/
   # Must also contain the same .onnx files (openwakeword loads from here)
   ```

2. Verify the model name in config matches the file (without `_v0.1.onnx` suffix)

3. Try lowering threshold to 0.3

4. Check audio input is working:
   ```bash
   arecord -d 3 /tmp/test.wav
   aplay /tmp/test.wav
   ```

5. If you see `AudioFeatures.__init__() got an unexpected keyword argument 'wakeword_models'`, you have openwakeword < 0.5.0. Upgrade: `pip install --no-deps 'openwakeword>=0.5.1'`

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

On Raspberry Pi Zero 2 W (64-bit):

| Component | CPU | Memory |
|-----------|-----|--------|
| Wake Word (openwakeword/ONNX) | 10-15% | ~80MB |
| Silero VAD (ONNX) | 3-5% | ~30MB |
| Audio capture | 5% | ~10MB |
| Beamforming (optional) | 5-7% | ~5MB |
| WebSocket | 2% | ~20MB |
| LEDs | <1% | <5MB |
| **Total (without beamforming)** | **~25%** | **~145MB** |
| **Total (with beamforming)** | **~30%** | **~150MB** |

## Beamforming (Optional)

The ReSpeaker 2-Mics Pi HAT has two microphones spaced 58mm apart. This enables **Delay-and-Sum (DAS) beamforming** for improved noise rejection.

### Benefits

- **3-6 dB SNR improvement** for noise from the sides
- **Better speech recognition** in noisy environments
- **Effective frequency range**: 600 Hz - 3000 Hz (ideal for speech)

### How It Works

1. Captures stereo audio (left and right microphone)
2. Calculates time delay based on steering angle
3. Aligns signals by shifting one channel
4. Sums aligned signals (constructive interference from target direction)
5. Outputs enhanced mono audio

### Configuration

To enable beamforming, update your `satellite.yaml`:

```yaml
audio:
  sample_rate: 16000
  chunk_size: 1280
  channels: 2                   # Required for beamforming
  device: "array16k"            # Stereo device with rate conversion
  playback_device: "plughw:0,0"
  beamforming:
    enabled: true
    mic_spacing: 0.058          # ReSpeaker 2-Mics: 58mm
    steering_angle: 0.0         # 0 = front-facing, 90 = right, -90 = left
```

**Requirements:**
- `.asoundrc` must include the `array16k` device (see step 5)
- `channels: 2` for stereo capture
- `device: "array16k"` for resampled stereo at 16kHz

### Performance

On Pi Zero 2 W, beamforming adds approximately 5-7% CPU overhead. This is acceptable given the noise rejection benefits.

## Architecture Notes

### Why 64-bit OS?

64-bit Raspberry Pi OS is now recommended for the Pi Zero 2 W:
- **ONNX Runtime**: Official wheels available for aarch64 (no 32-bit builds)
- **Silero VAD**: Requires ONNX Runtime for efficient voice activity detection
- **Better performance**: 64-bit operations are native on the ARM Cortex-A53

The ReSpeaker 2-Mics HAT works on 64-bit OS with a custom MCLK overlay (see step 3).

### Why openwakeword instead of pymicro-wakeword?

- `openwakeword` with ONNX Runtime is now preferred on 64-bit ARM (aarch64)
- `pymicro-wakeword`'s TFLite models are stateful streaming models that don't work correctly with `tflite_runtime` on ARM64 (outputs all zeros)
- `openwakeword` ONNX models work correctly and provide accurate wake word detection
- The detector automatically prefers ONNX when available, with fallback to pymicro-wakeword for 32-bit systems

### Why PyAudio instead of soundcard?

- `soundcard` library uses PipeWire/PulseAudio which may not detect the ReSpeaker HAT
- `PyAudio` uses ALSA directly and respects the `.asoundrc` configuration
- This ensures the ReSpeaker microphone is used instead of HDMI audio

### Thread Safety

The satellite uses `asyncio.run_coroutine_threadsafe()` to safely schedule async operations from the audio capture thread, which runs separately from the main event loop.

## License

MIT
