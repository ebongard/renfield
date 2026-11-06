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

🛑 **Ein ausgefallener Satellit faellt NICHT von selbst auf.** Bis 2026-09-26 gab es
dafuer nichts: die halbe Flotte war dunkel (Esszimmer 1 Tag, Arbeitszimmer 2 Tage,
BensZimmer 30 Tage) und keine einzige Benachrichtigung. Jetzt meldet die stuendliche
Aufgabe `satellite_fleet_watchdog` die Fehlenden. Das Lebenszeichen ist die LAUFENDE
Registratur des `SatelliteManager`, NICHT `satellites.last_authenticated_at` — die
Spalte wird nur beim Verbindungsaufbau gesetzt und altert bei einem stabil verbundenen
Geraet, waehrend es quicklebendig ist. Details: `.claude/rules/scheduled-tasks.md`.


Loaded only when a satellite file is read. Trust/OTA signing lives in `satellite-trust-ota.md`.
Long form: `docs/design/ble-presence-improvement.md`, `docs/SATELLITE_ACOUSTIC_COMMISSIONING.md`,
`docs/SATELLITE_OTA_UPDATES.md`, `docs/XVF3800_SATELLITE.md`.

## Never
- **Never run `journalctl` scans or `journalctl -f` over SSH on a Pi Zero 2 W satellite.** Two such sessions on
  2026-09-19 were each followed by the device dropping off the network and rebooting (the capture loop must not be
  starved; an I2S overflow can crash the kernel). Track a voice turn from the backend log and the voice-server log
  (`faster_whisper: Processing audio with duration …` = the true recording length). Pi Zero SD cards are fragile:
  ask before restarting a satellite service.
- **Never let the turn-end REASON die in a log line.** `audio_end` carries `silence` | `button` | `timeout`;
  `timeout` = the cap cut the utterance MID-SENTENCE, and only logging it made a truncated recording
  indistinguishable from a finished sentence (7 cases in 7 days). `truncation_notice()` prepends a DETERMINISTIC
  hint — never a prompt addition — and `turn_end_reason` rides in the `session_end` event.
- **Never derive a duration from a bounded container.** The turn clock is `_recorded_chunks`; until v1.4.9 the length of
  a buffer capped at 500 chunks (40.0 s) was used, so a limit above 40 s silently never fired.
- Never commit wake-word models, device MACs/IRKs, enrollment tokens or ambient captures (`data/wakeword-ambient/`).

## Permissions on a voice turn
- Recognised speaker → that user's permissions; unrecognised → `None`, which every gate reads as "no permission model"
  (#690 fail-open). `resolve_anonymous_identity()` replaces that None under auth-on, and **exactly one** of two
  mechanisms owns the turn: `SATELLITE_DEVICE_ACCOUNT` (dark) names a user — then its ROLE is the grant set and the
  list below is not read; otherwise `SATELLITE_ANONYMOUS_PERMISSIONS` (dark) supplies a POSITIVE list over ALL MCP
  servers — an unnamed server is denied, read-only ones too. Both empty = unchanged; neither applies while auth is off.
- A device account is an identity, not a person: `users.is_device_account` (the flag is the contract, never the name)
  blocks memory extraction and presence booking wherever the identity came from — the speaker lookup reads it too, so
  linking a speaker to a device account cannot smuggle them back in. A configured device account that does not resolve
  (unknown name, user without the flag, DB hiccup) DENIES the turn; widening silently would be worse than a refusal.
- **`get_permissions()` needs `selectinload(User.role)`.** It reads the `role` relationship, and an async session
  raises `MissingGreenlet` on a lazy load — inside the session too. Both permission lookups swallow exceptions, so the
  failure mode is not a traceback but a recognised speaker with no id and no rights: no presence, no extraction.
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

## One source for fleet settings
Every satellite, the Orange Pis included, is provisioned from Ansible
`src/satellite/provisioning/group_vars/satellites.yml` + host_vars. Until 2026-11-06 the Esszimmer pod had its own
ConfigMap, and #1282 raised the recording limit only in group_vars (the pod kept 15 s); a second source must not
come back.

## Orange Pi Zero 3W: bare-metal like the Pis (both since 2026-11-05/06, no satellite in k8s any more)
Why: OTA cannot update a pod (code lives in the ephemeral container layer), and a cluster node with an always-on mic
needs 6443/10250/VXLAN out of the IoT VLAN. Bare-metal = the Pi playbook with `satellite_platform: orangepi` in
host_vars: it skips `/boot/firmware/config.txt`, uses Ubuntu groups via `satellite_hw_groups` and adds the Orange Pi
host tasks (CPU cap 1.2 GHz, conditional XVF3800 USB re-enumeration at boot, logs on disk). A fresh board first gets
`provisioning/orangepi-first-boot.sh` as root (fresh machine-id + SSH host keys, the ansible user); camera:
`provision-camera.yml` (only an IMX219 works, a Camera Module 3/IMX708 never enumerates).
Traps: systemd refuses a unit naming a missing group (`216/GROUP`); the XVF3800 udev rule needs its add event replayed
without the Pi's reboot (handler `apply XVF3800 udev rule`, else `xvf_host` "Failed to open device", ring dark);
`hey_renfield.onnx` is not in git — copy it from another satellite. **Leaving a cluster:** reset ON the node first
(disable kubelet, `kubeadm reset`, disable containerd), delete the Node object, then reboot to clear the iptables
(nothing persists them there). Deleting the Node first kills calico + kube-proxy while kube-proxy's DNAT of
192.168.1.230:443 stays, so the satellite loses the server; and a still-enabled kubelet re-registers the node on the
next boot. Only then the IoT VLAN, else the switch gets a .90.x lease and still fails the 443 check.
**XVF3800 ring: two scales.** Fleet brightness (config, night-dimming push) is the APA102's 0..31; the chip's
`LED_BRIGHTNESS` is 0..255 and dims only breath/rainbow, never solid colours. Passing 0..31 through left the breathing
idle ring dark on every XVF3800 until 1.4.16 — `XVF3800LEDController.chip_brightness()` converts. Idle = blue breath at
`LED_SPEED` 1; yellow is PROCESSING, so no room colour for idle.

## BLE presence
Phones rotate their address; `ble/rpa.py` resolves it with the per-person IRK and **silently no-ops without
`cryptography`**. Fleet default: continuous scan + `ble_scan_interval=3` on ALL satellites (a report-rate asymmetry
biases room arbitration). Do not bond a phone to one satellite — it out-shouts its neighbours.

## Acoustic commissioning is mandatory
A new room is not live until its ambient audio is in the wake-word hard-negative set. The synthetic false-positive
metric lies by ~30×; mic-gain levers reduce false wakes, only room-specific negatives eliminate them.
**Who may hold the mic is a property of `hat_type`, and THE TEMPLATE DECIDES, NOT THE PROSE.** The criterion is whether
`provisioning/templates/asoundrc-<hat_type>.j2` resolves its capture PCM to a `type dsnoop` (`2mic`, `2mic-v2`,
`whisplay` → shareable) or straight to a device (`4mic` → `plughw:` because dsnoop/dmix crash the AC108 driver;
`xvf3800-usb` → `hw:Array`, dmix/dsnoop omitted on purpose → both EXCLUSIVE). "HAT mic" is NOT the criterion — #1388
first shipped with `4mic` in the shared column because the table was written against that prose. On an exclusive mic
`arecord` and the running service cannot both open the device, so the capture must stop `renfield-satellite` first.
`bin/capture-room-ambient.sh` reads `hat_type` from the inventory and branches; the stop is never silent
(`--allow-service-stop` or an interactive `yes`, and that switch FORCES the exclusive path from every mode — the
escape hatch when a classification is wrong on a device). A satellite left deaf by an aborted capture is worse than a
failed capture, so the restart hangs off a trap (EXIT/INT/TERM), `on_exit` first sets SIG_IGN for INT/TERM (inherited
by the `systemctl start` child — a second Ctrl-C is the REGULAR case, the slow ssh restart looks like a hang), retries
3× with a pause, and is confirmed with ONE `systemctl is-active`. **DEAF is only claimed when the STOP was confirmed**;
a failed stop yields a WARNING that the service is probably still running — the wrong verdict costs a trip.
