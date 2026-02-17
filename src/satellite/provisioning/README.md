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
