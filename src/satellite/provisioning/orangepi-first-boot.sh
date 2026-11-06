#!/bin/bash
# One-time setup of a freshly flashed Orange Pi Zero 3W satellite, before
# provision.yml. Written for the vendor image
# Orangepizero3w_1.0.0_ubuntu_jammy_server_linux6.6.98 (Küche, Esszimmer).
#
# Run AS ROOT on the board (root needs your SSH key in authorized_keys):
#   ./orangepi-first-boot.sh <hostname> [ansible_user]
# Then add the board to inventory.yml + host_vars (satellite_platform: orangepi)
# and run provision.yml. Idempotent.
#
# Why each step:
#  - the image ships a BAKED machine-id and SSH host keys: every board flashed
#    from it shares them with every other one (the identical machine-id also
#    collides in DHCP client ids), so both are regenerated once.
#  - the image has only root and `orangepi`; the playbook runs as the same
#    user as on the Pis, with passwordless sudo.
set -euo pipefail

NEW_HOSTNAME="${1:?usage: $0 <hostname> [ansible_user]}"
ANSIBLE_USER="${2:-evdb}"

[ "$(id -u)" -eq 0 ] || { echo "must run as root" >&2; exit 1; }
[ -s /root/.ssh/authorized_keys ] || { echo "/root/.ssh/authorized_keys is empty" >&2; exit 1; }

echo "1/3 hostname + fresh machine-id"
OLD_HOSTNAME=$(hostname)
hostnamectl set-hostname "$NEW_HOSTNAME"
sed -i "s/\b${OLD_HOSTNAME}\b/${NEW_HOSTNAME}/g" /etc/hosts
grep -q "$NEW_HOSTNAME" /etc/hosts || echo "127.0.1.1 $NEW_HOSTNAME" >> /etc/hosts
if [ ! -f /var/lib/renfield-identity-reset ]; then
  rm -f /etc/machine-id /var/lib/dbus/machine-id
  systemd-machine-id-setup
  ln -sf /etc/machine-id /var/lib/dbus/machine-id
  echo "2/3 fresh SSH host keys (reconnect and re-trust the host afterwards)"
  rm -f /etc/ssh/ssh_host_*
  ssh-keygen -A >/dev/null
  systemctl restart ssh
  touch /var/lib/renfield-identity-reset
else
  echo "2/3 identity already reset - skipped"
fi

echo "3/3 user $ANSIBLE_USER (SSH key from root, passwordless sudo)"
GROUPS_CSV=""
for g in sudo audio video plugdev i2c; do
  if getent group "$g" >/dev/null; then GROUPS_CSV+="$g,"; fi
done
id "$ANSIBLE_USER" >/dev/null 2>&1 || useradd -m -s /bin/bash -G "${GROUPS_CSV%,}" "$ANSIBLE_USER"
install -d -m700 -o "$ANSIBLE_USER" -g "$ANSIBLE_USER" "/home/$ANSIBLE_USER/.ssh"
install -m600 -o "$ANSIBLE_USER" -g "$ANSIBLE_USER" /root/.ssh/authorized_keys "/home/$ANSIBLE_USER/.ssh/authorized_keys"
echo "$ANSIBLE_USER ALL=(ALL) NOPASSWD:ALL" > "/etc/sudoers.d/010-$ANSIBLE_USER-nopasswd"
chmod 440 "/etc/sudoers.d/010-$ANSIBLE_USER-nopasswd"
visudo -cq

echo "done: $(hostname), $(id "$ANSIBLE_USER")"
