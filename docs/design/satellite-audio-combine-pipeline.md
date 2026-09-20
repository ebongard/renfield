# Satellite Audio Capture — hardware-appropriate stereo→mono combine

**Status:** DESIGN (2026-07-07) → **BUILT** (`b0ef9001`, `capture.py` `combine`/`select_channel` with auto-derived legacy defaults; §9 Q2 resolved). Triggered by a live Fitnessraum (XVF3800) incident where the wakeword stopped firing.
**Scope:** the satellite capture pipeline (`src/satellite/renfield_satellite/audio/capture.py`) — how multi-channel hardware audio is reduced to the single mono stream the wakeword/STT require. Not the transport (C1) or the backend.

---

## 1. Problem — measured, not assumed

The Fitnessraum satellite (ReSpeaker **XVF3800** USB, Pi Zero 2 W) stopped detecting the wakeword. After ruling out AGC gain, wakeword threshold, the deployed code, USB/ALSA health, and a chip factory-reset, a direct capture measurement found the cause. Same spoken "Renfield", captured three ways:

| Capture path | loudest-300 ms RMS | verdict |
|---|---|---|
| XVF3800 **stereo, channel 0** (processed beam) | **3375** | strong, clean speech |
| XVF3800 stereo, channel 1 (AEC residual) | 1282 | weak (residual) |
| **Mono via ALSA `default`** — *what the satellite actually captured* | **25–39** | near-silence |

The hardware and the processed beam are healthy. But the satellite captures **mono** from ALSA `default`, and ALSA's 2→1 downmix of this device collapses the signal to near-silence. **The wakeword was being fed silence.** (Offline, every naive downmix of a clean stereo capture — ch0, ch1, average, difference — is strong (1282–3375); only the live `default` mono path silences, because the XVF3800's two USB channels are phase-related processed-beam + residual that cancel under its downmix.)

### Root cause
The XVF3800's two output channels are **not two microphones**. They are:
- **ch0 / Left** = the fully processed beam (hardware AEC + beamforming + noise-suppression) — the clean, mono-equivalent speech.
- **ch1 / Right** = the AEC *residual* (echo-canceller leftover), near-silence for speech.

Mixing them is meaningless, and for this device it cancels. The satellite must **select** the processed beam, not **downmix**.

### Why the pipeline does the wrong thing
The capture pipeline already has a stereo→mono **combine stage** — but the XVF3800 bypasses it:

- **2-mic ReSpeaker HAT:** `channels: 2` + `beamforming: true` → captures stereo, runs `BeamformerDAS.process_int16` (delay-and-sum of two *raw mics*) → enhanced mono. Correct.
- **AC108 4-mic (arecord):** `_arecord_capture_loop` (`capture.py:345-349`) does **channel selection** — `ch = s32[1::self.channels]` extracts one channel of four (ch1; ch0 is the AC108's silent reference), `>> 16` to S16. Correct, and the exact precedent this design generalizes.
- **XVF3800:** `beamforming: false` + `channels: 1` + `device: default` → the PyAudio path captures **one channel from ALSA `default`**, delegating the 2→1 reduction to ALSA's downmix. It never enters a satellite-side combine stage. **This is the flaw.**

So the fix is not to invent stereo capture — the pipeline already captures stereo and combines to mono for two of three hardware types. The fix is to route the XVF3800 through a combine stage too, with the method its hardware needs: **channel select**.

---

## 2. Hard constraint (bounds the design)

The **wakeword models (OpenWakeWord / microWakeWord) and STT are mono-only** — they cannot consume stereo. So the satellite MUST produce a single mono stream locally, before the wakeword. A "fully stereo pipeline" is not possible without retraining the wakeword, and streaming stereo to the backend would double bandwidth against C1's goal. Therefore the design keeps the pipeline **mono downstream of capture**; the only question is doing the stereo→mono step *correctly per hardware*.

---

## 3. Design — one capture, a pluggable combine

Generalize the capture around: **capture at the hardware's native channel count → apply a hardware-appropriate `combine` → emit mono 16 kHz S16.**

```
                      ┌──────────────── combine (config-driven) ────────────────┐
 native capture  ───► │  beamform     DAS of N raw mics        (2-mic HAT)       │ ───► mono S16 ─► wakeword / VAD / STT / transport
 (1..4 ch)            │  select       take channel K            (XVF3800, AC108) │
                      │  passthrough  already mono, as-is       (legacy mono)    │
                      └──────────────────────────────────────────────────────────┘
```

Three combine modes:
- **`beamform`** — existing `BeamformerDAS` over N raw mics (2-mic HAT). Unchanged.
- **`select`** — take channel `K` from the interleaved frame, drop the rest. New for the PyAudio path; the arecord path already does exactly this (generalize its logic).
- **`passthrough`** — input is already mono; emit as-is (legacy single-channel devices, byte-identical to today).

The XVF3800 becomes `channels: 2` + `combine: select` + `select_channel: 0`. It stops relying on ALSA downmix and flows through the same architecture as every other device.

### 3a. Coordinated chip routing (must agree with `select_channel`)
Channel select is only correct if ch0 deterministically carries the processed beam. The XVF3800 output routing is `AUDIO_MGR_OP_L` / `AUDIO_MGR_OP_R` (`(category, source)` pairs). Measured today: `OP_L = MUX_USER_CHOSEN_CHANNELS[8] 0` carries the strong beam (3375) and `OP_R = MUX_AEC_RESIDUALS` the residual — so ch0 is correct *now*, but the routing is non-standard and `CLEAR_CONFIGURATION` does **not** reset it. So provisioning must **pin `OP_L` to the processed-beam source and persist it** (`SAVE_CONFIGURATION 1`), so `select_channel: 0` is deterministic across resets/firmware. The exact category/source for "processed beam" is verified empirically (capture stereo, confirm ch0 RMS ≫ ch1) as part of the rollout, not guessed.

---

## 4. Config schema

`AudioConfig` (`config.py`) gains a `combine` field; `beamforming: true` maps to `combine: beamform` for back-compat.

```yaml
audio:
  channels: 2                 # native capture count
  combine: "select"           # beamform | select | passthrough
  select_channel: 0           # used when combine=select
  # beamforming: true         # DEPRECATED alias → combine: beamform (kept for back-compat)
```

Per-hardware defaults (set in `templates/satellite.yaml.j2` by `hat_type`, or host_var):

| hat_type | channels | combine | select_channel |
|---|---|---|---|
| 2mic / 2mic-v2 | 2 | beamform | — |
| xvf3800-usb | 2 | **select** | **0** |
| ac108 (4-mic) | 4 | select | 1 |
| whisplay / single-mic | 1 | passthrough | — |

Flag-off / unspecified → `passthrough` (or `beamform` when `beamforming: true`) = **today's behavior byte-identical**. Only the XVF3800 template default changes.

---

## 5. Code changes (small, one module)

1. **`config.py`** — add `combine: str = "passthrough"` + `select_channel: int = 0` to `AudioConfig`; loader reads `audio.combine`/`audio.select_channel`; `beamforming: true` back-compat → `combine="beamform"` (and forces `channels=2`, as today).
2. **`capture.py`** — the PyAudio consumer path gains a `select` branch: from the interleaved `int16` stereo frame, take `frame[select_channel::channels]` (mirrors the arecord path's `s32[1::channels]` at `capture.py:348`, minus the S32→S16 shift). Factor the combine into one `_combine(frame) -> mono` helper used by both the PyAudio and arecord loops so the three modes live in one place (removes the arecord path's ad-hoc extraction duplication).
3. **`templates/satellite.yaml.j2`** — emit `combine` + `select_channel` from `hat_type` defaults (the audio block already added `codec` for C1).
4. **`host_vars/satellite-fitnessraum.yml`** — `audio_channels: 2`, `audio_combine: select`, `audio_select_channel: 0`. (host_vars are gitignored — operator-local.)
5. **Provisioning (`provision.yml`)** — the XVF3800 tuning task pins `AUDIO_MGR_OP_L` to the processed-beam source + `SAVE_CONFIGURATION` (§3a), so ch0 is deterministic.

No backend change. No transport change. The mono contract downstream of capture is unchanged, so wakeword/VAD/STT/C1 are untouched.

---

## 6. Testing (mono-only, deterministic — no hardware needed for the unit tests)

- **Unit (`tests/satellite/`):** synthesize an interleaved stereo `int16` buffer with a known tone on ch0 and silence/residual on ch1; assert `combine=select, select_channel=0` returns exactly ch0; assert `beamform` still runs DAS; assert `passthrough` is identity; assert `select_channel` out of range fails loud. This is the coverage the original XVF3800 mono path never had.
- **On-device validation harness (the lesson from this incident):** before trusting the wakeword, measure. A `bin/` capture-and-RMS check (the one used to find this bug) captures a few seconds and reports per-combine RMS; the rollout gate is **combined-mono RMS on live speech ≫ ambient** (e.g. loudest-300 ms RMS > ~1000), not "the wakeword fired." Detection is validated *after* the audio path is proven.

---

## 7. Rollout — deliberate, and shaped by how this incident happened

The bug was found only after a live satellite was destabilized by an unrelated change. The rollout rules encode that lesson:

1. **Land the code change as a small PR** (config + combine + unit test), build a satellite image deliberately.
2. **Never validate on a live, working satellite.** Fitnessraum is already service-disabled from this investigation — it is the safe canary. Deploy there with the service down.
3. **Prove the audio path with the RMS harness first** (§6), *then* re-enable the service and test the wakeword. Do not infer the audio path from wakeword behavior — that inversion is what cost this whole investigation.
4. **Only after Fitnessraum passes** does the XVF3800 template default flip for the fleet; other XVF3800 sats re-provision in a maintenance window (deps/config land restart-free via `--tags`, the activating restart is explicit and confirmed).
5. Flag-off / other hardware unchanged and byte-identical throughout.

### 7a. Fleet capture audit — every hardware type (not just the XVF3800)

We discovered the XVF3800 was feeding the wakeword **silence** only because we finally measured the combined-mono RMS. Nothing else flagged it — the service was "healthy," the model loaded, USB/ALSA were fine, and it had run for days. **We have never put a number on the capture path of any other hat.** The 2-mic DAS beamforming, the AC108 `ch1` selection, and the Whisplay passthrough are all *assumed* good; none is *measured* good. A quieter version of the same class of bug (wrong channel, a beamformer that attenuates, a mis-wired mic) could be silently degrading detection on any of them.

So the XVF3800 fix is **phase 1 of a fleet-wide capture audit**, run per hardware type with the identical measured method:

| # | hat_type | combine to verify | audit check |
|---|---|---|---|
| 1 | xvf3800-usb | select ch0 | this design; RMS harness + wakeword |
| 2 | 2mic / 2mic-v2 | beamform (DAS) | measure combined-mono RMS on live speech; compare DAS output vs raw per-mic RMS — is the beamformer *helping* or attenuating? |
| 3 | ac108 (4-mic) | select ch1 | confirm ch1 is the loudest real mic (ch0 is the silent reference — verify that's still true); measure combined RMS |
| 4 | whisplay / single-mic | passthrough | measure raw mono RMS — confirm the single mic delivers real speech (Whisplay WM8960 ALC/noise-gate history makes this worth checking) |

Each hat: capture with the `bin/` RMS harness (§6) while someone speaks, confirm loudest-300 ms RMS ≫ ambient, and only then trust the wakeword. Where a hat measures weak, the new `combine` config makes the fix a one-line change (mode/channel), not a code change. The audit's output is a per-hardware "known-good combine + measured RMS" record checked into the hardware docs, so a future regression is caught by re-running the harness, not by a user reporting "the satellite does nothing."

---

## 8. Alternatives considered

- **ALSA-route quick fix** (a device-local `.asoundrc` mapping ch0→mono, point `device:` at it): restores Fitnessraum today with zero code, but it's a per-device config hack that leaves the fleet architecture wrong and the XVF3800 a special case outside the combine stage. Acceptable as a stopgap; not the fix. (If function is needed *before* the PR lands, do this, and treat it as temporary.)
- **Full stereo to the backend:** rejected — wakeword/STT are mono-only (§2) and it doubles C1 bandwidth for no gain.
- **Keep ALSA downmix, "fix" it with mixer weights:** brittle, device-specific, and still mixes a beam with a residual. Channel select is simpler and correct.
- **Software beamforming on the XVF3800's two channels:** meaningless — they aren't two mics (§1 root cause).

---

## 9. Open questions

1. **Exact `AUDIO_MGR_OP_L` category/source for "processed beam"** — to pin deterministically (§3a). Verify empirically during rollout; today ch0 already carries it.
2. ~~Unify the arecord path~~ **RESOLVED (implemented):** both the S16 PyAudio path and the S32 arecord path share `_select_mono` (dtype-preserving channel select); the AC108 stays byte-identical because `select_channel` and `combine` **auto-derive** the legacy defaults when unset (`combine`: beamforming→beamform / channels>1→select / else passthrough; `select_channel`: 4-mic→ch1, else ch0). Un-reprovisioned sats need no config change.
3. ~~**Deprecation of `beamforming:`**~~ **RESOLVED: the alias stays** (cheap; `beamforming.enabled: true` auto-derives `combine: beamform`, `capture.py`); templates migrate opportunistically, no work item.

**Source:** Fitnessraum XVF3800 wakeword incident 2026-07-07 (live capture measurements above; root-caused to the ALSA mono downmix of the processed-beam + AEC-residual). Related: `docs/design/voice-identity-wakeword-verification.md` (C1 transport, adjacent in the same audio path), `docs/XVF3800_SATELLITE.md`, `src/satellite/renfield_satellite/audio/{capture,beamformer}.py`, ReSpeaker XVF3800 host_control docs (`AUDIO_MGR_OP_L/R`, `CLEAR_CONFIGURATION`, `REBOOT`, `SAVE_CONFIGURATION`).
