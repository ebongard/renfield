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

### Phase 1c — Backend RSSI smoothing + hysteresis  ⏳ DEFERRED (shared backend)
Median/EWMA + hand-off hysteresis in the room-arbitration logic to kill
flip-flop. Touches the production backend → its own reviewed change.

> **STATUS 2026-06-19: SHIPPED + DEPLOYED.** Phase 1 (continuous scan + BlueZ
> Experimental) and Phase 2 (IRK store + RPA resolution + UI pairing flow) are
> merged (#825/#826/#828/#829) and live: backend IRK store deployed, the
> Esszimmer Orange Pi satellite resolves an iPhone via BLE, and the BLE stack is
> rolled out to the Pi fleet (multi-satellite room arbitration active). Phases
> 1b/1c/3 remain deferred.

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

## Open questions
- Bond per-satellite vs central IRK distribution?
- Audit: which household devices use stable vs randomized addresses today?
- Privacy/consent model for storing IRKs (ties into household privacy posture).

## Background moved from CLAUDE.md (2026-09-20)

Moved verbatim when CLAUDE.md was split into path-scoped rules. The invariants live in
`.claude/rules/satellites.md`; this is the long form (resolver, IRK store, pairing flow, the
Esszimmer k8s satellite, node resilience, the bonding bias and its fix, deploy notes).

Modern phones advertise a **rotating** BLE Resolvable Private Address (RPA), so a static MAC whitelist can't track them. Renfield resolves the rotating address back to a stable identity using the device's **Identity Resolving Key (IRK)** — the same mechanism as Home Assistant's *Private BLE Device* / Bermuda. No app, no extra hardware. Design + status: [`docs/design/ble-presence-improvement.md`](docs/design/ble-presence-improvement.md) (SHIPPED + DEPLOYED).

- **Resolver** (`src/satellite/renfield_satellite/ble/rpa.py`): the BLE-spec `ah` hash (AES-128, validated against the spec vector); pure software on scanned adverts → works on any adapter (no raw HCI / Classic-BT / bonding). The continuous scanner (`ble/scanner.py`, `ble.continuous`) keeps one `BleakScanner` running with EWMA-smoothed RSSI. **Fleet default is now continuous + `ble_scan_interval=3` (equalised 2026-07-10)** — the report loop `sleep(scan_interval)` gates the report cadence in BOTH modes (continuous only steadies RSSI, it does NOT lower the report rate), so the prior 3s-Esszimmer / 30s-Pi split was a 10× report asymmetry that biased room-arbitration toward the chattier satellite (the measured root cause of the "phone shows the wrong adjacent room" mislocation — diagnosed via the read-only `GET /api/presence/debug/sightings` diagnostic). Continuous makes the 3s poll near-free (in-memory snapshot, no extra radio bursts).
- **Backend IRK store** (`user_ble_irks`, migration `pc20260619`): per-person IRK **encrypted at rest** via `services/secret_encryption.py` (Fernet from `SECRET_KEY` — rotating `SECRET_KEY` is destructive to these secrets). Admin API `GET/POST/PATCH/DELETE /api/presence/irks` (the IRK is **never returned**). Pushed to satellites via the `ble_known_irks` WS message; `presence_service.process_ble_report` maps a resolved `identity` back to the user (`irk:<label>` key, survives rotation).
- **UI pairing flow** ("pair my phone for presence", `components/presence/IrkPairing.tsx`): `POST /api/presence/irks/capture` → `satellite_manager.request_irk_capture` → the satellite opens a bounded BlueZ pairing window (`_capture_irk_for_request`: discoverable + `bt-agent`), reads the bonded phone's IRK from `/var/lib/bluetooth` (BlueZ doesn't expose IRKs over D-Bus), re-secures, replies `irk_capture_result`; the backend stores it encrypted. Mirrors the `capture_snapshot`/`bt_scan_request` request-response pattern.
- **The Esszimmer k8s satellite** (`k8s/satellite-esszimmer.yaml`): node-pinned privileged pod, `hostNetwork`, `hostAliases renfield.local→Traefik LB`, hostPath `/dev/snd` + `/dev/bus/usb` (USB audio + XVF3800 LED via `xvf_host`) + `/run/dbus` (BLE via host BlueZ) + **`/var/lib/bluetooth` RO** (IRK capture). Image built on the Pi, imported into the node containerd (`ctr -n k8s.io images import`), `imagePullPolicy: Never`. The custom `hey_renfield.onnx` wakeword model + personal device MACs/IRKs live in the image build context / backend DB, never in git. **Node resilience:** the pod is hardware-pinned to this node, so a dead node = a dead Esszimmer that k8s can't self-heal — it went `NotReady`+unreachable twice in a day, each needing a manual power-cycle. `k8s/orangepi-node-resilience.sh` (run as root on the node) applies the same two-layer watchdog the bare-metal sats have — SoC HW watchdog (`RuntimeWatchdogSec=14s`, sunxi ~16s max, for kernel/PID-1 hangs) + the shared `renfield-net-watchdog` timer (reboot if the gateway is unreachable, for a wedged net stack) — plus **persistent journald** (the journal was `volatile`, so the death cause was lost on every reboot; now diagnosable).
- **Bonded vs resolver:** a satellite that has *bonded* the phone (Esszimmer did) lets BlueZ resolve the RPA to the identity MAC natively → it's tracked as a normal `ble` known-device. Non-bonded satellites use the pushed IRK + the software resolver. Device identities live in the **backend known-devices DB** (`UserBleDevice`) — not in the manifest. With the BLE stack on multiple satellites, the backend's `_assign_room` does multi-satellite RSSI arbitration (mean RSSI + per-extra-satellite bonus + hysteresis). **Bonding can BIAS that arbitration (fixed 2026-06-22):** a bonded satellite resolves the phone *natively on every advert* and reports a strong, near-constant signal, so in adjacent/open-plan rooms it **out-shouts** a non-bonded neighbour and wins the room even when the user is physically in the other room (observed Esszimmer-bonded ~20 matches/min vs Wohnzimmer Pi-Zero ~2/min → always assigned Esszimmer). Fix: **un-bond the phone** so *every* satellite uses the same software IRK resolver — `bluetoothctl remove <identity-mac>` on the bonding satellite's **host** (the bond lives in the host's `/var/lib/bluetooth`; the k8s pod mounts it RO and can't remove it). The IRK is already in the backend, so detection continues via software; only the unfair native edge is removed, and RSSI/placement then decides the room correctly (verified: presence tracks the actual room bidirectionally). Re-bond anytime via the IRK pairing flow if a satellite needs native resolution. See `docs/design/ble-presence-improvement.md`.
- Deploy note: the satellite image needs `bluez`/`bluez-tools` + `cryptography`; bare-metal Pis need the `cryptography` dep and `sudo` for the `/var/lib/bluetooth` read. **`cryptography` is mandatory for IRK resolution and `rpa.py` silently no-ops without it** (`resolve_rpa()` returns `False` for every address → phone presence dies invisibly). It was missing from every bare-metal venv (it lived only in `satellite_python_packages`, installed under the `[python]` tag, which the safety code-only `--tags app` deploy skips — the same drift the `envirophat` task documents). Now installed by a dedicated `[python, app]` task **and** in the satellite `requirements.txt`; the satellite also logs a loud warning if IRKs arrive while `_CRYPTO_AVAILABLE` is False. Verify after deploy: `venv/bin/python -c "from renfield_satellite.ble.rpa import _CRYPTO_AVAILABLE; print(_CRYPTO_AVAILABLE)"`.
