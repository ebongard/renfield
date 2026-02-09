# Technical Debt & Future TODOs

## Renfield Satellite

### Hardware Status (Pi Zero 2 W with 64-bit OS)

| Item | Status | Notes |
|------|--------|-------|
| **Silero VAD** | ✅ Working | ONNX Runtime 1.23.2 funktioniert auf aarch64 |
| **Noise Reduction** | ✅ Working | `noisereduce` vollständig installierbar auf 64-bit |
| **ONNX Runtime** | ✅ Working | Version 1.23.2 mit CPUExecutionProvider |
| **Beamforming** | ✅ Working | DAS Beamforming mit ReSpeaker 2-Mics (58mm Abstand) |
| **GPU Acceleration** | N/A | Pi Zero hat keine GPU für ML |

---

## Resolved Items

### ✅ 64-bit OS für Pi Zero 2 W (RESOLVED 2026-01-26)

Pi Zero 2 W läuft jetzt mit 64-bit OS (Debian 12 bookworm aarch64):
- ONNX Runtime 1.23.2 funktioniert
- Silero VAD (~2.3MB ONNX model) funktioniert
- `noisereduce` vollständig installierbar
- PyTorch nicht getestet (nicht benötigt dank ONNX)

### ✅ Silero VAD (RESOLVED 2026-01-26)

VAD-Modul unterstützt jetzt mehrere Backends:
- RMS (immer verfügbar)
- WebRTC VAD (leichtgewichtig)
- Silero VAD via ONNX Runtime (beste Qualität auf 64-bit)

Model-Download:
```bash
curl -L -o /opt/renfield-satellite/models/silero_vad.onnx \
  'https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad.onnx'
```

---

## Future TODOs

### High Priority

- [x] **ReSpeaker 4-Mic Array (AC108) Audio Capture** ✅ (RESOLVED 2026-02-09)
  - AC108 codec supports only 4ch/S32_LE natively
  - **Root cause:** PyAudio + onnxruntime (openwakeword) in the **same process** causes
    kernel crash. The crash happens at `pa.open()` — not during audio processing. Each
    library works independently; together they trigger a kernel panic on Pi Zero 2 W.
  - **Solution:** `use_arecord: true` — `arecord` subprocess isolates the I2S driver
    from onnxruntime. arecord captures 4ch/S32_LE, Python converts to mono S16_LE
    (**channel 1**, right-shift 16 bits). Channel 0 is silent (reference/unused),
    microphones are on channels 1, 2, 3.
  - Config: `device: "hw:0,0"`, `channels: 4`, `use_arecord: true`
  - `OMP_NUM_THREADS=1` in systemd service limits onnxruntime CPU usage
  - `os._exit(0)` in shutdown handler prevents `pa.terminate()` kernel crash
  - **VAD:** RMS backend recommended — Silero VAD unreliable under CPU load (two ONNX
    models per chunk saturates Pi Zero 2 W). Silence detection uses audio-chunk counting
    instead of wall-clock time to be immune to CPU processing lag.

- [ ] **Audio Preprocessing auf Backend verschieben**: Für ressourcenschwache Satellites
  - Noise Reduction im Backend statt auf Satellite
  - Satellite sendet Raw Audio, Backend preprocessed vor Whisper

### Medium Priority

- [x] **Sprechererkennung** ✅ (Bereits im Backend implementiert)
  - SpeechBrain ECAPA-TDNN auf Backend
  - Speaker Enrollment via Web-UI
  - Personalisierte Antworten pro Benutzer

- [ ] **Opus Audio Compression**
  - Statt Base64-PCM → Opus-kodiert
  - ~50% weniger Bandbreite
  - Minimal Qualitätsverlust bei 16kHz Voice

- [ ] **Echo Cancellation**
  - Wenn Satellite spricht und gleichzeitig aufnimmt
  - WebRTC Audio Processing Library

### Low Priority

- [x] **Beamforming mit 2 Mikrofonen** ✅ (Implementiert 2026-01-26)
  - Delay-and-Sum (DAS) Beamforming für ReSpeaker 2-Mics HAT
  - 3-6 dB SNR Verbesserung für seitlichen Lärm
  - Stereo-Aufnahme mit automatischer Mono-Konvertierung
  - ~5-7% CPU Overhead auf Pi Zero 2 W

- [ ] **Beamforming mit 4 Mikrofonen** (Audio capture now works — see resolved item above)
  - Extend BeamformerDAS for 4-mic circular array geometry
  - arecord captures all 4 channels — extract multiple channels for beamforming
  - ReSpeaker 4-Mic Array mic positions need calibration
  - Expected: 6-10 dB SNR gain, multi-axis noise rejection
  - ~10-15% CPU on Pi Zero 2 W (estimated)

- [ ] **Wake Word Training**
  - Custom Wake Words trainieren
  - OpenWakeWord Training Pipeline

- [x] **OTA Updates für Satellites** ✅ (Implementiert in #26)
  - Automatische Software-Updates via Web-UI
  - Rollback bei Fehlern
  - Siehe: `docs/SATELLITE_OTA_UPDATES.md`

---

## Pi Zero 2 W mit 64-bit OS

### Anforderungen
- **64-bit OS erforderlich** (Debian 12 bookworm aarch64)
- 512MB RAM ist ausreichend für ONNX Runtime
- Custom GPCLK Overlay für ReSpeaker HAT (siehe `src/satellite/hardware/`)

### Funktionierende Features (64-bit)
| Feature | Status | Package |
|---------|--------|---------|
| RMS VAD | ✅ | numpy |
| WebRTC VAD | ✅ | webrtcvad |
| Silero VAD | ✅ | onnxruntime |
| Noise Reduction | ✅ | noisereduce |
| Beamforming (DAS) | ✅ | numpy |
| Wake Word (TFLite) | ✅ | pymicro-wakeword |
| Wake Word (ONNX) | ✅ | pyopen-wakeword |

### Installation auf 64-bit
```bash
# Standard-Installation (funktioniert vollständig)
pip install -r requirements.txt

# Silero VAD Model
mkdir -p /opt/renfield-satellite/models
curl -L -o /opt/renfield-satellite/models/silero_vad.onnx \
  'https://github.com/snakers4/silero-vad/raw/master/src/silero_vad/data/silero_vad.onnx'
```

---

## Legacy: Pi Zero 2 W mit 32-bit OS

> **Nicht mehr empfohlen.** Verwende 64-bit OS für volle Funktionalität.

### Einschränkungen (32-bit)
- ARM32 (armv7l) → PyTorch/ONNX Runtime nicht verfügbar
- Silero VAD nicht möglich → WebRTC VAD als Alternative
- noisereduce benötigt `--no-deps` Installation

### Workarounds (32-bit)
| Problem | Workaround |
|---------|------------|
| Silero VAD nicht möglich | WebRTC VAD |
| noisereduce hängt | `pip install noisereduce --no-deps` |
| onnxruntime nicht verfügbar | Nicht möglich auf 32-bit |
