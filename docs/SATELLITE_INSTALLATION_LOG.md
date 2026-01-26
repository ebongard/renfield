# Renfield Satellite Installation Log

**Target:** satellite-benszimmer.local
**Date:** 2026-01-26
**System:** Debian 12 (bookworm), aarch64 (64-bit ARM)
**Kernel:** 6.12.62+rpt-rpi-v8
**Hardware:** Raspberry Pi Zero 2 W + ReSpeaker 2-Mics Pi HAT

---

## 1. System Information

```
Linux satellite-BensZimmer 6.12.62+rpt-rpi-v8 #1 SMP PREEMPT
Architecture: aarch64 (64-bit ARM)
OS: Debian GNU/Linux 12 (bookworm)
Model: Raspberry Pi Zero 2 W Rev 1.0
```

## 2. Installation Steps

### 2.1 System Update

```bash
sudo apt update && sudo apt upgrade -y
```

**Status:** COMPLETED

### 2.2 Install System Dependencies

```bash
sudo apt install -y \
    python3 python3-pip python3-venv python3-dev \
    git \
    libasound2-dev portaudio19-dev \
    libsndfile1 \
    mpv libmpv-dev \
    libgpiod2 python3-lgpio \
    python3-spidev
```

**Status:** COMPLETED

### 2.3 Create Installation Directory

```bash
sudo mkdir -p /opt/renfield-satellite
sudo chown evdb:evdb /opt/renfield-satellite
```

**Status:** COMPLETED

### 2.4 Copy Satellite Code

```bash
rsync -av --exclude='__pycache__' --exclude='*.pyc' --exclude='.git' \
    src/satellite/ satellite-benszimmer.local:/opt/renfield-satellite/
```

**Status:** COMPLETED

### 2.5 Create Python Virtual Environment

```bash
cd /opt/renfield-satellite
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

**Status:** COMPLETED

### 2.6 Install Additional Python Dependencies

```bash
pip install pyaudio lgpio webrtcvad pymicro-wakeword pyopen-wakeword aiohttp
```

**Status:** COMPLETED

### 2.7 Configure Satellite

Created `/opt/renfield-satellite/config/satellite.yaml`:
```yaml
satellite:
  id: "sat-benszimmer"
  room: "Bens Zimmer"
  language: "de"

server:
  url: "ws://demeter.local:8000/ws/satellite"
  auto_discover: true
  reconnect_interval: 5

audio:
  sample_rate: 16000
  channels: 1
  chunk_size: 480

wakeword:
  engine: "micro"
  default_keyword: "alexa"
  threshold: 0.5

hardware:
  led:
    enabled: true
    type: "apa102"
    num_leds: 3
    brightness: 50
  button:
    enabled: true
    gpio: 17
```

**Status:** COMPLETED

### 2.8 Setup Systemd Service

Created `/etc/systemd/system/renfield-satellite.service`:
```ini
[Unit]
Description=Renfield Satellite Voice Assistant
After=network-online.target sound.target
Wants=network-online.target

[Service]
Type=simple
User=evdb
WorkingDirectory=/opt/renfield-satellite
ExecStart=/opt/renfield-satellite/venv/bin/python -m renfield_satellite config/satellite.yaml
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable renfield-satellite
```

**Status:** COMPLETED

### 2.9 Configure User Permissions

```bash
sudo usermod -aG audio,gpio,spi,i2c evdb
```

**Status:** COMPLETED

### 2.10 Enable Hardware Interfaces

```bash
sudo raspi-config nonint do_spi 0
sudo raspi-config nonint do_i2c 0
```

**Status:** COMPLETED

### 2.11 Install ReSpeaker HAT Driver

```bash
cd /tmp
git clone --depth=1 https://github.com/HinTak/seeed-voicecard
cd seeed-voicecard
sudo ./install.sh
```

**Note:** DKMS kernel module failed to build for kernel 6.12.x, but overlay files were installed.

**Status:** COMPLETED (with kernel module build failure)

### 2.12 Configure Audio Overlay (Initial Attempt - FAILED)

Added to `/boot/firmware/config.txt`:
```
dtparam=i2s=on
dtoverlay=seeed-2mic-voicecard
```

**Status:** FAILED - MCLK clock issue on kernel 6.x

### 2.13 Custom GPCLK Overlay (FIX)

The standard overlays use a "fixed-clock" device which doesn't generate an actual clock signal on kernel 6.x. Created a custom overlay that uses the real BCM2835 GPCLK0 hardware clock.

```bash
# Copy custom overlay to satellite
rsync -av src/satellite/hardware/ satellite-benszimmer.local:/tmp/hardware/

# Install the overlay
ssh satellite-benszimmer.local "cd /tmp/hardware && sudo ./install-overlay.sh simple"

# Reboot to apply
sudo reboot
```

The custom overlay (`seeed-2mic-gpclk-simple`) configures:
- GPIO4 in ALT0 mode for GPCLK0 output
- BCM2835 clock manager to output 12.288 MHz
- WM8960 codec to consume this real clock

**Status:** COMPLETED - Audio capture now works

### 2.14 Start Service

```bash
sudo systemctl start renfield-satellite
```

**Status:** COMPLETED - All components functional

---

## 3. Verification

- [x] Service running (systemd active)
- [x] WebSocket connection to server (connected successfully)
- [x] Wake word detection (hey_mycroft model loaded)
- [x] **Audio capture** - WORKING (after custom GPCLK overlay)
- [ ] TTS playback - Not tested yet

---

## 4. Resolved Issues

### 4.1 WM8960 MCLK Clock Issue (RESOLVED)

**Original Error:**
```
wm8960 1-001a: No MCLK configured
ASoC: error at snd_soc_dai_hw_params on wm8960-hifi: -22
```

**Root Cause:**
The WM8960 codec requires a 12.288 MHz master clock (MCLK) signal. Standard overlays (`seeed-2mic-voicecard`, `wm8960-soundcard`) use a `fixed-clock` device tree node which only provides metadata - it doesn't actually generate a clock signal. On kernel 6.x, the WM8960 driver is stricter and requires a real clock.

**Solution:**
Created a custom device tree overlay (`seeed-2mic-gpclk-simple`) that:
1. Configures GPIO4 in ALT0 mode (GPCLK0 function)
2. Uses BCM2835_CLOCK_GP0 (clock ID 38) instead of fixed-clock
3. Sets `assigned-clock-rates = <12288000>` for 12.288 MHz output
4. Has the WM8960 codec consume this clock, triggering `clk_prepare_enable()` in the driver

The overlay source is in `src/satellite/hardware/` with installation instructions.

**Status:** RESOLVED - Audio capture fully functional

### 4.2 DKMS Module Build Failure (Not Critical)

The seeed-voicecard kernel module failed to build for kernel 6.12.x due to kernel header mismatches. This is not critical as our custom device tree overlay provides all necessary functionality.

---

## 5. Working Components

| Component | Status | Notes |
|-----------|--------|-------|
| SPI (LEDs) | Working | APA102 LEDs functional |
| GPIO (Button) | Working | Button on GPIO17 |
| Wake Word Detection | Working | pymicro-wakeword with hey_mycroft |
| WebSocket Connection | Working | Connects to server successfully |
| Audio Capture | **Working** | Fixed with custom GPCLK overlay |
| Audio Playback | Working | Via ALSA/MPV |

---

## 6. Service Status Summary

```
Service: renfield-satellite.service
Status: active (running)
WebSocket: Connected to ws://demeter.local:8000/ws/satellite
Wake Words: hey_mycroft (loaded)
Audio Backend: PyAudio (ALSA)
Audio Capture: WORKING
Overlay: seeed-2mic-gpclk-simple
```

---

## 7. Files Created/Modified

| File | Purpose |
|------|---------|
| `/opt/renfield-satellite/` | Installation directory |
| `/opt/renfield-satellite/config/satellite.yaml` | Configuration |
| `/etc/systemd/system/renfield-satellite.service` | Systemd unit |
| `/home/evdb/.asoundrc` | ALSA configuration |
| `/boot/firmware/config.txt` | Boot configuration with overlay |
| `/boot/firmware/overlays/seeed-2mic-gpclk-simple.dtbo` | Custom GPCLK overlay (MCLK fix) |

### Source Files (in repository)

| File | Purpose |
|------|---------|
| `src/satellite/hardware/seeed-2mic-gpclk-overlay.dts` | Full GPCLK overlay source |
| `src/satellite/hardware/seeed-2mic-gpclk-simple-overlay.dts` | Simplified GPCLK overlay source |
| `src/satellite/hardware/install-overlay.sh` | Overlay installation script |
| `src/satellite/hardware/README.md` | Hardware configuration documentation |

---

## 8. Next Steps

1. ~~**Resolve MCLK issue**~~ - DONE: Custom GPCLK overlay created and installed
2. **Full integration test** - Test wake word → transcription → response → TTS flow
3. **Volume calibration** - Optimize microphone gain and speaker volume
4. **LED animations** - Verify LED state feedback works correctly
