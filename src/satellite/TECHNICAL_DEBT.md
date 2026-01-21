# Technical Debt & Future TODOs

## Renfield Satellite

### Hardware Limitations (Pi Zero 2 W)

| Item | Status | Notes |
|------|--------|-------|
| **Silero VAD** | Blocked | PyTorch/ONNX Runtime nicht verfügbar auf ARM32. Workaround: WebRTC VAD |
| **Noise Reduction** | Partial | `noisereduce` Installation hängt (matplotlib/contourpy). Workaround: `pip install noisereduce --no-deps` |
| **GPU Acceleration** | N/A | Pi Zero hat keine GPU für ML |

---

## Future TODOs

### High Priority

- [ ] **Silero VAD für Pi 4/5**: Implementierung für leistungsstärkere Satellites
  - PyTorch oder ONNX Runtime funktioniert auf Pi 4 (64-bit OS)
  - Silero VAD bietet bessere Spracherkennung als WebRTC VAD
  - Model: `silero_vad.onnx` (~2MB)

- [ ] **64-bit OS für Pi Zero 2 W testen**: Könnte ONNX Runtime ermöglichen
  - Pi Zero 2 W hat 64-bit CPU, läuft aber standardmäßig mit 32-bit OS
  - 64-bit OS könnte PyTorch Lite / ONNX Runtime ermöglichen

- [ ] **Audio Preprocessing auf Backend verschieben**: Für ressourcenschwache Satellites
  - Noise Reduction im Backend statt auf Satellite
  - Satellite sendet Raw Audio, Backend preprocessed vor Whisper

### Medium Priority

- [ ] **Sprechererkennung (Phase 4)**
  - pyannote.audio auf Backend (State-of-the-Art)
  - Resemblyzer als leichtgewichtige Alternative
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

- [ ] **Beamforming mit 2 Mikrofonen**
  - ReSpeaker HAT hat 2 Mikrofone
  - Aktuell wird nur Mono genutzt
  - Beamforming könnte Noise Rejection verbessern

- [ ] **Wake Word Training**
  - Custom Wake Words trainieren
  - OpenWakeWord Training Pipeline

- [ ] **OTA Updates für Satellites**
  - Automatische Software-Updates
  - Rollback bei Fehlern

---

## Bekannte Einschränkungen

### Pi Zero 2 W
- 512MB RAM → große Python-Pakete können nicht kompiliert werden
- ARM32 (armv7l) → PyTorch nicht verfügbar
- Keine piwheels für alle Pakete

### Workarounds
| Problem | Workaround |
|---------|------------|
| Silero VAD nicht möglich | WebRTC VAD (`pip install webrtcvad`) |
| noisereduce hängt | `pip install noisereduce --no-deps` |
| onnxruntime hängt | Swap erhöhen oder weglassen |
| torch nicht verfügbar | Kein Workaround auf ARM32 |

---

## Getestete Konfiguration

**Hardware:**
- Raspberry Pi Zero 2 W
- ReSpeaker 2-Mics Pi HAT V2.0
- Raspberry Pi OS Lite (32-bit, Bookworm)

**Funktionierende VAD-Backends:**
- ✅ RMS (immer verfügbar)
- ✅ WebRTC VAD (`pip install webrtcvad`)
- ❌ Silero VAD (PyTorch/ONNX nicht verfügbar)

**Audio Preprocessing:**
- ✅ Normalisierung (numpy only)
- ⚠️ Noise Reduction (benötigt `--no-deps` Installation)
