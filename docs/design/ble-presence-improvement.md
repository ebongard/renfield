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
