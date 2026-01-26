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

```bash
git clone https://github.com/HinTak/seeed-voicecard.git
cd seeed-voicecard
sudo ./install.sh
sudo reboot
```

> **Note:** We use the [HinTak fork](https://github.com/HinTak/seeed-voicecard) which has better kernel compatibility.

#### MCLK Fix for 64-bit OS (Pi Zero 2 W)

On 64-bit Raspberry Pi OS, the WM8960 codec needs a proper MCLK signal. Create a custom overlay:

```bash
# Create the overlay source
cat > /tmp/gpclk-pwm.dts << 'EOF'
/dts-v1/;
/plugin/;

/ {
    compatible = "brcm,bcm2835";

    fragment@0 {
        target = <&gpio>;
        __overlay__ {
            gpclk0_pins: gpclk0_pins {
                brcm,pins = <4>;
                brcm,function = <4>; /* ALT0 = GPCLK0 */
            };
        };
    };

    fragment@1 {
        target-path = "/";
        __overlay__ {
            gpclk0: gpclk0@7e101070 {
                compatible = "brcm,bcm2835-clock";
                reg = <0x7e101070 0x08>;
                clocks = <&clocks 6>;  /* PLLD = 500MHz */
                #clock-cells = <0>;
                clock-output-names = "gpclk0";
                pinctrl-names = "default";
                pinctrl-0 = <&gpclk0_pins>;
                assigned-clocks = <&gpclk0>;
                assigned-clock-rates = <12288000>;
            };
        };
    };
};
EOF

# Compile and install
dtc -@ -I dts -O dtb -o /tmp/gpclk-pwm.dtbo /tmp/gpclk-pwm.dts
sudo cp /tmp/gpclk-pwm.dtbo /boot/firmware/overlays/

# Add to config.txt
echo "dtoverlay=gpclk-pwm" | sudo tee -a /boot/firmware/config.txt

sudo reboot
```

After reboot, verify MCLK is working:
```bash
cat /sys/kernel/debug/clk/clk_summary | grep gpclk
# Should show: gpclk0 12288000 Hz
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
cat > ~/.asoundrc << 'EOF'
# Rate converter for sample rate conversion
defaults.pcm.rate_converter "samplerate"

pcm.!default {
    type asym
    playback.pcm "playback"
    capture.pcm "capture"
}

pcm.playback {
    type plug
    slave.pcm "dmixed"
}

pcm.capture {
    type plug
    slave.pcm "array"
}

pcm.dmixed {
    type dmix
    slave.pcm "hw:seeed2micvoicec"
    ipc_key 555555
}

# Stereo microphone array (for beamforming)
pcm.array {
    type dsnoop
    slave {
        pcm "hw:seeed2micvoicec"
        channels 2
    }
    ipc_key 666666
}

# Stereo capture with automatic rate conversion (for beamforming at 16kHz)
pcm.array16k {
    type rate
    slave {
        pcm "array"
        rate 48000
    }
    converter "samplerate"
}
EOF
```

> **Important:** Without this configuration, PyAudio will use the wrong audio device (HDMI instead of ReSpeaker).

**Note:** The `array16k` device is required for beamforming with stereo capture at 16kHz sample rate.

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

For **64-bit ARM (aarch64)** on Pi Zero 2 W:

```bash
# Check architecture
uname -m
# Should show: aarch64

# Install ONNX Runtime (official release for 64-bit ARM)
pip install onnxruntime

# Install PyAudio (preferred for ALSA/ReSpeaker support)
pip install pyaudio

# Install other dependencies
pip install numpy openwakeword websockets python-mpv spidev lgpio pyyaml zeroconf
```

> **Note:** We use PyAudio instead of soundcard because PyAudio uses ALSA directly and respects the `.asoundrc` configuration. The soundcard library uses PipeWire/PulseAudio which may not detect the ReSpeaker HAT.

> **Note:** On 64-bit OS, use `lgpio` instead of `RPi.GPIO` for GPIO control (button).

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
  channels: 1                   # Use 2 for beamforming (automatically set when enabled)
  device: "default"             # Uses ALSA default, or "array16k" for beamforming
  playback_device: "default"
  beamforming:
    enabled: false              # Enable Delay-and-Sum beamforming
    mic_spacing: 0.058          # ReSpeaker 2-Mics HAT: 58mm spacing
    steering_angle: 0.0         # 0 = front-facing

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

On Raspberry Pi Zero 2 W (64-bit):

| Component | CPU | Memory |
|-----------|-----|--------|
| Wake Word (pymicro-wakeword) | 10-15% | ~80MB |
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

- `pymicro-wakeword` is now recommended on 64-bit (built-in TFLite models work)
- `openwakeword` with ONNX runtime is an alternative with similar accuracy
- Both provide accurate wake word detection

### Why PyAudio instead of soundcard?

- `soundcard` library uses PipeWire/PulseAudio which may not detect the ReSpeaker HAT
- `PyAudio` uses ALSA directly and respects the `.asoundrc` configuration
- This ensures the ReSpeaker microphone is used instead of HDMI audio

### Thread Safety

The satellite uses `asyncio.run_coroutine_threadsafe()` to safely schedule async operations from the audio capture thread, which runs separately from the main event loop.

## License

MIT
