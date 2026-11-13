# BLE Presence Improvement

Improve household BLE presence (accuracy, latency, reliability), prompted by the
Esszimmer Orange Pi Zero 3W (Allwinner A733, **Bluetooth 5.4**) being a much
stronger BT radio than the Pi-Zero fleet — and a good primary anchor.

## Current state (before this work)
- Satellite `ble/scanner.py`: a periodic `BleakScanner.discover()` burst every
  `scan_interval` (30 s) for `scan_duration` (5 s); reports `{mac, rssi}` for
  devices in a `known_devices` MAC whitelist above `rssi_threshold`. Backend
  aggregates per-room RSSI for arbitration.
- `classic_rssi: false` on the A733 board (AIC8800 raw-HCI `hcitool cc/rssi` is
  broken; advertisement RSSI is real and used).

## Problems (what limits accuracy)
1. **MAC randomization (root limiter).** Modern phones/watches rotate their BLE
   address (RPA), so a static MAC whitelist drifts. **Not fixed by any BT version.**
2. **Scan latency / coverage.** 5 s scan every 30 s → up to ~30 s latency; a
   device advertising in the gap is missed.
3. **RSSI jitter.** Single-shot advertisement RSSI is noisy → room arbitration
   flip-flops (cf. the Kinderbad synthetic-−50 hijack incident).

## What the BT 5.4 controller offers (probed on the A733)
- ✅ LE **2M** + **Coded (Long Range)** PHY (`LECODEDTX/RX`), Extended
  Advertising, address privacy.
- ❌ **No usable AoA/AoD direction-finding** (no CTE exposed; needs a multi-antenna
  array). No angle-based positioning.

## Plan (phased)

### Phase 1 — Continuous, smoothed scanning  ✅ IMPLEMENTED (this change)
- **BlueZ `Experimental = true`** codified in the satellite Ansible
  (`provision.yml` → `ini_file` on `/etc/bluetooth/main.conf`, restarts
  bluetoothd). Unlocks the AdvertisementMonitor/passive APIs and survives a
  re-image. *(Already set live on the Esszimmer host.)*
- **Continuous scanning** in `ble/scanner.py` (`continuous: true`): one
  long-running `BleakScanner` with a detection callback instead of discover()
  bursts; per-device **EWMA-smoothed RSSI** with a freshness window
  (`smoothing_alpha`, `freshness_seconds`). The scan loop polls
  `get_readings()` each `scan_interval`. Falls back to the periodic discover()
  path when `continuous: false` (default → fleet byte-identical).
- Config plumbed through `BLEConfig`, `load_config`, the Ansible template +
  group_vars (`ble_continuous` etc.).
- **Latency** reduced by `scan_interval 30→3`. **Fleet-equalised 2026-07-10**
  (`group_vars`: `ble_scan_interval 30→3` + `ble_continuous true` for ALL sats;
  was only the A733/Esszimmer before). **The report loop `sleep(scan_interval)`
  gates the report cadence in BOTH modes** — continuous only steadies RSSI (EWMA),
  it does NOT lower the report rate. So the 3s-Esszimmer / 30s-Pi split meant a
  10× report asymmetry that biased room-arbitration toward the chattier satellite
  (measured root cause of the "phone in Wohnzimmer shows Esszimmer" mislocation;
  diagnosed via the read-only `GET /api/presence/debug/sightings` + an RSSI-over-
  time chart). Equalising cadence fixed it; continuous makes the 3s poll near-free
  (in-memory snapshot, no extra radio bursts — verified on Pi Zero 2 W BT 4.2).

### Phase 1b — AdvertisementMonitor passive offload  ⏳ DEFERRED
Use BlueZ passive scanning (`scanning_mode="passive"` + `or_patterns`, kernel
RSSI-threshold offload) rather than active continuous scan. Lower power/CPU;
needs Experimental (now enabled). Follow-on to the continuous callback above.

### Phase 1c — Backend RSSI smoothing + hysteresis  ✅ SHIPPED (shared backend)
Asymmetric EWMA (fast attack / slow release) per room + margin hysteresis in the
room-arbitration logic (`ha_glue/services/presence_service.py`, "#10 asymmetric
RSSI filter + margin hysteresis").

> **STATUS 2026-06-19: SHIPPED + DEPLOYED.** Phase 1 (continuous scan + BlueZ
> Experimental) and Phase 2 (IRK store + RPA resolution + UI pairing flow) are
> merged (#825/#826/#828/#829) and live: backend IRK store deployed, the
> Esszimmer Orange Pi satellite resolves an iPhone via BLE, and the BLE stack is
> rolled out to the Pi fleet (multi-satellite room arbitration active). Phase 1c
> shipped later in the backend; Phases 1b/3 remain deferred.

### Phase 2 — Defeat MAC randomization via IRK-based RPA resolution  ✅ SHIPPED + DEPLOYED (the real win)
**Corrected mechanism** (the original "bond the phone to the satellite" is
infeasible — iOS/Android won't expose themselves for passive bonding). Instead,
the same approach Home Assistant's *Private BLE Device* / Bermuda use, which is
reliable with iPhones and needs **no new hardware and no app**:

- **Obtain the IRK out-of-band, once per person.** An iPhone's IRK lives in the
  owner's **Mac / iCloud keychain**; an Android's in its bonded-device info. No
  pairing to the satellite.
- **Resolve the rotating RPA in software.** Given the IRK, each advertised
  random address is checked with the BLE `ah` hash (AES-128) → matches map the
  rotating address back to a stable identity. Advertisement-scanning only — no
  raw HCI, no Classic-BT, no bonding — so it **works on the AIC8800 board**.

**Built (this change):** `ble/rpa.py` (spec-validated `ah` resolution),
`BLEScanner` IRK routing (`update_irks` / resolve in the continuous + periodic
paths → presence keyed by resolved identity), config plumbing (`ble.irks`,
name→hex), `cryptography` dep, unit tests (incl. the BT spec vector).

**Remaining:** backend per-person IRK store (encrypted) + push to satellites
(like the known-devices list); enrollment flow + documented Mac-export step;
privacy review for storing IRKs; live end-to-end proof with one real IRK.

> **Byte-order gotcha (fixed #840, 2026-06-22).** BlueZ stores the
> `IdentityResolvingKey` in `/var/lib/bluetooth/.../info` **least-significant-octet
> first**, but `ble/rpa.py` and the backend IRK store expect it **MSO-first** (the
> BLE-spec `ah` order). The UI pairing-capture reader (`_read_bonded_irks`) read
> the key as-is, so a captured IRK was stored byte-swapped and **silently never
> resolved** a rotating address — masked because a *bonded* satellite resolves the
> phone natively via BlueZ, and because the first captured IRK never persisted to
> the backend until the bug was hit live. `_read_bonded_irks` now reverses at that
> single boundary (the manual `POST /api/presence/irks` path already takes
> MSO-first hex, so both converge). Proven: the device's real advertised RPA
> resolves only against the reversed bytes.

> **Silent-no-op gotcha (fixed 2026-06-22).** `ble/rpa.py` guards on
> `_CRYPTO_AVAILABLE` and **returns `False` for every address** when the
> `cryptography` package isn't importable — so a satellite missing the dep
> receives IRKs, scans, sees the phone's RPA at −40 dBm 1 m away, and resolves
> *nothing*, with no error. `cryptography` lived only in `satellite_python_packages`
> (installed under the `[python]` tag); the safety code-only `--tags app` deploy
> skips that tag, so the dep was never installed on **any** bare-metal satellite
> and software IRK resolution was dead house-wide — presence only ever worked on
> the Esszimmer, which resolves the *bonded* phone natively via BlueZ and bypasses
> the software resolver. Fix: a dedicated `[python, app]` pip task + `cryptography`
> in the satellite `requirements.txt` + a loud startup warning when IRKs arrive
> while `_CRYPTO_AVAILABLE` is False. Verified live: installing `cryptography` and
> restarting made arbeitszimmer resolve the phone and presence flip to Arbeitszimmer.

> **Bonded-satellite arbitration bias — un-bond to fix (2026-06-22).** Once every
> satellite could resolve the phone, room assignment in **adjacent / open-plan
> rooms became wrong**: standing in the Wohnzimmer, presence showed **Esszimmer**.
> Cause: the phone was *bonded* on the Esszimmer (Orange Pi), so BlueZ resolved it
> **natively on every advert** — a strong, near-constant signal (~20 matches/min)
> — while the non-bonded Wohnzimmer (weak Pi-Zero radio + software IRK) caught only
> a trickle (~2/min). `_assign_room`'s RSSI arbitration therefore picked the
> Esszimmer even with the user in the Wohnzimmer. The match *count* isn't the
> arbitration input (it's smoothed RSSI), but native bonding makes the bonded
> satellite report a consistently strong signal that a weaker neighbour can't beat.
>
> **Fix: un-bond the phone so all satellites use the same software IRK path.**
> `bluetoothctl remove <identity-mac>` on the bonding satellite's **host** (the bond
> is in the host's `/var/lib/bluetooth`; the k8s pod mounts it RO and cannot remove
> it). The IRK is already stored in the backend (`user_ble_irks`), so the formerly-
> bonded satellite keeps detecting via the software resolver — only the unfair
> native edge is gone, so it reports *real* advert RSSI like everyone else. Verified
> live: after un-bonding, presence tracked the actual room **bidirectionally**
> (Arbeitszimmer↔Wohnzimmer), held stably with no flicker. Reversible — re-pair via
> the IRK pairing flow if a satellite genuinely needs native resolution. The general
> lesson: **don't bond the phone to only one of several overlapping satellites** —
> either bond none (software IRK everywhere, fair RSSI) or accept that the bonded
> one will dominate its RF neighbourhood.

> Classic-BT (BlueZ connection-RSSI) was evaluated and rejected for iPhones —
> no API to poll an iPhone over BR/EDR and iPhones aren't Classic-discoverable.

### Phase 3 — Optional reach  ⏳ DEFERRED
Coded-PHY (Long Range) scanning for compatible tags/beacons; connection-based
RSSI for bonded devices.

## Non-goals
Direction finding / angle positioning (hardware can't); UWB.

## Rollout & metrics
Designed fleet-wide; validated on the Esszimmer A733 as the strongest anchor,
then rolled to the Pi fleet via Ansible (`--tags app`). Older radios still gain
continuous scan + smoothing (and later IRK). Metrics: presence-detection
latency, room-arbitration flip rate, % time known devices resolved after MAC
rotation.

## Live readings view (admin diagnostic)

The numbers behind a room decision used to be reachable only as JSON
(`GET /api/presence/debug/sightings`). The **Readings** tab on `/admin/presence`
(`components/presence/LiveReadings.tsx`) shows them live: per tracked device the
person, the room the system places them in and its confidence; one row per
satellite with latest / mean / min–max RSSI, reading count, age and a trend
line over the sighting window; connected satellites that did NOT hear the
device ("not heard" — as telling as a weak reading); and per room the filtered
value with the threshold a challenger needs (`current + PRESENCE_SWITCH_ENTER_MARGIN_DB`).

- **One payload builder** (`ha_glue/services/presence_debug.py::build_sightings_payload`)
  serves the REST route and the socket. It adds `enabled`, `settings`
  (`rssi_floor_dbm`, `switch_enter_margin_db`, `stale_timeout_s`,
  `rssi_filter_enabled`), `satellites` (connected roster, in-memory) and per
  device `filtered_rooms` (id-keyed) to the earlier response; nothing was removed.
- **Push, not poll:** ADMIN-gated `/ws/presence-debug`
  (`ha_glue/api/websocket/presence_debug_handler.py`) sends one `snapshot` on
  connect and a fresh one after BLE reports, coalesced to at most one per second.
  `process_ble_report` calls `note_ble_report()`, which returns at once while no
  viewer is connected — the diagnostic costs nothing unwatched. Frontend:
  `hooks/usePresenceReadings.ts` (the only timer is the reconnect backoff).
- **Gate:** ADMIN on both, fail-closed (a permission lookup error refuses the
  socket). The payload is every tracked person's movement trace; the device
  `key` stays a redacted hash, never the raw MAC or the IRK label.
- **A refusal is readable, and only a confirmed one is final:** the handler
  accepts the handshake and then closes with an application code. Closing
  before `accept` answers the upgrade with HTTP 403, which a browser reports as
  1006 — the page could not tell "denied" from "network down". Two codes, both
  denying, nothing sent on either: `WSAuthError.FORBIDDEN` (4403) = the user was
  loaded and is not an admin → the hook shows "no access", drops any snapshot
  and stops; `WSAuthError.UNAUTHORIZED` (4001) = the server could not establish
  who is asking (no/expired token, no user, lookup error) → possibly temporary,
  the hook retries with backoff ("reconnecting"). The hook also never opens a
  token-less socket after a FAILED token request (`requestWsToken` keeps a
  failure apart from the faucet's "no token needed"); "live" and the backoff
  reset wait for the first snapshot, since an accepted handshake proves nothing.
- **Maximum socket lifetime** (`MAX_SESSION_SECONDS`, 15 min): the ADMIN check
  runs at the handshake only, and this stream is personal data. The server
  closes normally (1000); the hook reconnects at once, without a status change
  or backoff growth, and the new handshake checks the role again. A normal
  close from a socket that never delivered a snapshot counts as a drop
  (ordinary backoff), so it cannot drive a reconnect loop.
- **A dropped viewer is closed** (1011), not merely unregistered — otherwise the
  tab would sit on an open, silent socket showing "Live" with frozen numbers.
- **Voice hold / pin** (see "Voice hold" and "After the hold" below): per device
  `voice_hold_remaining_s`, `voice_pinned`, `location_uncertain`
  (`PresenceService.is_location_uncertain`, not a copy of its rule); in
  `settings` `voice_hold_s` and `voice_release_margin_db`. The view shows "held
  by voice for N s" (a challenger that has met the margin then reads "blocked by
  the hold", never "takes over"), "held guess", and — for a pinned room the
  device was never heard in — the threshold `floor + release margin` instead of
  the enter margin, mirroring `_should_switch_filtered`.
- **Screen readers:** only the connection status is a live region; the "as of"
  time changes every second and stays outside it.
- **Limits:** sightings live in the memory of the pod that holds the satellite
  sockets, so the view shows that pod's state (same as the REST route). A device
  that fell silent keeps its last readings until the next report for it prunes
  them; the age column makes that visible. No new flag — like the REST route it
  follows `PRESENCE_ENABLED` only.

## Open questions
- Bond per-satellite vs central IRK distribution?
- Audit: which household devices use stable vs randomized addresses today?
- Privacy/consent model for storing IRKs (ties into household privacy posture).

## Background moved from CLAUDE.md (2026-09-20)

Moved verbatim when CLAUDE.md was split into path-scoped rules. The invariants live in
`.claude/rules/satellites.md`; this is the long form (resolver, IRK store, pairing flow, the
Esszimmer k8s satellite, node resilience, the bonding bias and its fix, deploy notes).

Modern phones advertise a **rotating** BLE Resolvable Private Address (RPA), so a static MAC whitelist can't track them. Renfield resolves the rotating address back to a stable identity using the device's **Identity Resolving Key (IRK)** — the same mechanism as Home Assistant's *Private BLE Device* / Bermuda. No app, no extra hardware. Design + status: this document (SHIPPED + DEPLOYED).

- **Resolver** (`src/satellite/renfield_satellite/ble/rpa.py`): the BLE-spec `ah` hash (AES-128, validated against the spec vector); pure software on scanned adverts → works on any adapter (no raw HCI / Classic-BT / bonding). The continuous scanner (`ble/scanner.py`, `ble.continuous`) keeps one `BleakScanner` running with EWMA-smoothed RSSI. **Fleet default is now continuous + `ble_scan_interval=3` (equalised 2026-07-10)** — the report loop `sleep(scan_interval)` gates the report cadence in BOTH modes (continuous only steadies RSSI, it does NOT lower the report rate), so the prior 3s-Esszimmer / 30s-Pi split was a 10× report asymmetry that biased room-arbitration toward the chattier satellite (the measured root cause of the "phone shows the wrong adjacent room" mislocation — diagnosed via the read-only `GET /api/presence/debug/sightings` diagnostic). Continuous makes the 3s poll near-free (in-memory snapshot, no extra radio bursts).
- **Backend IRK store** (`user_ble_irks`, migration `pc20260619`): per-person IRK **encrypted at rest** via `services/secret_encryption.py` (Fernet from `SECRET_KEY` — a rotation is destructive to these secrets unless the old key is listed in `SECRET_KEY_PREVIOUS` and `bin/rotate_secret_encryption.py` re-encrypts them; runbook in `docs/ENVIRONMENT_VARIABLES.md`, BL-0357). Admin API `GET/POST/PATCH/DELETE /api/presence/irks` (the IRK is **never returned**). Pushed to satellites via the `ble_known_irks` WS message; `presence_service.process_ble_report` maps a resolved `identity` back to the user (`irk:<label>` key, survives rotation).
- **UI pairing flow** ("pair my phone for presence", `components/presence/IrkPairing.tsx`): `POST /api/presence/irks/capture` → `satellite_manager.request_irk_capture` → the satellite opens a bounded BlueZ pairing window (`_capture_irk_for_request`: discoverable + `bt-agent`), reads the bonded phone's IRK from `/var/lib/bluetooth` (BlueZ doesn't expose IRKs over D-Bus), re-secures, replies `irk_capture_result`; the backend stores it encrypted. Mirrors the `capture_snapshot`/`bt_scan_request` request-response pattern.
- **The Esszimmer k8s satellite** (`k8s/satellite-esszimmer.yaml`): node-pinned privileged pod, `hostNetwork`, `hostAliases renfield.local→Traefik LB`, hostPath `/dev/snd` + `/dev/bus/usb` (USB audio + XVF3800 LED via `xvf_host`) + `/run/dbus` (BLE via host BlueZ) + **`/var/lib/bluetooth` RO** (IRK capture). Image built on the Pi, imported into the node containerd (`ctr -n k8s.io images import`), `imagePullPolicy: Never`. The custom `hey_renfield.onnx` wakeword model + personal device MACs/IRKs live in the image build context / backend DB, never in git. **Node resilience:** the pod is hardware-pinned to this node, so a dead node = a dead Esszimmer that k8s can't self-heal — it went `NotReady`+unreachable twice in a day, each needing a manual power-cycle. `k8s/orangepi-node-resilience.sh` (run as root on the node) applies the same two-layer watchdog the bare-metal sats have — SoC HW watchdog (`RuntimeWatchdogSec=14s`, sunxi ~16s max, for kernel/PID-1 hangs) + the shared `renfield-net-watchdog` timer (reboot if the gateway is unreachable, for a wedged net stack) — plus **persistent journald** (the journal was `volatile`, so the death cause was lost on every reboot; now diagnosable).
- **Bonded vs resolver:** a satellite that has *bonded* the phone (Esszimmer did) lets BlueZ resolve the RPA to the identity MAC natively → it's tracked as a normal `ble` known-device. Non-bonded satellites use the pushed IRK + the software resolver. Device identities live in the **backend known-devices DB** (`UserBleDevice`) — not in the manifest. With the BLE stack on multiple satellites, the backend's `_assign_room` does multi-satellite RSSI arbitration (mean RSSI + per-extra-satellite bonus + hysteresis). **Bonding can BIAS that arbitration (fixed 2026-06-22):** a bonded satellite resolves the phone *natively on every advert* and reports a strong, near-constant signal, so in adjacent/open-plan rooms it **out-shouts** a non-bonded neighbour and wins the room even when the user is physically in the other room (observed Esszimmer-bonded ~20 matches/min vs Wohnzimmer Pi-Zero ~2/min → always assigned Esszimmer). Fix: **un-bond the phone** so *every* satellite uses the same software IRK resolver — `bluetoothctl remove <identity-mac>` on the bonding satellite's **host** (the bond lives in the host's `/var/lib/bluetooth`; the k8s pod mounts it RO and can't remove it). The IRK is already in the backend, so detection continues via software; only the unfair native edge is removed, and RSSI/placement then decides the room correctly (verified: presence tracks the actual room bidirectionally). Re-bond anytime via the IRK pairing flow if a satellite needs native resolution. See `docs/design/ble-presence-improvement.md`.
- Deploy note: the satellite image needs `bluez`/`bluez-tools` + `cryptography`; bare-metal Pis need the `cryptography` dep and `sudo` for the `/var/lib/bluetooth` read. **`cryptography` is mandatory for IRK resolution and `rpa.py` silently no-ops without it** (`resolve_rpa()` returns `False` for every address → phone presence dies invisibly). It was missing from every bare-metal venv (it lived only in `satellite_python_packages`, installed under the `[python]` tag, which the safety code-only `--tags app` deploy skips — the same drift the `envirophat` task documents). Now installed by a dedicated `[python, app]` task **and** in the satellite `requirements.txt`; the satellite also logs a loud warning if IRKs arrive while `_CRYPTO_AVAILABLE` is False. Verify after deploy: `venv/bin/python -c "from renfield_satellite.ble.rpa import _CRYPTO_AVAILABLE; print(_CRYPTO_AVAILABLE)"`.

## Voice hold (2026-11-13)

A voice or auth sighting is certain; a BLE reading is an estimate. Observed live on 2026-11-13: a voice
turn in one room moved the user there, and three seconds later a BLE reading at the RSSI floor from another
room moved them back. Cause: `_should_switch_filtered` switches at once when the current room has no
filtered value, and a room the phone was never heard in has none (watch-list item F2 of the filter rollout).

Since then `register_voice_presence` sets `UserPresence.voice_hold_until`, and `_assign_room` returns before
the switch decision while the hold runs: BLE may confirm the voice room, not replace it. `last_seen` and the
filter state keep updating during the hold, so the user is not marked absent and the first decision after
the hold rests on current readings. `PRESENCE_VOICE_HOLD_SECONDS` (default 60, 0 = off) sets the length; the
value is a choice, not a measurement. A second voice sighting moves the user at once and restarts the hold.

The hold covers every caller of `register_voice_presence`, so a typed chat message from a room-bound device
pins the room for the same time. With the setting at 0 the old path is unchanged.

### After the hold: the last certain sighting stands (2026-11-13, owner's decision)

The hold alone only postpones the flip: once it ends, a room the phone was never heard in still has no
filtered value, and the first reading from elsewhere would win by default. The phone and the person are not
the same thing; the owner chose the person. `UserPresence.voice_pinned` marks a room that rests on a voice/auth
sighting BLE has not confirmed. For such a room `_should_switch_filtered` measures the challenger against the
RSSI floor plus `PRESENCE_VOICE_RELEASE_MARGIN_DB` (default 10 → −70 dBm at the −80 floor), on the filtered
value. Only BLE MOVING the user ends the pin. BLE merely agreeing does not: a weak reading in the voice room
would otherwise hand the room to BLE and lose it the moment that reading ages out (and a second device of the
same user, whose pass rebuilds the per-user filter, would do the same).

**A held room is a guess, and the privacy gates treat it as unknown.** `UserPresence.room_evidence_at` records
when something last placed the user in the current room (a sighting, or a BLE pass whose best room was this
one). `is_location_uncertain()` is true for a pinned user once that is older than the longer of the hold and
the BLE freshness window — per user, so it neither flickers with several devices nor depends on the filter
(the legacy path has none). The message relay (`announce_in_room`) drops such a user from the room's occupants
and blocks when a non-recipient is among them. `ha_should_play_tts_for_notification` likewise
fails closed: a `confidential` notification is not spoken while ANY tracked user's room is a guess (the target
may not be there, someone else may be), and for `personal` such a user counts as a possible listener in every
room and not as an occupant of the booked one. Display surfaces (kiosk, history) and media follow keep using
the booked room.

Consequences, intended: someone who speaks in a room and leaves the phone elsewhere stays in that room for as
long as the phone is heard only weakly; they become absent when the phone is not heard for
`PRESENCE_STALE_TIMEOUT` (the sweep runs on BLE reports, so only while some satellite still reports). The 10 dB is a choice. The readings behind it: on 2026-11-13 the phantom room's
filtered value sat between −75 and −78 dBm over one minute, while entries with the phone in the room reach
−60 and better. `0` restores the old rule. Legacy (non-filter) path: unchanged apart from the hold.

Still open: a lone satellite near the floor can place a user in its room when no voice sighting is involved,
and RSSI is compared across satellites without any per-receiver calibration. Both are what "Placement v2"
below addresses — in shadow mode so far, so the legacy behaviour described here is still what acts.

## Placement v2 (stage 1: shadow mode, 2026-11-13)

A second room decision, built beside the legacy one and — in this stage — only logged.
Code: `ha_glue/services/presence_placement.py` (the pure decision),
`ha_glue/services/presence_placement_shadow.py` (the shadow wiring). Flag:
`PRESENCE_PLACEMENT_V2_SHADOW_ENABLED`, default off.

### Why (measured on a live instance, 2026-11-13)

Rooms carry neutral names here and in the recording: room-a … room-e have a receiver (sat-a …
sat-e), room-f … room-h have none, and room-a and room-e are one open-plan space.

The legacy decision picks the best of whatever receivers report at or above −80 dBm. It has no
answer "none of these":

- A room without a live receiver is booked to whichever neighbour hears the phone at −71…−77 dBm
  (room-f → room-e, room-g → room-b, room-h → room-b).
- Once the incumbent room is silent, any reading takes over without a margin; conversely the
  incumbent keeps a room on a decaying filter value with no fresh reading behind it.
- `confidence` measures level, not ambiguity (one receiver per room caps the second term at 0.1).
- `_cleanup_stale` runs only inside `process_ble_report`: with no report, presence freezes.
- The filter advances one step per REPORT from any satellite, not per unit of time, so more
  reporting receivers mean a faster attack and decay.
- `is_location_uncertain` is true only for voice-pinned users, so the privacy gates trust a
  0.3-confidence BLE verdict.
- Receivers are compared on one scale although they differ: at about 1 m the readings were −50
  (room-a), −58 (room-e) and −66 dBm (room-d), and position or orientation of the phone
  moves a reading by 10–20 dB.
- Two kinds of receiver exist since the stage-0 experiment: room-d and room-a report everything
  they hear (satellite cutoff −100), the other three still cut at −80 and send nothing below it.
- In the middle of the open-plan space (room-a + room-e) the phone reads 12–14 dB stronger on the
  room-e receiver.

### The verdict

`decide(readings, receivers, zones, previous, now, config)` answers for ONE device at ONE moment.
It has no I/O and no module state, and the clock is an argument.

| kind | meaning |
|---|---|
| `placed` | a receiver vouches for the device. `rooms` = every room of the zone (what a gate must use), `room` = the nearer room inside it (a hint for a display), `zone` = the zone's name or none |
| `home_unknown` | heard within the deadline, but no receiver places it |
| `absent` | not heard for longer than the deadline while at least one receiver is alive |
| `unavailable` | not heard and no receiver alive — nothing can be said, least of all "absent" |

Every verdict carries its components: best and runner-up comparable values, the margin between
them, the distance from the in-room bound, the sample count, the age of the newest reading, which
receivers were censored or offline, a machine-readable `reason`, and one evidence row per receiver.

**Smoothing** is per receiver and time-based — a function of the (timestamp, value) pairs alone:

    value(now) = Σ rssi_i · w_i / Σ w_i,    w_i = exp(−(now − t_i) / τ)

Two readings with the same timestamp are one reading (the later one counts). Neither the number of
reports nor what other receivers send moves a receiver's value. A reading whose level or timestamp
is not a finite number is no reading: device entries reach the presence service unvalidated and
JSON carries `NaN`, which would turn the mean into NaN — and every comparison against NaN is false.
For the same reason both gates of the rule are written "unless proven inside": a value that cannot
be compared rejects, it never places.

**Receiver states.** `heard` = a reading no older than `fresh_s`. `censored` = alive (heartbeat or
a recent reading) but silent: all that is known is "at or below its cutoff, or deaf". It is never
turned into a reading; for the margin it stands at `cutoff + offset`, the strongest value it could
be hiding. `offline` = no sign of life: it says nothing either way.

**The rule.** A zone is claimed only when (1) one of its calibrated receivers hears the device
inside that receiver's own in-room bound (`smoothed ≥ in_room_dbm`) AND (2) the zone's strongest
comparable value (`smoothed + offset_db`) leads every receiver outside the zone — heard ones at
their value, censored ones at their cutoff — by `margin_db`. The incumbent zone gets both tests
relaxed by `hysteresis_db`; it does not survive a reading that has gone stale. If no zone can be
claimed the verdict is `home_unknown`. An open-plan zone is decided as one unit: one member inside
its bound is enough, and members are never each other's runner-up. An uncalibrated receiver inside
a zone neither helps nor hinders that zone — it cannot vouch and it does not compete against its
own zone — so the hint then names the strongest CALIBRATED member's room even if the uncalibrated
one hears the device louder.

The clock belongs to the caller and must not run backwards for one device: readings newer than
`now` are ignored, so an earlier `now` sees an emptier window and drops the incumbent.

**Parameters** are per receiver and have no defaults — a bound measured for one receiver in one
room holds for no other. JSON settings for now (a later stage moves them into a table):

    PRESENCE_PLACEMENT_V2_RECEIVERS='{"<satellite_id>": {"offset_db": 0, "in_room_dbm": -60, "cutoff_dbm": -80}}'
    PRESENCE_PLACEMENT_V2_ZONES='{"<zone name>": [<room_id>, <room_id>]}'

`in_room_dbm` is on the receiver's own (raw) scale, so the offset never moves it. A receiver that
is not listed is uncalibrated: it keeps a device "home" and can stand in the way of a placement
elsewhere (compared at face value), but it can never place. The decision's own numbers
(`…_TIME_CONSTANT_SECONDS` 6, `…_FRESH_SECONDS` 20, `…_MARGIN_DB` 10, `…_HYSTERESIS_DB` 3) are
choices, not measurements; the deadline is `PRESENCE_STALE_TIMEOUT`, the sign-of-life window
`DEVICE_HEARTBEAT_TIMEOUT`.

What switches the shadow off with one ERROR line while the backend starts regardless: unusable
content of the two JSON strings, a broken rule between the numbers (hysteresis above the margin,
freshness above the deadline, a value that is not positive or not finite), and any failure to
build the shadow at all, a failing import included. What it does not cover: the four numbers are
ordinary float settings, so a value that is not a number stops the backend at start like any other
float setting.

### What the labelled walk shows — and how little that is

`tests/backend/fixtures/presence_walk_2026-11-13.json`: one phone, one evening, five receivers,
7020 readings over 124 minutes, twelve labelled segments of about 50 s. The replay
(`tests/backend/test_presence_placement_replay.py`) derives provisional parameters from the
recording by a fixed rule (cutoff = weakest reading the receiver ever sent; bound = 10th percentile
of its readings during the station in its own room; offset = 0) and runs the walk through both
decisions.

| true room | receiver in room | legacy | v2 |
|---|---|---|---|
| room-f | no | room-e | `home_unknown` |
| room-g | no | room-b | `home_unknown` |
| room-h | no | room-b | `home_unknown` |
| room-a (middle of the open space) | yes | room-e | placed, zone room-a+room-e |
| room-e | yes | room-e | placed, zone room-a+room-e |
| room-d | yes | room-d | placed room-d |
| room-c | yes | room-c | placed room-c |
| room-b | yes | room-b | placed room-b |
| 1 m from the room-a receiver | yes | room-a | placed, zone |
| 1 m from the room-e receiver | yes | room-e | placed, zone |
| 1 m from the room-d receiver | yes | room-d | `home_unknown` — the price |

No wrong room at any evaluated instant of a scored segment. One correct placement is given up.

That price is counted on the labelled segments only, about 10 of the 124 minutes. Over the whole
walk v2 says `home_unknown` for 41 % of the time (the legacy decision always names a room) and
changes its verdict 34 times against the legacy decision's 25. How much of the 41 % is a room
without a receiver and how much a placement given up cannot be told from this recording.

This is thin, and the recording itself says where:

- **A bound taken at one position does not describe the room.** The room-d receiver reads −46 dBm
  at the room station and −66 dBm at "1 m in front of it". The bound from the first (−47) rejects
  the second. The bound from the second (−68) leaves a 7 dB lead over the room-e receiver.
- **The separation that carries the reject option is as large as the stated orientation spread.**
  In room-h the room-b receiver reads up to −65 dBm; in room-b itself its
  10th percentile is −53. Twelve dB, one position each.
- **The three receivers that cut at −80 mostly repeat themselves and fall silent with the phone in
  the room.** 44–66 % of their readings sit in runs of five or more identical values (the satellite
  re-sends its own smoothed value for 20 s after the last advert), and gaps of up to 63 s occur
  between two readings at −65 dBm or better. "17 readings per station" are a handful of
  measurements there, the backend's reading age understates the true one by up to 20 s, and
  "silent" on such a receiver quite often means "deaf for a moment", not "≤ −80".
- **Inside the open-plan zone the hint is weak**: in the middle of the space it names
  room-e while the label says room-a. That is why zone membership, not the hint, is the
  verdict.
- **A room nobody hears stays a hole.** In room-b no other receiver hears the phone at
  all; without its own receiver the verdict would be `absent` after the deadline.
- **The censoring rule is not carried by this recording.** Treating a silent receiver as "far"
  instead changes 25 of 7007 evaluated instants and no station; only the unit tests hold it. The
  in-room bound, by contrast, decides 14 % of the instants and two of the three stations in a room
  without a receiver (the third, room-f, is also rejected by the margin alone).
- Held-out checks (bounds from the 1 m segments scored on the room stations and the reverse; first
  half of each station scored on the second) also show no wrong room, at the price of every room
  whose receiver has no segment of the deriving kind.

### Staged plan

- **Stage 0 (running):** two receivers report everything (satellite cutoff −100) to see what lies
  below −80.
- **Stage 1 (this):** the decision runs in shadow mode. With the flag on it reads the same
  sightings as the legacy decision and
  - logs one line when the two decisions START to disagree about a person and one when they STOP
    (with the duration and the number of distinct verdict pairs in between) — never per report.
    STOP is written only after the agreement has held for 30 s without interruption
    (`DISAGREEMENT_SETTLE_S`, a choice: longer than the default freshness window), so a device on
    the edge of a bound yields one pair, not one per report;
  - logs one line at every voice/auth sighting stating what each decision believed just before it
    (`[match]`/`[miss]` against the sighting — free ground truth);
  - adds `placement_v2` per device to the live-readings payload
    (`presence_debug.build_sightings_payload`), evaluated at the snapshot's clock and without
    advancing the shadow's own state.

  Log lines carry the user id (as the existing presence lines do), room names and numbers —
  no device key, no satellite id, no user name. A failure inside the shadow is logged once per
  entry point with the exception TYPE only: its message may carry a device key.

  Several devices of one person: every device has its own verdict (the payload shows them one by
  one), and the disagreement log joins them per person, because the legacy decision keeps one room
  per person. "In room R" agrees when ANY device is placed there; "not tracked" agrees only when
  EVERY device is gone. That is a logging rule so that a second phone left elsewhere is not a
  standing disagreement — the real rule for several devices is stage 2.

  If the wall clock steps back, the shadow holds its verdicts and logs nothing but one WARNING
  until the clock has passed the earlier time again.
- **Stage 2 — the state follows v2:** expiry by deadline (a timer per device, not a sweep inside a
  report; `unavailable` must not fire leave events); `UserPresence` may be "home, room unknown";
  `is_location_uncertain` true for every source whose verdict is not `placed`; voice/auth sightings
  and the hold expressed in v2 terms; one rule for several devices of one person; parameters and
  zones from a table with a calibration flow; the satellite reports its own cutoff, the age of the
  last advert and a BLE sign of life (a heartbeat proves the process, not the scanner).
- **Stage 3 — the consumers follow:** privacy gates count by zone (`verdict.rooms`) and treat
  `home_unknown` as a possible listener everywhere; `confidence` is computed from the components;
  the kiosk payload gains "home, room unknown"; history and the agent's "where is X" can answer it;
  the legacy path and its settings are removed.

### What stage 1 does NOT do

It changes no presence state, fires no event or hook, reaches no kiosk and no privacy gate. The
legacy decision, `_cleanup_stale`, `is_location_uncertain`, the gates and `confidence` are
untouched. There is no timer: the shadow decides when a report arrives and when a snapshot is
built. It places nobody until receivers are configured, and no receiver numbers ship. With the flag
off the new modules are not imported.
