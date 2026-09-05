# Satellite Acoustic Commissioning

**Every new satellite installation MUST record its room's ambient audio and be
represented in the wakeword model's hard-negative set before it is considered
live.** This is not tuning. It is a commissioning gate, on the same footing as
"the sound card is detected" and "the service connects to the backend".

A satellite that passes hardware provisioning but skips this gate is not
finished — it is a false-positive storm waiting for the room to be used.

---

## Why this is mandatory, not optional

A wakeword model only rejects what it was trained to reject. openWakeWord is a
tiny classifier over a frozen speech-embedding pipeline; it has no notion of
"that is just a fan". Rooms differ enormously — tiles, extractor fans, running
water, HVAC, a TV, a dishwasher — and each room's noise signature is an
independent chance to cross the detection threshold.

Three production incidents, each the same root cause:

| Date | Room | What happened | What did NOT fix it |
|---|---|---|---|
| 2026-06-30 | fleet-wide | `renfield_de` v1 measured ~16 FP/hr on synthetic speech, then false-fired **~500/hr** in real rooms. Constant wake to empty-transcription storm. | Threshold. A model scoring 0.90-0.99 on noise cannot be thresholded out without destroying recall. |
| 2026-07-06 | Arbeitszimmer | An audiobook on the room's speaker false-woke the satellite **~24x/day**; the model scored narration up to 97%. v3 had been hardened against room *ambient* but never against continuous media speech. | Cutting `ALC Max Gain` 7 to 4 halved it. It did not eliminate it. Only v4, retrained with the audiobook as a hard-negative, reached 0. |
| 2026-09-04/05 | Kinderbad | **25 wakes in 9 h** (~3/h), every one an empty transcription, zero successful sessions. | Enabling the ADC high-pass filter. It removed a real measurement artefact (a DC offset reading as `audio_rms` 1812 while the room actually sat at -55 dBFS) but the false wakes continued unabated. |

The pattern is consistent: **mic-gain and filter levers reduce false positives;
only room-specific hard-negatives eliminate them.** Reach for the levers first
because they are cheap and sometimes dominant (an over-gained XVF3800 array once
accounted for 100% of residual FP), but do not mistake a reduction for a fix.

The corollary is the expensive one: **the synthetic false-positive metric lies
by roughly 30x.** An offline number measured on synthetic speech tells you
almost nothing about the room. Only held-out real ambient from that room does.

---

## The gate

A satellite is acoustically commissioned when all six steps pass. Steps 1-3 are
per-satellite. Steps 4-6 are per-model, so several new rooms can be batched into
one retrain.

### 1. Capture-chain sanity

Before measuring anything about the room, prove the capture chain reports the
room and not itself.

```bash
ansible satellite-<room> -i src/satellite/provisioning/inventory.yml -m shell \
  -a "amixer -c 0 scontrols"
```

Two hard requirements:

- **No DC offset.** On the TLV320AIC3104 (`seeed2micvoicec` 2-mic HAT) the ADC
  high-pass filter defaults to `Disabled`, and the resulting DC bias dominates
  the signal. Set `ADC HPF Cut-off` to `0.0045xFs` on **both** channels and
  persist it via `aic3104_adc_hpf` in `host_vars/satellite-<room>.yml`.
  Symptom when missed: an implausibly high `audio_rms` in the fleet heartbeat
  with a quiet room, and all signal energy below 100 Hz.
- **No clipping, no dead channel.** Every channel must show a plausible level.

`bin/capture-room-ambient.sh` enforces both automatically and refuses the
capture otherwise. Do not train on a rejected capture.

### 2. Gain calibration

Set the capture gain to what the room actually needs, **before** capturing, so
the negatives match what the model will hear in production. Changing the gain
after capture invalidates the capture.

| Hardware | Lever | Notes |
|---|---|---|
| XVF3800 USB array | `PP_AGCDESIREDLEVEL` | Dominant FP knob. `0.03` amplified a noise floor to speech amplitude; `0.015` dropped peak activation 0.96 to 0.29. Persist with `xvf_host SAVE_CONFIGURATION 1`. |
| WM8960 HAT | `ALC Max Gain` | Max (7) amplified an audiobook to peak 0.98; `4` gave 0.09-0.71. |
| TLV320AIC3104 HAT | `PGA` (capture) | Plus the mandatory `ADC HPF Cut-off` above. |

Persist every value in the gitignored `host_vars/satellite-<room>.yml` so a
re-provision does not silently revert it. A gain that lives only on the device
is a gain you will lose.

### 3. Record the room — the mandatory step

```bash
bin/capture-room-ambient.sh satellite-<room> --minutes 45 --label commissioning
```

The script reads the satellite's live audio config, records **raw
multi-channel** off the shared `dsnoop` PCM (no service stop needed on HAT
mics), runs the sanity gate, and stores the result plus a provenance sidecar
under `data/wakeword-ambient/<satellite>/`.

**The capture must span the room's real acoustic events.** A quiet 10-minute
block captures a noise floor, and a noise floor is not what false-fires the
model. Capture while the room is used, or capture long enough to cover a full
usage cycle. Per room type, the events that matter:

| Room | Must be in the capture |
|---|---|
| Bathroom | Running water, shower, toilet flush, extractor fan, door, tiles reverberating |
| Kitchen | Extractor hood, dishwasher, running water, crockery, radio |
| Living room | TV and music at normal volume, conversation, doors |
| Office | Keyboard, fans, phone/video calls, **audiobooks or podcasts** |
| Bedroom | HVAC, night quiet, radio alarm |

Anything that plays **continuous human speech** in the room (TV, radio,
audiobooks, podcasts) is the single highest-risk category and must be captured
explicitly. Hours of varied speech means a phonetic near-miss eventually crosses
threshold. This is what caught the Arbeitszimmer.

Repeat with different `--label` values to build a corpus. More material from
more acoustic states is strictly better.

**Prove the capture is provocative before you train on it.** This is the real
acceptance test for a capture, and it is cheap: score it with the model that is
currently deployed. Material the model never reacts to teaches it nothing.

```bash
python src/satellite/wakeword-training/scripts/score_wav.py \
    <deployed-model>.onnx <capture>-mono.wav
```

Kinderbad, 2026-09-05, scored against the deployed `renfield_de` v3:

| Capture | Peak score | Detections |
|---|---|---|
| The 10-min capture already in the v3 corpus (June, quiet) | **0.127** | **0** |
| The 45-min in-use capture (September) | **0.969** | **8** (~11/h) |

That is the whole argument for this gate in one table. Kinderbad *was* already
represented in the negative set — with ten quiet minutes the model never reacted
to, which is why v3 learned nothing about the room and kept false-firing.
**A capture that peaks below ~0.3 is not usable hard-negative material.**

**Counting false positives in the log — use the right line.** A false positive
is the `Wake word '<model>' detected` line. Do NOT grep for `session:`: satellite
re-registrations also carry a `DB session:` line, which inflates the count.

```bash
kubectl --context renfield-private -n renfield logs deploy/backend --since=8h --tail=80000 \
  | grep "Wake word '" | grep -oE "detected by sat-[a-z]+" | sort | uniq -c
```

**Do not expect wall-clock alignment.** It is tempting to correlate a logged wake
with an offset in the capture and call that the triggering audio. On Kinderbad
that did not hold: the one wake logged during the capture window (09:02:55, i.e.
t=1288s) scores **0.001** offline in every channel variant — beamformed, either
raw channel, and the plain downmix. Clocks were verified synchronized and the
detector path was verified to take the beamformed mono unmodified, so neither
explains it; ALSA capture drift on a loaded Pi Zero is the leading suspect but is
unproven. Judge a capture by the aggregate score above, not by frame alignment.

**Privacy.** These recordings capture whatever is said in the room. Treat them
as private household data: `data/wakeword-ambient/` is gitignored, keep the
corpus on trusted machines, and delete captures once the model is trained and
validated. Tell the household that a commissioning capture is running.

### 4. Derive the detector-side mono

```bash
python src/satellite/wakeword-training/scripts/derive_detector_mono.py \
    data/wakeword-ambient/satellite-<room>/*.wav
```

The detector never sees the raw channels. Each satellite collapses them to mono
differently — Delay-and-Sum beamforming on a 2-mic HAT, channel select on the
XVF3800, a plain mean elsewhere — and this script replays the satellite's own
collapse using the satellite's own beamformer code.

Training on the wrong collapse silently poisons the negative set: on a
beamforming satellite, an ALSA mono downmix is a *different signal* from the one
the model scores.

### 5. Retrain with the room as a hard-negative

Follow `src/satellite/wakeword-training/README.md`. In short: drop the derived
mono wavs into `/work/ambient/` **alongside the existing corpus** (never replace
it — every previously-commissioned room must stay represented), then

```bash
python scripts/gen_hard_negs.py     # 75/25 split by time, no leakage
./scripts/run_train_de.sh           # ambient as hard_negative class
                                    # AND as false_positive_validation_data_path
```

The 75/25 time split matters: the first 75% becomes windowed training
hard-negatives, the last 25% becomes **held-out** FP validation. Validating on
data the model trained on reproduces the exact lie this whole procedure exists
to prevent.

### 6. Validate, then sign off

```bash
python scripts/validate_ambient.py   # held-out FP rate + recall on positives
```

Acceptance criteria:

| Metric | Threshold | Measured on |
|---|---|---|
| False positives | **0/h** at the deployed threshold | the new room's **held-out** ambient |
| Recall | no worse than the previous model (currently ~70-75%) | positive clips |
| Live recall | a spoken "Renfield" detected from normal positions in the room | the actual room, by a human |

The live check is not a formality. Synthetic TTS-on-TTS recall understates real
recall, and an offline number has never once been sufficient. Equally, a
satellite that stops false-firing because it stopped hearing anything has failed,
not passed — confirm both directions.

Record the outcome (model version, md5, per-room FP rate) in the training README
before considering the room live.

---

## Retrofit and drift

Commissioning is not once-forever. Re-run steps 3-6 when:

- a satellite moves to a different room, or the room's furnishing changes materially
- a new continuous-speech source appears in the room (a TV, a smart speaker, an audiobook habit)
- the mic hardware or the capture gain changes
- the room starts showing wakes with empty transcriptions

**Detection signal.** A wake followed by an empty transcription is the
fingerprint of a false positive: the model woke, recorded, and there was no
speech to transcribe. Watch it per satellite:

```bash
kubectl --context renfield-private -n renfield logs deploy/backend --since=12h \
  | grep "empty_transcription" | grep -oE "sat-[a-z]+" | sort | uniq -c | sort -rn
```

A room that dominates that count and has near-zero successful sessions is not
commissioned, whatever its provisioning status says.

---

## Fleet status

| Satellite | Hardware | Room ambient in the negative set | Notes |
|---|---|---|---|
| Fitnessraum | XVF3800 | yes (v3) | `PP_AGCDESIREDLEVEL=0.015` |
| Arbeitszimmer | WM8960 / Whisplay | yes (v3 + v4 audiobook) | `ALC Max Gain=4` |
| Wohnzimmer | 2-mic HAT | yes (v3) | |
| Kinderbad | 2-mic HAT (AIC3104) | **yes (v5, 2026-09-05)** | ADC HPF enabled 2026-09-04; FPs persisted (25 genuine wakes in the following 9 h). Its 10-min June capture scored peak 0.127 (0 detections) — useless as a negative. The 45-min in-use capture scores 0.969 / 8 against v3; v5 brings that to 0.609 / 1. **v5 not yet deployed; live recall unverified.** |
| Esszimmer | Orange Pi / XVF3800 | yes (v3) | parked, needs new hardware |

---

## Related

- `src/satellite/wakeword-training/README.md` — the full training recipe and the
  history behind each configuration choice
- `src/satellite/provisioning/README.md` — hardware provisioning (this gate runs
  after it)
- `docs/WAKEWORD_CONFIGURATION.md` — model registration and fleet rollout
- `bin/capture-room-ambient.sh` — the capture step, with its sanity gate
