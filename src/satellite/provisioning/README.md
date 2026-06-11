# Renfield Satellite Provisioning

Ansible playbook for provisioning Raspberry Pi Zero 2 W satellites with ReSpeaker audio HATs.

## Supported Hardware

| HAT | `hat_type` | LEDs | SPI | Power Pin | ALSA Card |
|-----|-----------|------|-----|-----------|-----------|
| ReSpeaker 2-Mic HAT V1 (WM8960) | `2mic` | 3 | 0:0 | — | `seeed2micvoicec` |
| ReSpeaker 2-Mic HAT V2 (TLV320AIC3104) | `2mic-v2` | 3 | 0:0 | — | `seeed2micvoicec` |
| ReSpeaker 4-Mic Array | `4mic` | 12 | 0:1 | GPIO 5 | `seeed4micvoicec` |

## Prerequisites

On your **control machine** (Mac/Linux):

```bash
pip install ansible
```

On the **Pi** (fresh Raspberry Pi OS image):
- SSH enabled, user configured
- Network connected (Wi-Fi or Ethernet)
- SPI and I2C enabled in `raspi-config`

## Quick Start

Provision a single satellite:

```bash
cd src/satellite/provisioning
ansible-playbook -i inventory.yml provision.yml --limit satellite-fitnessraum -v
```

Provision all satellites:

```bash
ansible-playbook -i inventory.yml provision.yml -v
```

## Tags

Run individual phases:

```bash
# Just update the config
ansible-playbook -i inventory.yml provision.yml --limit satellite-fitnessraum --tags config

# Just redeploy code
ansible-playbook -i inventory.yml provision.yml --limit satellite-fitnessraum --tags app

# Just update models
ansible-playbook -i inventory.yml provision.yml --limit satellite-fitnessraum --tags models
```

Available tags: `system`, `boot`, `driver`, `python`, `app`, `config`, `models`, `service`

## Dry Run

Preview changes without applying:

```bash
ansible-playbook -i inventory.yml provision.yml --limit satellite-fitnessraum --check -v
```

## HiFiBerry DLNA Renderers (https TTS)

The `hifiberry` inventory group + `provision-hifiberry.yml` are **separate from
the satellites** — a HiFiBerry is a DLNA music renderer, not a Renfield
satellite. The playbook installs the Renfield TLS CA and pins `renfield.local`
in `/etc/hosts` so the HiFiBerry's gstreamer can fetch backend-served **https**
TTS audio (relay/announce). Without it, TTS to the HiFiBerry fails *silently*
(the renderer reports `playing` but never fetches the URL).

Why only the HiFiBerry: Linn/openHome renderers resolve `renfield.local` via DNS
and accept the self-signed cert natively; the HiFiBerry's gstreamer is strict
(rejects the self-signed cert) and its systemd-resolved hijacks `.local` as mDNS
(NOTFOUND before DNS). Full background: `docs/MESSAGE_RELAY.md` → "TTS audio
delivery to renderers".

```bash
cd src/satellite/provisioning
# Login user is root (HiFiBerryOS has no sudo) → --ask-pass (password: hifiberry)
ansible-playbook -i inventory.yml provision-hifiberry.yml --ask-pass --limit hifiberry-arbeitszimmer

# Dry run
ansible-playbook -i inventory.yml provision-hifiberry.yml --ask-pass --check
```

Idempotent. Tags: `ca`, `hosts`. **Re-run after a HiFiBerryOS update** — an OS
update wipes the CA + `/etc/hosts` edits. `files/renfield-ca.pem` is the
cluster's public TLS cert (no key); if it ever rotates, refresh it per the
comment at the top of `provision-hifiberry.yml`.

## Adding a New Satellite

1. Add the host to `inventory.yml`
2. Create `host_vars/<hostname>.yml` with HAT-specific settings
3. Run the playbook

## Host Variables

| Variable | Description | 2-mic default | 4-mic default |
|----------|-------------|---------------|---------------|
| `hat_type` | `"2mic"`, `"2mic-v2"`, or `"4mic"` | `"2mic"` | `"4mic"` |
| `satellite_id` | Unique satellite ID | — | — |
| `satellite_room` | Room name | — | — |
| `led_num` | Number of APA102 LEDs | `3` | `12` |
| `led_spi_device` | SPI device number | `0` | `1` |
| `led_power_pin` | GPIO for LED power | `null` | `5` |
| `audio_device` | ALSA capture device | `"capture"` | `"default"` |
| `audio_playback_device` | ALSA playback device | `"plughw:0,0"` | `"default"` |
| `audio_channels` | Recording channels | `2` | `1` |
| `beamforming_enabled` | Delay-and-Sum beamforming | `true` | `false` |

## Safety Notes

- The playbook uses `systemctl start`, not `restart` — safe for first provisioning
- Each step is idempotent — safe to re-run
- Driver installation triggers a reboot (handled automatically)
- For code-only updates, use `--tags app` to skip hardware steps

## Verification

After provisioning, check:

```bash
# Sound card detected?
ssh satellite-fitnessraum.local "cat /proc/asound/cards"

# Service running?
ssh satellite-fitnessraum.local "sudo journalctl -u renfield-satellite -n 30"

# Expected log lines:
#   LED power enabled on GPIO5       (4-mic only)
#   SPI opened: bus 0, device 1      (4-mic only)
#   Connected to server
```
