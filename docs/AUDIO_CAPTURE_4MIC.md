# ReSpeaker 4-Mic Array: Native Audio Capture Plan

## Status: Phase 1 Active, Phase 2 Planned

## Background

The ReSpeaker 4-Mic Array uses the AC108 quad-channel ADC codec. Hardware constraints:

| Property | Value |
|----------|-------|
| **Format** | S32_LE only (no S16_LE at hardware level) |
| **Channels** | 4 (one per microphone) |
| **ADC resolution** | ~24-bit in 32-bit container |
| **Sample rates** | 8-48 kHz |
| **Driver** | HinTak/seeed-voicecard (DKMS) |
| **ALSA card** | `seeed4micvoicec` |

### Critical: AC108 Kernel Crash Triggers

1. **dsnoop/dmix on capture device** — AC108 is capture-only. Using dmix/dsnoop in `.asoundrc` crashes the kernel driver immediately.
2. **Opening raw hw device with wrong format** — Opening `hw:seeed4micvoicec` with S16_LE/1ch triggers `ac108_set_clock` crash (I2S SYNC error). The device only accepts S32_LE/4ch.

---

## Phase 1: ALSA plughw Downconversion (Current)

Uses ALSA `plughw:` plugin layer to handle format/channel conversion in C:

```
AC108 hw device (S32_LE/4ch)
  → plughw: (format conversion S32→S16, channel mix 4→1)
    → Application gets S16_LE/1ch
```

### Configuration

**`.asoundrc`** (no dsnoop/dmix!):
```
pcm.capture {
    type plug
    slave.pcm "plughw:seeed4micvoicec"
}
```

**`satellite.yaml`**:
```yaml
audio:
  device: "capture"        # Matches ALSA plugin name, NOT raw hw device
  channels: 1
  beamforming:
    enabled: false
```

**PyAudio device matching**: The satellite's `capture.py` searches PyAudio device names by substring. ALSA plugin devices like `"capture"` appear as `"capture: - (default)"` in PyAudio. The raw hw device appears as `"seeed-4mic-voicecard: ..."`. Using `"capture"` ensures PyAudio routes through `plughw:` which handles format conversion safely.

### Pros
- Simple, works with existing code unchanged
- Format conversion in optimized C (ALSA)
- Minimal CPU overhead

### Cons
- Loses 4-channel information (averages all mics without spatial processing)
- No beamforming possible (needs multi-channel data)
- 16-bit truncation (marginal loss for speech)

---

## Phase 2: Native S32_LE/4ch Capture (Planned)

Captures at AC108's native format, converts in Python, enables 4-channel beamforming.

```
AC108 hw device (S32_LE/4ch)
  → PyAudio paInt32/4ch (raw capture)
    → NumPy: deinterleave to (1280, 4)
      → S32→S16 conversion (right-shift 16 bits)
        → 4-channel DAS beamforming → mono S16_LE
          → Downstream pipeline (wake word, VAD, WebSocket)
```

### Changes Required

#### 1. `config.py` — Add format_bits support
```python
@dataclass
class AudioConfig:
    format_bits: int = 16        # 16 or 32
    channels: int = 1            # 1 (mono) or 4 (native 4-mic)
    native_channels: int = 4     # Hardware channel count for conversion
```

#### 2. `capture.py` — Native format capture + conversion
```python
# In _start_pyaudio():
if self.format_bits == 32:
    pa_format = pyaudio.paInt32
else:
    pa_format = pyaudio.paInt16

self._stream = self._pyaudio.open(
    format=pa_format,
    channels=self.native_channels,  # 4 for AC108
    rate=self.sample_rate,
    input=True,
    ...
)

# In _pyaudio_capture_loop():
if self.format_bits == 32 and self.native_channels > 1:
    # S32_LE interleaved 4ch → deinterleave
    raw = np.frombuffer(audio_bytes, dtype=np.int32)
    multichannel = raw.reshape(-1, self.native_channels)  # (1280, 4)

    # Apply 4-channel beamforming
    if self._beamformer:
        mono_int32 = self._beamformer.process_multichannel(multichannel)
    else:
        # Simple average if no beamformer
        mono_int32 = multichannel.mean(axis=1).astype(np.int32)

    # S32 → S16 (right-shift 16 bits, upper bits contain signal)
    mono_int16 = (mono_int32 >> 16).astype(np.int16)
    audio_bytes = mono_int16.tobytes()
```

#### 3. `beamformer.py` — 4-mic circular array DAS

The ReSpeaker 4-Mic Array has microphones in a roughly square layout. Mic positions (approximate, need calibration on actual board):

```
      Mic 0 (top)
       ●
      / \
Mic 3 ●   ● Mic 1
      \ /
       ●
      Mic 2 (bottom)
```

New class: `BeamformerDAS4Mic`
- Takes 4-channel input `(samples, 4)`
- Computes per-channel delays based on steering angle and mic geometry
- Aligns all 4 channels and sums (constructive interference)
- Returns mono output

#### 4. Ansible host_vars — Update for native capture
```yaml
# satellite-fitnessraum.yml
audio_device: "seeed-4mic"      # Raw hw device name in PyAudio
audio_channels: 4               # Native 4 channels
audio_format_bits: 32           # S32_LE
beamforming_enabled: true       # Enable 4-channel beamforming
```

### Performance Budget (Pi Zero 2 W)

| Component | CPU per 80ms chunk | Notes |
|-----------|-------------------|-------|
| `np.frombuffer` + reshape | ~0.05ms | Creates views, no copy |
| 4-channel DAS beamforming | ~1-3ms | Estimated from 2ch (~0.5-1ms) |
| `>> 16` + `astype(int16)` | ~0.1ms | Vectorized shift |
| **Total** | **~1.5-3.5ms** | **~2-4% CPU on one core** |

### Data Rate Impact

| Stage | Bytes/chunk | Bytes/sec |
|-------|-------------|-----------|
| Raw capture (S32_LE/4ch) | 20,480 | 256 KB/s |
| After conversion (S16_LE/1ch) | 2,560 | 32 KB/s |
| WebSocket (base64) | 3,414 | 43 KB/s |

Convert as early as possible to minimize memory pressure on the 512MB Pi Zero 2 W.

### Memory Impact

With `MAX_AUDIO_BUFFER_CHUNKS = 500`:
- Raw buffer: 500 x 20,480 = **10 MB** (before conversion)
- After conversion: 500 x 2,560 = **1.3 MB**

Strategy: Convert in capture loop before buffering, so downstream only sees S16_LE/1ch.

---

## Downstream Consumer Requirements

All downstream consumers expect **S16_LE mono at 16kHz**. No changes needed downstream if conversion happens in `capture.py`:

| Consumer | Expected Input | File |
|----------|---------------|------|
| OpenWakeWord | `np.int16` mono | `wakeword/detector.py` |
| Silero VAD | `np.int16` mono → float | `audio/vad.py` |
| WebRTC VAD | Raw PCM bytes, 16-bit mono | `audio/vad.py` |
| AudioPreprocessor | `np.int16` mono | `audio/preprocessor.py` |
| WebSocket streaming | Raw PCM bytes (base64) | `network/websocket_client.py` |
| Server-side Whisper STT | Mono audio | Backend |
