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
  - **Alternative:** ReSpeaker XVF3800 macht AEC + Beamforming + Noise Suppression in Hardware
    → siehe [docs/XVF3800_SATELLITE.md](../../docs/XVF3800_SATELLITE.md)

### Medium Priority

- [ ] **Boot-Window WS-Handshake-Timeout (rotes LED-Blink beim Hochfahren)**
  - **Symptom:** Nach Pi-Reboot blinken die LEDs ~11 Min rot, dann wechselt der Satellite auf grünen IDLE-Pulse und funktioniert. Pro fehlgeschlagenem Versuch: `Server error: timed out during opening handshake`.
  - **Verifizierter Beobachtungsfall (2026-05-03, sat-wohnzimmer):** WLAN/DHCP fertig 20:22:27 → 9 fehlgeschlagene WS-Connects 20:23:06 bis 20:29:44 (Backoff 5→10→20→40→60s cap) → NTP `Initial clock synchronization` 20:33:18 → 1. erfolgreicher Connect 20:34:09 (51 s nach NTP-Sync).
  - **Hard facts:** Backend-Pod hatte 30 h Uptime (kein k8s-seitiges Problem). Backend-Logs zeigen **keinen einzigen** der 9 Versuche → Failure passiert vor dem FastAPI-Handler (TCP, TLS oder Traefik-Stage). WLAN war stabil, kein flap.
  - **NTP-Korrelation auffällig, Kausalität nicht bewiesen.** Drei verbliebene plausible Mechanismen:
    1. mDNS-Warmup: Avahi am Pi gerade gestartet, k8s `mdns-responder`-Pod (192.168.1.180) muss `renfield.local → 192.168.1.230` per Multicast publizieren. Bei IGMP-Snooping/Multicast-Filtering im Heimrouter sind initiale Antworten verzögert. Würde sich aber eher als `getaddrinfo`-Hang äußern, nicht als reproduzierbare 10-s-Library-Default-Timeouts.
    2. TLS-Cert-Validity: Saved-Clock-Init lag bei 20:21:59. Bei `notBefore` der Server-Cert in der Zukunft → TLS-Verify-Fail. *Aber:* würde `CERTIFICATE_VERIFY_FAILED` liefern, nicht „timed out".
    3. Traefik / k8s-Ingress-Stage — keine Beobachtung möglich (Default-Config loggt keine Access-Lines).
  - **Diagnoseplan beim nächsten Auftreten:**
    - `tcpdump` als systemd-service auf dem Pi installieren, der wlan0-Traffic ab Boot mitschneidet
    - Avahi mit Verbose-Logging starten
    - Aus pcap eindeutig sehen: TCP-SYN ohne SYN-ACK? TLS-ClientHello ohne ServerHello? mDNS-Anfragen ohne Antwort?
    - Erst dann Symptom-Hardening (z. B. `open_timeout=30s` in `WebSocketClient` + kürzerer Initial-Backoff) oder echten Root-Cause-Fix entscheiden.
  - **Workaround heute:** `systemctl restart renfield-satellite` nach Boot reicht aus.
  - Quelle/Kontext der Untersuchung: Session vom 2026-05-03; Pfade: `src/satellite/renfield_satellite/network/websocket_client.py:240-241` (ping_interval/timeout), `:535-566` (heartbeat fire-and-forget — separate Härtung möglich), `satellite.py:411-446` (`_reconnect_with_discovery`).

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
  - **Alternative:** XVF3800 hat Hardware-AEC → siehe [docs/XVF3800_SATELLITE.md](../../docs/XVF3800_SATELLITE.md)

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
