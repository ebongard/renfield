# Satellite Deployment Safety & Provisioning

## CRITICAL: SD Card Brick Risk

Pi Zero 2 W SD cards are **EXTREMELY fragile**. A SIGKILL during restart can corrupt the filesystem and brick the device permanently.

- The Hobbyraum satellite was bricked during a `systemctl restart` deploy
- ALWAYS ask before restarting any satellite service
- Use `--tags app` for code-only deploys (avoids driver/service restart risk)

## Ansible Provisioning (Recommended)

```bash
cd src/satellite/provisioning/

# Full provisioning (drivers + service + code)
ansible-playbook -i inventory.yml playbook.yml --limit <hostname>

# Code-only deploy (SAFE — no service restart)
ansible-playbook -i inventory.yml playbook.yml --limit <hostname> --tags app
```

## HAT Variants

| HAT | Channels | Config |
|-----|----------|--------|
| ReSpeaker 2-Mics Pi HAT | 2ch/S16_LE | Default config |
| ReSpeaker 4-Mic Array (AC108) | 4ch/S32_LE | `use_arecord: true` required |

### AC108 4-Mic Special Rules

- PyAudio + onnxruntime in same process = kernel crash
- AC108 channel 0 is SILENT — mics on channels 1, 2, 3
- MUST use `use_arecord: true` to isolate I2S driver
- Set `OMP_NUM_THREADS=1` in systemd service
- Config: `device: "hw:0,0"`, `channels: 4`

## Legacy Deploy Script

```bash
./bin/deploy-satellite.sh [hostname] [user]
```

## Pre-Deploy Checklist

1. Confirm with user (explain brick risk)
2. Test SSH connection: `ssh <hostname> 'uptime'`
3. Check disk space: `ssh <hostname> 'df -h /'`
4. Prefer `--tags app` unless driver changes needed
5. After deploy: verify service status via `ssh <hostname> 'systemctl status renfield-satellite'`
