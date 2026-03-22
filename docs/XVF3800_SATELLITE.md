# ReSpeaker XMOS XVF3800 Satellite Integration

## Status: Planung

Dieses Dokument beschreibt zwei Integrationswege fuer das Seeed Studio ReSpeaker XMOS XVF3800
4-Mic Array als Renfield-Satellite. Das Board gibt es in zwei Varianten:

- **Standalone (SKU p-6488, ~$50):** Nur XVF3800, USB-Modus
- **Mit XIAO ESP32S3 (SKU p-6489, ~$55):** XVF3800 + ESP32S3, USB- oder WiFi-Modus

---

## Hardware-Spezifikation

| Eigenschaft | Wert |
|---|---|
| **Formfaktor** | Kreisfoermig, 99mm Durchmesser, 4mm Hoehe |
| **Mikrofone** | 4x PDM MEMS, kreisfoermig angeordnet (SNR 64dBA, AOP 120dBL) |
| **Audio-Prozessor** | XMOS XVF3800 (xcore.ai), VocalFusion 4-Mic |
| **Audio-Codec** | TI TLV320AIC3104 (DAC/ADC fuer Speaker-Ausgang) |
| **LEDs** | 12x WS2812B (NeoPixel Ring), individuell ansteuerbar |
| **Lautsprecher** | 3.5mm Klinke + JST PH 2.0 (bis 5W, Class-D Verstaerker) |
| **USB** | USB Audio Class 2.0 (UAC2), Plug-and-Play |
| **Stromversorgung** | 5V USB-C, ~1-2W gesamt |
| **Tasten** | Mute-Button (LED wird rot bei Mute), Reset-Button |

### XMOS XVF3800 Audio-Pipeline (on-chip)

Die gesamte Audioverarbeitung laeuft **auf dem XMOS-Chip in Hardware**, nicht auf dem Host:

```
4x PDM MEMS Mikrofone
  -> Acoustic Echo Cancellation (AEC)     # Entfernt Speaker-Audio aus Mikrofon-Signal
  -> 3-Beam Beamforming                   # 1 Scan-Beam + 2 Tracking-Beams
  -> Direction of Arrival (DoA)           # Sprecherposition erkennen
  -> De-Reverberation                     # Raumakustik kompensieren
  -> DNN Noise Suppression               # Neuronales Netz, stationaer + nicht-stationaer
  -> Voice Activity Detection (VAD)
  -> Automatic Gain Control (60dB)
  -> Processed Output (16kHz, mono)       # Sauberes Sprachsignal
```

Dies ersetzt die Software-Verarbeitung auf dem Pi (Beamforming, noisereduce, VAD) und die
geplante Backend-Verschiebung (`TECHNICAL_DEBT.md` > "Audio Preprocessing auf Backend verschieben").
Der XVF3800 liefert bereits vorverarbeitetes Audio — weder Satellite noch Backend muessen
Noise Reduction oder Beamforming durchfuehren.

### XIAO ESP32S3 (nur bei SKU p-6489)

| Eigenschaft | Wert |
|---|---|
| **MCU** | ESP32-S3R8, Dual-Core Xtensa LX7, 240MHz |
| **RAM** | 512KB SRAM + 8MB PSRAM (Octal, 80MHz) |
| **Flash** | 8MB SPI |
| **WiFi** | 802.11n, 2.4GHz (kein 5GHz) |
| **Bluetooth** | BLE 5.0 + Classic BT |
| **Groesse** | 21 x 17.5mm |
| **Deep Sleep** | ~14uA |

### Zwei USB-C Ports (nur ESP32S3-Variante)

Das Board hat zwei USB-C Anschluesse, die **nicht gleichzeitig** angeschlossen werden koennen:

- **XMOS USB-C** (neben 3.5mm Klinke): UAC2 Audio + DFU fuer XVF3800-Firmware
- **ESP32S3 USB-C** (auf dem XIAO-Modul): Serial/JTAG/Flash fuer ESP32

Es gibt zwei Firmware-Varianten fuer den XVF3800 — sie sind **gegenseitig exklusiv**:

| Firmware | Version | Modus | Kanäle | Verwendung |
|---|---|---|---|---|
| USB (Standard) | v2.0.x | UAC2 ueber USB | 2ch oder 6ch | An PC/Pi als USB-Mikrofon |
| I2S Master | v1.0.x | I2S zum ESP32S3 | 2ch | ESPHome/WiFi-Satellite |

---

## Ansatz A: XVF3800 via USB am Raspberry Pi

### Ueberblick

Das XVF3800 wird als USB-Audigeraet an einen vorhandenen Pi Zero 2 W angeschlossen.
Der bestehende Python-Satellite-Stack laeuft unveraendert weiter, profitiert aber von
der ueberlegenen Audio-Hardware.

```
[XVF3800 Board] ── USB-C ──> [Raspberry Pi Zero 2 W]
                                ├─ ALSA: "reSpeaker XVF3800 4-Mic Array"
                                ├─ Wake Word (openwakeword/ONNX, lokal)
                                ├─ VAD, Audio Streaming
                                ├─ LED-Steuerung (optional, separater SPI)
                                ├─ Button (GPIO)
                                └─ WebSocket Client → Renfield Backend
```

### Vorteile gegenueber ReSpeaker HATs

| Aspekt | ReSpeaker 2-Mic/4-Mic HAT | XVF3800 via USB |
|---|---|---|
| **Treiber** | seeed-voicecard Kernel-Modul, bricht bei Updates | Kein Treiber, Standard `snd_usb_audio` |
| **Audio-Vorverarbeitung** | Software (Python Beamformer, noisereduce) | **Hardware** (AEC, 3-Beam, DNN auf XMOS-Chip) |
| **Kernel-Crash** | AC108+onnxruntime = Kernel Panic | USB-Audio isoliert, kein Crash |
| **`use_arecord` Hack** | Noetig fuer 4-Mic Array | Nicht noetig |
| **Kanal-Mapping** | AC108 Kanal 0 ist stumm (Bug) | Kanal 0 = sauberes, vorverarbeitetes Audio |
| **Echo Cancellation** | Nicht vorhanden (TECHNICAL_DEBT) | Hardware-AEC auf dem Chip |
| **Beamforming** | Software DAS (2-Mic) oder nicht (4-Mic) | Hardware 3-Beam auf dem Chip |
| **GPIO-Belegung** | HAT belegt GPIO (I2S, SPI, Button) | GPIO frei, nur USB-Kabel |
| **Montage** | HAT aufgesteckt | USB-Kabel, flexiblere Positionierung |
| **Full-Duplex** | Kein AEC → Probleme bei gleichzeitigem Playback | AEC nutzt USB-Playback als Referenz |

### ALSA-Geraet

Das Board erscheint als Standard-USB-Audio-Karte:

```bash
# Erkennung pruefen
arecord -l
# card N: XVF3800 [reSpeaker XVF3800 4-Mic Array], device 0: ...

# Aufnahme testen (vorverarbeitetes Audio auf Kanal 0)
arecord -D plughw:XVF3800,0 -c 1 -r 16000 -f S16_LE -d 5 test.wav

# Wiedergabe testen (ueber 3.5mm/JST Speaker)
aplay -D plughw:XVF3800,0 test.wav
```

**USB-Kanal-Layout (2ch-Firmware, Standard):**

| Kanal | Inhalt |
|---|---|
| 0 (Links) | Processed: AEC + Beamforming + Noise Suppression |
| 1 (Rechts) | ASR-Beam: Fuer Spracherkennung optimierter Beam |

**USB-Kanal-Layout (6ch-Firmware):**

| Kanal | Inhalt |
|---|---|
| 0 | Processed Conference Output |
| 1 | ASR-Beam |
| 2-5 | Rohe Mikrofon-Signale (Mic 0-3) |

### Konfiguration

**`satellite.yaml`** — Aenderungen gegenueber ReSpeaker HAT:

```yaml
audio:
  device: "plughw:XVF3800,0"    # USB-Geraetename (stabil ueber Reboots)
  playback_device: "plughw:XVF3800,0"  # Speaker-Ausgang ueber selben USB
  channels: 1                    # Kanal 0 = vorverarbeitetes Mono-Audio
  use_arecord: false             # Nicht noetig — kein AC108 Kernel-Crash
  beamforming:
    enabled: false               # XVF3800 macht 3-Beam in Hardware
```

**`.asoundrc`** — Optional, fuer stabilen Geraete-Alias:

```
pcm.xvf3800 {
    type hw
    card "XVF3800"
}

pcm.!default {
    type asym
    playback.pcm "xvf3800"
    capture.pcm "xvf3800"
}
```

**Udev-Regel** (optional, verhindert USB-Autosuspend):

```
# /etc/udev/rules.d/99-respeaker-xvf3800.rules
ACTION=="add", SUBSYSTEM=="usb", ATTR{idVendor}=="2886", ATTR{idProduct}=="0037", \
    TEST=="power/autosuspend", ATTR{power/autosuspend}="-1"
```

### Aenderungen im Code

**Keine Code-Aenderungen noetig.** Der bestehende Satellite-Code funktioniert unveraendert:

- `capture.py` oeffnet das ALSA-Geraet via PyAudio → empfaengt S16_LE/16kHz/Mono
- Wake Word Detection, VAD, WebSocket Streaming — alles identisch
- Nur die YAML-Config muss angepasst werden (Device-Name)

### LED-Steuerung

Die 12 WS2812B LEDs auf dem XVF3800-Board werden **nicht** ueber SPI gesteuert (wie die
APA102 auf den ReSpeaker HATs), sondern ueber I2C-Befehle an den XMOS-Chip via das
`xvf_host` Tool. Alternativ:

- **Option 1:** `xvf_host` Aufrufe aus dem Satellite-Python-Code (Subprocess)
- **Option 2:** Separate APA102/WS2812B LED-Streifen am Pi ueber SPI (wie bisher)
- **Option 3:** LEDs ignorieren — die Audio-Qualitaet ist der Hauptvorteil

### Provisioning

Fuer Ansible (`provisioning/inventory.yml`) ein neues HAT-Profil:

```yaml
# host_vars/satellite-neuerraum.yml
hat_type: "xvf3800-usb"         # Neues Profil
audio_device: "plughw:XVF3800,0"
audio_playback_device: "plughw:XVF3800,0"
audio_channels: 1
use_arecord: false
beamforming_enabled: false       # Hardware-Beamforming
led_type: "none"                 # Oder "apa102" fuer separaten LED-Streifen
```

### TECHNICAL_DEBT Auswirkung

Durch den XVF3800 werden folgende offene Punkte adressiert:

| TODO | Status mit XVF3800 |
|---|---|
| Audio Preprocessing auf Backend verschieben | **Unnoetig** — XVF3800 macht es in Hardware |
| Echo Cancellation | **Geloest** — Hardware-AEC auf dem Chip |
| 4-Mic Beamforming | **Geloest** — Hardware 3-Beam auf dem Chip |
| Opus Audio Compression | Weiterhin sinnvoll fuer Bandbreiten-Optimierung |

---

## Ansatz B: ESP32S3 direkt via aioesphomeapi (ohne Raspberry Pi)

### Ueberblick

Der ESP32S3 wird mit ESPHome geflasht und verbindet sich direkt per WiFi mit dem
Renfield-Backend. Kein Raspberry Pi noetig. Das Backend implementiert einen
ESPHome Voice Assistant Server via `aioesphomeapi`.

```
[XVF3800 + XIAO ESP32S3]              [Renfield Backend]
  ├─ XVF3800: AEC, Beamforming          ├─ ESPHomeSatelliteManager
  ├─ ESP32S3: Wake Word (TinyML)         │    ├─ aioesphomeapi.APIClient pro Geraet
  ├─ ESP32S3: LED Ring (WS2812B)         │    ├─ ReconnectLogic (Auto-Reconnect)
  ├─ ESP32S3: Speaker (I2S→DAC)          │    └─ mDNS Discovery
  └─ ESPHome Native API (TCP:6053)       ├─ ESPHomeVoicePipeline
       │                                 │    ├─ handle_start() → Session erstellen
       │  ① Wake Word detected           │    ├─ handle_audio() → PCM sammeln
       │ ─────────────────────────→       │    ├─ WhisperService.transcribe()
       │                                 │    ├─ OllamaService.extract_intents()
       │  ② Raw PCM Audio Stream         │    ├─ ActionExecutor.execute()
       │ ─────────────────────────→       │    ├─ PiperService.synthesize()
       │                                 │    └─ send_voice_assistant_audio()
       │  ③ TTS Audio zurueck            │
       │ ←─────────────────────────       └─ Bestehende Services (unveraendert)
       │
       │  ④ Events (STT_END, etc.)
       │ ←─────────────────────────
```

### ESPHome Native API Protokoll

| Eigenschaft | Wert |
|---|---|
| **Transport** | TCP, Port 6053 (konfigurierbar) |
| **Serialisierung** | Protocol Buffers |
| **Verschluesselung** | Noise-Encryption (32-Byte PSK, Base64) |
| **Keepalive** | Ping/Pong, Timeout bei 4.5x Intervall |
| **Python-Library** | `aioesphomeapi` (asyncio-kompatibel) |
| **Reconnect** | Exponential Backoff + mDNS Listener (sofort bei Wiederverfuegbarkeit) |

### Audio-Format

| Eigenschaft | ESPHome liefert | Renfield erwartet | Kompatibel |
|---|---|---|---|
| Sample Rate | 16kHz | 16kHz | Ja |
| Bit Depth | 16-bit signed (S16_LE) | 16-bit signed (S16_LE) | Ja |
| Channels | Mono | Mono | Ja |
| Encoding | Raw PCM (kein Header) | Raw PCM → WAV-Header synthetisiert | Ja |
| Chunk-Groesse | 1024 Bytes (512 Samples, 32ms) | 2560 Bytes (1280 Samples, 80ms) | Ja (wird konkateniert) |

### Voice Pipeline Protokoll

**ESP32 → Backend (Callbacks in `subscribe_voice_assistant`):**

```python
# 1. Wake Word erkannt — Pipeline starten
async def handle_start(
    conversation_id: str,           # Eindeutige Session-ID
    flags: int,                     # USE_VAD, USE_WAKE_WORD
    audio_settings: VoiceAssistantAudioSettings,  # noise_suppression, auto_gain, volume
    wake_word_phrase: str | None,   # Welches Wake Word ("hey_jarvis", etc.)
) -> int:
    # Return 0 fuer TCP-Audio-Modus (kein UDP-Server noetig)
    return 0

# 2. Audio-Chunk empfangen (TCP-Modus, 1024 Bytes/Chunk)
async def handle_audio(audio_bytes: bytes) -> None:
    await audio_queue.put(audio_bytes)

# 3. Audio-Stream beendet (Stille erkannt oder Abbruch)
async def handle_stop(abort: bool) -> None:
    await audio_queue.put(None)  # Sentinel
```

**Backend → ESP32 (Events und Audio):**

```python
# Pipeline-Fortschritt signalisieren (steuert LED-States auf dem Geraet)
client.send_voice_assistant_event(VoiceAssistantEventType.VOICE_ASSISTANT_RUN_START, {})
client.send_voice_assistant_event(VoiceAssistantEventType.VOICE_ASSISTANT_STT_START, {})
client.send_voice_assistant_event(VoiceAssistantEventType.VOICE_ASSISTANT_STT_END, {"text": "Licht an"})
client.send_voice_assistant_event(VoiceAssistantEventType.VOICE_ASSISTANT_TTS_START, {"text": "Ok"})

# TTS-Audio streamen (16kHz, 16-bit, mono, raw PCM)
client.send_voice_assistant_event(VoiceAssistantEventType.VOICE_ASSISTANT_TTS_STREAM_START, {})
for chunk in wav_chunks(512):   # 512 Samples = 1024 Bytes pro Chunk
    client.send_voice_assistant_audio(chunk)
    await asyncio.sleep(0.032 * 0.9)  # 90% der Chunk-Dauer als Pacing
client.send_voice_assistant_event(VoiceAssistantEventType.VOICE_ASSISTANT_TTS_STREAM_END, {})

# Fehler melden
client.send_voice_assistant_event(VoiceAssistantEventType.VOICE_ASSISTANT_ERROR, {
    "code": "stt-failed",
    "message": "Whisper transcription failed"
})
```

### VoiceAssistant Feature Flags

```python
class VoiceAssistantFeature(enum.IntFlag):
    VOICE_ASSISTANT    = 1 << 0   # Basis Voice Assistant
    SPEAKER            = 1 << 1   # Lokaler Speaker fuer TTS
    API_AUDIO          = 1 << 2   # TCP-Audio (ab ESPHome 2024.4)
    TIMERS             = 1 << 3   # Timer-Verwaltung
    ANNOUNCE           = 1 << 4   # Media/Announcements abspielen
    START_CONVERSATION = 1 << 5   # Geraet kann Konversation initiieren
```

### Implementierungsplan

#### Neue Dateien

```
src/backend/
  services/
    esphome_satellite_manager.py    # Verbindungs-Management, Discovery, Reconnect
  api/
    esphome/
      voice_pipeline.py             # Voice Pipeline Handler (analog satellite_handler.py)
```

#### 1. ESPHomeSatelliteManager

Verwaltet Verbindungen zu allen ESP32-Satelliten:

```python
class ESPHomeSatelliteManager:
    """Manages connections to ESPHome voice satellites."""

    def __init__(self, config: ESPHomeSatelliteConfig):
        self._clients: dict[str, APIClient] = {}          # name → client
        self._pipelines: dict[str, ESPHomeVoicePipeline] = {}
        self._reconnect_logics: dict[str, ReconnectLogic] = {}

    async def start(self):
        """Connect to all configured ESPHome satellites."""
        for sat in self._config.satellites:
            client = APIClient(
                address=sat.host,
                port=sat.port,          # Default: 6053
                noise_psk=sat.noise_psk,
                client_info="renfield",
            )
            logic = ReconnectLogic(
                client=client,
                on_connect=lambda: self._on_connect(sat.name),
                on_disconnect=lambda expected: self._on_disconnect(sat.name, expected),
            )
            await logic.start()

    async def _on_connect(self, name: str):
        """Called when a satellite connects. Subscribe to voice assistant."""
        client = self._clients[name]
        info = await client.device_info()
        flags = info.voice_assistant_feature_flags
        pipeline = ESPHomeVoicePipeline(name, client, flags, self._services)
        self._pipelines[name] = pipeline
        client.subscribe_voice_assistant(
            handle_start=pipeline.handle_start,
            handle_stop=pipeline.handle_stop,
            handle_audio=pipeline.handle_audio if flags & VoiceAssistantFeature.API_AUDIO else None,
        )
```

#### 2. ESPHomeVoicePipeline

Mapped die ESPHome Voice Events auf die bestehende Renfield-Pipeline:

```python
class ESPHomeVoicePipeline:
    """Handles one voice session from an ESPHome satellite."""

    async def handle_start(self, conversation_id, flags, audio_settings, wake_word_phrase) -> int:
        self._session_id = conversation_id
        self._audio_queue = asyncio.Queue()
        asyncio.create_task(self._run_pipeline())
        return 0  # TCP-Audio-Modus

    async def handle_audio(self, audio_bytes: bytes):
        await self._audio_queue.put(audio_bytes)

    async def handle_stop(self, abort: bool):
        await self._audio_queue.put(None)  # Sentinel

    async def _run_pipeline(self):
        # 1. Audio sammeln
        pcm_chunks = []
        while True:
            chunk = await self._audio_queue.get()
            if chunk is None:
                break
            pcm_chunks.append(chunk)

        # 2. WAV assemblieren (wie satellite_handler.py)
        raw_pcm = b"".join(pcm_chunks)
        wav_bytes = self._pcm_to_wav(raw_pcm, sample_rate=16000, channels=1, sample_width=2)

        # 3. Bestehende Pipeline nutzen (identisch zu Pi-Satelliten)
        self._client.send_voice_assistant_event(STT_START, {})
        text = await self._whisper.transcribe_bytes(wav_bytes)
        self._client.send_voice_assistant_event(STT_END, {"text": text})

        intents = await self._ollama.extract_ranked_intents(text, room_context)
        result = await self._action_executor.execute(intents[0], permissions, user_id)
        response = await self._ollama.chat_stream(...)

        # 4. TTS zurueck streamen
        tts_wav = await self._piper.synthesize_to_bytes(response, language)
        await self._stream_tts(tts_wav)

    async def _stream_tts(self, wav_bytes: bytes):
        """Stream TTS audio back to ESP32 with pacing."""
        self._client.send_voice_assistant_event(TTS_STREAM_START, {})
        # WAV-Header ueberspringen (44 Bytes)
        pcm_data = wav_bytes[44:]
        chunk_size = 1024  # 512 Samples * 2 Bytes
        for i in range(0, len(pcm_data), chunk_size):
            self._client.send_voice_assistant_audio(pcm_data[i:i+chunk_size])
            await asyncio.sleep(0.032 * 0.9)  # 32ms pro Chunk, 90% Pacing
        self._client.send_voice_assistant_event(TTS_STREAM_END, {})
```

#### 3. Konfiguration

**`.env`** (Backend):

```bash
ESPHOME_SATELLITES_ENABLED=true
```

**`config/esphome_satellites.yaml`** (neu):

```yaml
satellites:
  - name: "sat-kueche"
    host: "xvf3800-kueche.local"
    port: 6053
    noise_psk: "base64_encoded_32_byte_key"
    room: "Kueche"
    language: "de"

  - name: "sat-schlafzimmer"
    host: "xvf3800-schlafzimmer.local"
    port: 6053
    noise_psk: "another_base64_key"
    room: "Schlafzimmer"
    language: "de"
```

PSK generieren: `openssl rand -base64 32`

#### 4. ESP32S3 Firmware (ESPHome YAML)

Basiert auf der [formatBCE ESPHome-Integration](https://github.com/formatBCE/Respeaker-XVF3800-ESPHome-integration).
Der XVF3800 muss mit der **I2S Master Firmware v1.0.7** geflasht werden (nicht USB-Firmware).

Wichtige ESPHome-Konfiguration:

```yaml
esphome:
  name: xvf3800-kueche
  friendly_name: "Satellite Kueche"

esp32:
  board: esp32-s3-devkitc-1
  framework:
    type: esp-idf

api:
  encryption:
    key: "base64_encoded_32_byte_key"  # Muss mit Backend-PSK uebereinstimmen

voice_assistant:
  microphone: xvf3800_mic
  speaker: xvf3800_speaker
  on_wake_word_detected:
    - light.turn_on: led_ring   # Visuelles Feedback

micro_wake_word:
  models:
    - model: hey_jarvis
```

### Feature-Vergleich: Pi-Satellite vs. ESP32-Satellite

| Feature | Pi + XVF3800 (Ansatz A) | ESP32S3 direkt (Ansatz B) |
|---|---|---|
| **Audio-Qualitaet** | XVF3800 Hardware-Processing | XVF3800 Hardware-Processing |
| **Wake Word** | openwakeword/ONNX auf Pi | micro_wake_word/TinyML auf ESP32 |
| **Speaker Recognition** | Ja (SpeechBrain auf Backend) | Ja (Audio geht auch zum Backend) |
| **BLE Presence** | `bleak` auf Pi | `esp32_ble_tracker` (ESPHome) |
| **Kamera** | Ja (Pi CSI-Port) | Nein |
| **LED-Steuerung** | Separater LED-Streifen oder xvf_host | ESPHome steuert WS2812B direkt |
| **OTA Updates** | Renfield OTA (Ansible/Web-UI) | ESPHome OTA (eigenes System) |
| **Stromverbrauch** | ~3-5W (Pi + XVF3800) | ~1-2W (nur XVF3800-Board) |
| **Kosten pro Raum** | ~$80+ (Pi + XVF3800 + SD + Netzteil) | ~$55 (XVF3800 + ESP32 + USB-Netzteil) |
| **SD-Karten-Risiko** | Ja (Pi SD-Karte kann korrupt werden) | Nein (kein Filesystem auf Flash) |
| **Protokoll** | WebSocket JSON (bestehend) | ESPHome Native API (neu) |
| **Backend-Aenderung** | Keine | Neuer Handler + Manager |
| **Offline-Faehigkeit** | Voll (Wake Word + VAD lokal) | Teilweise (Wake Word lokal, Rest via WiFi) |
| **Konversationskontext** | Voll (Session, RAG, Agent Loop) | Voll (Pipeline identisch nach STT) |

### Einschraenkungen (Ansatz B)

1. **Nur 2.4GHz WiFi** — kann in dichten WiFi-Umgebungen problematisch sein
2. **Wake Word waehrend Playback** — AEC funktioniert laut Community nicht zuverlaessig.
   Wake Word wird nicht erkannt, waehrend der Speaker Audio abspielt.
3. **Kein Kamera-Support** — ESP32S3 hat keinen CSI-Port
4. **Keine Custom-Logik** — ESPHome ist deklarativ; komplexe Logik muss im Backend liegen
5. **Community-Firmware** — Die formatBCE ESPHome-Integration ist nicht offiziell von
   Seeed oder ESPHome. Aenderungen koennen Breaking Changes enthalten.
6. **Gehaeuse** — Das offizielle Gehaeuse passt nicht mit ESP32S3-Modul. 3D-Druck noetig,
   aber Gehaeuse kann WiFi-Signal daempfen.

### Abhaengigkeiten (Ansatz B)

**Python (Backend):**

```
aioesphomeapi>=28.0    # ESPHome Native API Client
```

**Keine neuen System-Dependencies.** `aioesphomeapi` ist pure Python mit Protobuf.

---

## Empfohlene Strategie

### Phase 1: XVF3800 via USB am Pi (Ansatz A)

- **Aufwand:** Minimal (nur Config-Aenderung)
- **Risiko:** Sehr gering
- **Wann:** Sofort, als Drop-in Upgrade fuer bestehende Pi-Satelliten

Bestehende Pi Zero 2 W Satelliten bekommen das XVF3800-Board als USB-Mikrofon.
Die alten ReSpeaker HATs werden ersetzt. Kein Code, kein neuer Treiber, keine
Kernel-Crash-Gefahr.

### Phase 2: ESPHome-Integration (Ansatz B)

- **Aufwand:** Mittel (~2-3 Tage Implementierung)
- **Risiko:** Gering (nutzt bestehende Backend-Services)
- **Wann:** Fuer neue Raeume ohne vorhandenen Pi

Neuer `ESPHomeSatelliteManager` im Backend. Nutzt dieselbe Pipeline
(Whisper → Intent → Action → TTS) wie die Pi-Satelliten, nur mit anderem
Transport-Protokoll.

### Langfristig: Migration zu ESP32-only

Wenn die ESPHome-Integration stabil laeuft und Speaker Recognition nicht
benoetigt wird, koennen Pi-Satelliten schrittweise durch reine ESP32-Satelliten
ersetzt werden — guenstiger, stromsparender, kein SD-Karten-Risiko.

---

## Referenzen

- [Seeed Studio Produktseite (ESP32S3-Variante)](https://www.seeedstudio.com/ReSpeaker-XVF3800-4-Mic-Array-With-XIAO-ESP32S3-p-6489.html)
- [Seeed Wiki: Getting Started](https://wiki.seeedstudio.com/respeaker_xvf3800_xiao_getting_started/)
- [Seeed Wiki: Home Assistant Integration](https://wiki.seeedstudio.com/respeaker_xvf3800_xiao_home_assistant/)
- [XMOS XVF3800 Datasheet](https://www.xmos.com/download/XVF3800-Device-Datasheet)
- [XMOS VocalFusion Audio Pipeline](https://www.xmos.com/documentation/XM-014888-PC/html/modules/fwk_xvf/doc/datasheet/03_audio_pipeline.html)
- [formatBCE ESPHome-Integration (GitHub)](https://github.com/formatBCE/Respeaker-XVF3800-ESPHome-integration)
- [aioesphomeapi (GitHub)](https://github.com/esphome/aioesphomeapi)
- [GitHub: reSpeaker XVF3800 USB 4-Mic Array](https://github.com/respeaker/reSpeaker_XVF3800_USB_4MIC_ARRAY)
- [Home Assistant Community: XVF3800 ESPHome](https://community.home-assistant.io/t/respeaker-xmos-xvf3800-esphome-integration/927241)

### Verwandte Dokumente

- [TECHNICAL_DEBT.md](../src/satellite/TECHNICAL_DEBT.md) — Audio Preprocessing, Echo Cancellation, Beamforming TODOs
- [AUDIO_CAPTURE_4MIC.md](AUDIO_CAPTURE_4MIC.md) — AC108 4-Mic Audio Capture (ReSpeaker HAT, wird durch XVF3800 ersetzt)
- [Satellite README](../src/satellite/README.md) — Bestehende Pi-Satellite-Dokumentation
- [SPEAKER_RECOGNITION.md](SPEAKER_RECOGNITION.md) — Speaker Recognition (funktioniert mit beiden Ansaetzen)
- [OUTPUT_ROUTING.md](OUTPUT_ROUTING.md) — TTS Audio Output Routing
- [SATELLITE_MONITORING.md](SATELLITE_MONITORING.md) — Satellite Monitoring Dashboard
- [WAKEWORD_CONFIGURATION.md](WAKEWORD_CONFIGURATION.md) — Wake Word Management