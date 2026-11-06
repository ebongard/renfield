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
  persist it in `host_vars/satellite-<room>.yml` with **both**
  `aic3104_adc_hpf_enabled: true` (the task gate) and `aic3104_adc_hpf: "0.0045xFs"`.
  Setting only the value leaves the gate false and the playbook skips the task
  silently.
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
multi-channel**, runs the sanity gate, and stores the result plus a provenance
sidecar under `data/wakeword-ambient/<satellite>/`.

**Who holds the mic depends on `hat_type`, and the script branches on it** (read
from the inventory, never guessed). **The template decides, not the prose** —
the criterion is whether `provisioning/templates/asoundrc-<hat_type>.j2`
resolves its capture PCM to a `type dsnoop` or straight to a `hw:`/`plughw:`
device. "HAT mic" is *not* the criterion: `4mic` is a HAT and is exclusive.

| `hat_type` | Capture PCM resolves to | Capture | Service |
|---|---|---|---|
| `2mic`, `2mic-v2`, `whisplay` | `pcm.array` → `type dsnoop` | shared | keeps running |
| `4mic` | `plughw:seeed4micvoicec` | exclusive — `asoundrc-4mic.j2`: "Do NOT use dsnoop or dmix on the AC108 — they crash the kernel driver" | **stopped for the capture**, started again right after |
| `xvf3800-usb` | `hw:Array` via `plug` | exclusive — `asoundrc-xvf3800-usb.j2` omits `dsnoop`/`dmix` on purpose (SysV IPC ownership deadlocks PortAudio) | **stopped for the capture**, started again right after |

`tests/satellite/test_capture_room_ambient.py` re-derives this table from the
templates themselves, so a comment that drifts cannot move the behaviour.

On an exclusive mic the stop needs `--allow-service-stop` (or an interactive
`yes`): it is an intervention on a Pi Zero and the room is deaf until the
capture ends. `--allow-service-stop` **forces** the exclusive path from every
mode, including a mic the table calls shared — that is the escape hatch when a
classification turns out wrong on a device, with no script edit.

The restart is guaranteed, not hoped for:

- it hangs off a trap (EXIT/INT/TERM), so an aborted capture does not leave a
  mute satellite, and the mic is handed back **before** the minutes-long
  transfer;
- `on_exit` makes INT/TERM unreachable (`trap ''`, inherited as SIG_IGN by the
  `systemctl start` child) before touching the service, because the restart
  over ssh takes seconds, the first Ctrl-C therefore looks like a hang, and a
  second one would otherwise kill the restart in flight;
- it is retried up to **3 times** with a 5 s pause (this Wi-Fi is marginal; one
  dropped packet must not mean a deaf room) and confirmed with a single
  `systemctl is-active` — never a `journalctl` follow;
- the verdict distinguishes the two cases: stop confirmed and restart
  unconfirmed is `CRITICAL … the room is DEAF`; a stop that itself failed
  yields `WARNING … probably still running`, with the `is-active` command to
  check before travelling to the device. Both exit non-zero.

The sidecar records `hat_type` and `mic_capture_mode` for the capture. The
XVF3800 keeps its capture gain in chip flash rather than in ALSA, so there the
sidecar has no gain provenance and the script warns — note the
`xvf3800_tuning` value from `host_vars/satellite-<room>.yml` alongside the
capture (§2).

An unknown `hat_type`, a host without one, and a host that is not in the
Ansible inventory at all are refusals,
not guesses.

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

**Convert the timezone before you correlate anything.** The backend logs in
**UTC**; a capture's filename and mtime are in the satellite's **local** time.
Comparing them directly puts every wake two hours (CEST) off its true offset —
which is exactly the mistake that first made the Kinderbad capture look like it
contained no triggering audio at all. Verify with the pod's own log rather than
assuming:

```bash
kubectl --context renfield-private -n renfield get pods -o json \
  | python3 -c "import json,sys; print([p['status']['startTime'] for p in json.load(sys.stdin)['items'] if p['metadata']['name'].startswith('backend-')])"
kubectl --context renfield-private -n renfield logs deploy/backend --tail=100000 | head -1
```

The pod's `startTime` is UTC; if the first log line carries the same clock, the
logs are UTC too.

**Even with the timezone right, alignment is partial.** On the Kinderbad capture,
four logged wakes fall inside the window and exactly **one** (09:08:50 CEST,
t=1643 s) lands on an offline detection; the other three have no offline hit
within 120 s. The satellite gates a wake on VAD as well as the wakeword, so the
two signals are not the same event stream. Use an aligned hit as a bonus — a
named regression target — and judge the capture itself by the aggregate score
above.


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

**Detection signal.** Since #1386 every turn without text is classified. The
fingerprint of a false positive is `kind=vad_floor`: the device stopped on
silence and its VAD saw no speech. `vad_heard_speech` is the other case — the
VAD saw speech, STT returned nothing (unintelligible speech, or a loud
non-speech sound) — and `stt_error` is a failed STT call, not a wake at all.
From satellite v1.4.13 on the device itself reports in `audio_end` whether its
VAD saw speech in any chunk of the turn (`vad_speech_seen`), and that report
decides. An older satellite sends no such field; for it the length decides:
a stop at the earliest moment the VAD allows (2.0 s grace + 1.2 s silence =
3.12 s with the fleet values) counts as `vad_floor`. The log line shows which
rule applied (`vad_speech=true|false`, or `-` for the length rule). A
`vad_heard_speech` turn gets a short LED cue on the satellite (two magenta
blinks, solid magenta on the XVF3800 — a colour outside the selectable
`led.idle_color` set; no sound, night-dimmed like every pattern); a
`vad_floor` gets none. Watch it per room:

```bash
kubectl --context renfield-private -n renfield logs deploy/backend --since=12h \
  | grep "Empty voice turn (satellite)" | grep -oE "room=[^ ]+ kind=[a-z_]+" \
  | sort | uniq -c | sort -rn
```

or as a rate: `renfield_voice_empty_turn_total{source="satellite",kind="vad_floor"}`
(the counter has no room label; the log line and the `session_end` event in the
satellite history carry the room and the length). On a satellite before
v1.4.13 a `vad_floor` is not proof: a short command spoken entirely inside the
grace period ends at the floor too. With the report (`vad_speech=true` at
`audio=3.12s`) that case is a `vad_heard_speech`. If such turns pile up, check
whether the tail of the wake word itself reaches the first listening chunk —
the report counts the grace period, and a wake-word tail there would turn
false wakes into cues.

**Post-rollout watch (v1.4.13).** Before the fleet updates, note per room the
share of `vad_floor` vs `vad_heard_speech` among the empty turns (the log
command above, over a week); repeat it a week after the room's satellite runs
v1.4.13. Some shift from `vad_floor` to `vad_heard_speech` is expected (short
commands inside the grace period). A room whose `vad_floor` share collapses
while its wake-event count stays the same — many `vad_speech=true` at
`audio=3.12s` — points at the wake-word tail, not at more real speech: those
false wakes now light the cue. Then the first listening chunk(s) must be left
out of the report before the cue stays on.

A room that dominates the `vad_floor` count and has near-zero successful
sessions is not commissioned, whatever its provisioning status says. The floor
constants live in `ha_glue/services/voice_empty_turn.py`;
`tests/satellite/test_backend_vad_floor_matches_fleet.py` fails when the fleet
settings (group_vars) drift away from them.

---

## Fleet status

| Satellite | Hardware | Room ambient in the negative set | Notes |
|---|---|---|---|
| Fitnessraum | XVF3800 | yes (v3) | `PP_AGCDESIREDLEVEL=0.015`; still on v3, v5 available |
| Arbeitszimmer | WM8960 / Whisplay | yes (v3 + v4 audiobook) | `ALC Max Gain=4` |
| Wohnzimmer | 2-mic HAT | yes (v3) | still on v3, v5 available |
| Kinderbad | 2-mic HAT (AIC3104) | **yes (v5, 2026-09-05)** | ADC HPF enabled 2026-09-04; FPs persisted (25 genuine wakes in the following 9 h). Its 10-min June capture scored peak 0.127 (0 detections) — useless as a negative. The 45-min in-use capture scores 0.969 / 8 against v3; v5 brings that to 0.609 / 1. **v5 deployed + live-verified 2026-09-05** (backend `2026-09-05-renfield-de-v5`): 4 `completed` sessions in the first 1.6 h, versus 0 in 9 h under v3. |
| Esszimmer | Orange Pi / XVF3800 | yes (v3) | bare-metal via Ansible since 2026-11-06 (was a k8s pod until then); audio path aligned to Fitnessraum (ch0 select) 2026-07-10; still on v3, v5 available |

---

## Related

- `src/satellite/wakeword-training/README.md` — the full training recipe and the
  history behind each configuration choice
- `src/satellite/provisioning/README.md` — hardware provisioning (this gate runs
  after it)
- `docs/WAKEWORD_CONFIGURATION.md` — model registration and fleet rollout
- `bin/capture-room-ambient.sh` — the capture step, with its sanity gate
