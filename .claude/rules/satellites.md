---
paths:
  - "src/satellite/**"
  - "tests/satellite/**"
  - "k8s/satellite-*.yaml"
  - "src/backend/ha_glue/services/satellite_*.py"
  - "src/backend/ha_glue/api/websocket/satellite_handler.py"
  - "src/backend/ha_glue/api/routes/presence.py"
---
# Satellites & voice capture

Loaded only when a satellite file is read. Trust/OTA signing lives in `satellite-trust-ota.md`.
Long form: `docs/design/ble-presence-improvement.md`, `docs/SATELLITE_ACOUSTIC_COMMISSIONING.md`,
`docs/SATELLITE_OTA_UPDATES.md`, `docs/XVF3800_SATELLITE.md`.

## Never
- **Never run `journalctl` scans or `journalctl -f` over SSH on a Pi Zero 2 W satellite.** Two such sessions on
  2026-09-19 were each followed by the device dropping off the network and rebooting (the capture loop must not be
  starved; an I2S overflow can crash the kernel). Track a voice turn from the backend log and the voice-server log
  (`faster_whisper: Processing audio with duration …` = the true recording length). Pi Zero SD cards are fragile:
  ask before restarting a satellite service.
- **Never derive a duration from a bounded container.** The turn clock is `_recorded_chunks`; until v1.4.9 the length of
  a buffer capped at 500 chunks (40.0 s) was used, so a limit above 40 s silently never fired.
- Never commit wake-word models, device MACs/IRKs, enrollment tokens or ambient captures (`data/wakeword-ambient/`).

## Permissions on a voice turn
- Recognised speaker → that user's permissions; unrecognised → `None`, which every gate reads as "no permission model"
  (#690 fail-open). `SATELLITE_ANONYMOUS_PERMISSIONS` (dark, empty = unchanged, never applied while auth is off)
  replaces it with a POSITIVE list over ALL MCP servers — an unnamed server is denied, read-only ones too.
- A denial is marked `permission_denied` and SPOKEN; swallowing it would let the model answer the refused question.

## Voice turn
- A turn ends on VAD silence after the grace period: `vad.min_listening_seconds` 2.0 s + `silence_duration_ms` 1.2 s,
  capped by `vad.max_recording_seconds` (fleet 60 s). `_recorded_chunks` and `vad.reset()` are zeroed at EVERY
  LISTENING entry, including `_on_server_state_change("listening")`.
- **Silero v5+ is a streaming model:** every 512-sample frame needs the previous 64 samples prepended (576 per call),
  over a gap-free stream — the 1280-sample chunk is not a multiple of 512, so the remainder carries over, never
  zero-padded. Bare frames score ~0.00 for clear speech (0/118 vs 114/118 chunks): every turn in the fleet was cut at
  ~3.3 s until v1.4.11. Log signature: voice-server durations all 3.1–3.4 s. The model is pinned by tag + sha256
  (`silero_vad_version` / `silero_vad_sha256` in `provisioning/group_vars/satellites.yml`, same tag in the Dockerfile);
  before bumping run `tests/satellite/test_silero_vad_streaming.py` with `SILERO_VAD_MODEL` + `SILERO_VAD_SPEECH_WAV`.
- An inference error must fall back to RMS, never be judged: neutral 0.5 reads as SPEECH at threshold 0.5.
- `RATE_LIMITED` means ONE dropped frame, not a failed session — it must not reset a running turn (#1284).
- `wakeword.vad_gated` is off by default; turning it on makes the VAD a precondition for the wake word.

## A fleet setting has TWO sources
Ansible `src/satellite/provisioning/group_vars/satellites.yml` for the Pis **and** the ConfigMap inside
`k8s/satellite-esszimmer.yaml` for the pod. #1282 raised the recording limit only in group_vars; the pod kept the code
default of 15 s. Change `vad_*`, LED or audio values in both.

## Esszimmer = k8s pod, not a Pi
Orange Pi Zero 3W, node-pinned, privileged, `imagePullPolicy: Never`. **OTA cannot update it** (code lives in the
ephemeral container layer): build natively on the node, `ctr -n k8s.io images import`, `kubectl set image`, then bump
the tag in the manifest. Keep the node's disk below ~80 % — image GC deletes the unpullable image.

## BLE presence
Phones rotate their address; `ble/rpa.py` resolves it with the per-person IRK and **silently no-ops without
`cryptography`**. Fleet default: continuous scan + `ble_scan_interval=3` on ALL satellites (a report-rate asymmetry
biases room arbitration). Do not bond a phone to one satellite — it out-shouts its neighbours.

## Acoustic commissioning is mandatory
A new room is not live until its ambient audio is in the wake-word hard-negative set. The synthetic false-positive
metric lies by ~30×; mic-gain levers reduce false wakes, only room-specific negatives eliminate them.
