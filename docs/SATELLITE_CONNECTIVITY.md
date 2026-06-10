# Satellite Connectivity & Robustness

How a voice satellite (Pi Zero 2 W + ReSpeaker) connects to Renfield, the failure
modes that make it flaky, and the robustness knobs that let a blip self-heal
instead of wedging.

## Connection path

```
satellite (renfield_satellite)
  └─ single persistent WebSocket → wss://renfield.local/ws/satellite
        │  (k8s ingress renfield-https: /ws → backend:8000; self-signed cert, verify_tls=false)
        ▼
backend  ha_glue/api/websocket/satellite_handler.py  (/ws/satellite)
  • first message is `register` (id + room + capabilities) → `register_ack`
  • then: app heartbeat every 30s + WS ping/pong keepalive
  • SatelliteManager tracks live connections IN MEMORY (lost on a backend restart)
```

There is no pre-registration: any satellite may connect and is registered fresh
each time; the room row is created on first connect (`ROOMS_AUTO_CREATE_FROM_SATELLITE`).

## Failure modes (why it gets flaky)

1. **Weak 2.4 GHz WiFi.** The Pi Zero 2 W is single-band 2.4 GHz with a marginal
   antenna. Measured LAN RTT to a live satellite: 31–475 ms, ~220 ms jitter. Lossy
   links drop the WS; slow links make every handshake fragile. **This is the
   dominant cause** — fix it at the network layer (closer AP / mesh node / clean
   2.4 GHz channel; or a USB-OTG Ethernet adapter for a stationary unit).
2. **mDNS / boot-window race.** `renfield.local` depends on Avahi + the cluster
   `mdns-responder`. At boot (before NTP/mDNS are ready) the handshake can time
   out — these failures appear only in the *satellite* journal, never in backend
   logs (they never reach FastAPI).
3. **Device offline.** A powered-off / crashed / WiFi-disassociated Pi shows
   nothing in backend logs. Check it physically: `ping <ip>`, ARP, the LED. A
   4-mic-HAT unit can hit the AC108 + onnxruntime same-process kernel crash —
   mitigate with `use_arecord: true`.
4. **In-process wedges (fixed in the client).** See below.

## Robustness knobs (`server.*` in `satellite.yaml`)

Tuned defaults live in `provisioning/group_vars/satellites.yml`; override per-host
in `host_vars/`.

| Key | Default | Purpose |
|---|---|---|
| `ping_interval` | `15` | WS keepalive cadence (s) — tighter than the old hardcoded 20 → faster dead-link detection |
| `ping_timeout` | `8` | drop the link if no pong within (s) |
| `register_timeout` | `15` | **caps the post-connect `register` handshake.** Without it a slow/hung backend blocked the connect coroutine forever, wedging the satellite in CONNECTING with no reconnect |
| `max_disconnected_seconds` | `300` | if the satellite can't reconnect for this long, it **exits cleanly so systemd restarts a fresh process** (backstop for any in-process wedge). `0` disables |
| `reconnect_interval` | `5` | base for the exponential reconnect backoff (5→10→20→40→60 cap) |
| `heartbeat_interval` | `30` | app heartbeat cadence |

Client behavior fixes (shipped in the satellite package):
- `_register()` recv is bounded by `register_timeout` (was unbounded).
- A failed **heartbeat send now triggers reconnect immediately** (was swallowed,
  leaving a zombie connection until the WS ping timeout ~30 s later).
- The disconnect watchdog (`max_disconnected_seconds`) exits → systemd
  `Restart=always` brings up a clean process. `StartLimitIntervalSec=0` in the
  unit ensures these legitimate restarts never trip systemd's burst limit.

> The watchdog uses a **clean `os._exit`**, never a SIGKILL-mid-restart — so it
> does not risk the SD-card corruption that bricked a satellite before.

## Diagnosing "X won't connect"

```bash
# 1. Does the backend ever see it? (zero events ⇒ it never reached FastAPI)
kubectl -n renfield logs deploy/backend --since=24h | grep -i "sat-<room>\|📡\|👋"
# 2. Is the device even on the network? (read-only, safe)
ping <satellite-ip>;  arp -n <satellite-ip>
# 3. The device's own view (read-only):
ssh <pi> journalctl -u renfield-satellite -n 200 --no-pager
#    grep: "Connecting to", "Registered successfully", "Reconnecting in",
#          "timed out during opening handshake", "register ack not received"
```

## Deploying changes safely

Satellite **code/config** changes deploy with Ansible **`--tags app`** (avoids the
driver/service restart that risks bricking the SD card):

```bash
cd src/satellite/provisioning
ansible-playbook -i inventory.yml provision.yml --limit satellite-<room> --tags app
```

⚠️ Never blindly remote-restart a satellite service; a SIGKILL mid-restart can
corrupt the Pi Zero 2 W SD card. Roll out one host at a time and verify it
re-registers (backend log `📡 Satellite sat-<room> registered`) before the next.
