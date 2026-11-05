#!/bin/bash
# Host prep for an Orange Pi Zero 3W that will host a k8s satellite pod
# (k8s/satellite-<room>.yaml). Written for the vendor image
# Orangepizero3w_1.0.0_ubuntu_jammy_server_linux6.6.98 — the one both the
# Esszimmer and the Küche run.
#
# Run AS ROOT on the fresh board:  ./orangepi-node-prep.sh orangepi-<room>
# Then: orangepi-calico-kmods.sh, orangepi-node-resilience.sh, kubeadm join.
# Idempotent.
#
# Why each step:
#  - the image ships a BAKED machine-id and SSH host keys: every board flashed
#    from it shares them with every other one (and the identical machine-id
#    collides in DHCP client ids and in the kubelet), so both are regenerated once.
#  - kubeadm refuses to run with swap; the image enables zram swap.
#  - the vendor containerd ships with the CRI plugin disabled.
#  - kubelet/kubeadm are pinned to the cluster's minor and held; the kernel is
#    held because the calico modules are built for exactly this kernel.
#  - Bluetooth is soft-blocked on the image; BLE presence needs it.
set -euo pipefail

NEW_HOSTNAME="${1:?usage: $0 <hostname>   e.g. orangepi-kueche}"
KUBE_VERSION="${KUBE_VERSION:-1.35.6}"
KUBE_MINOR="${KUBE_VERSION%.*}"

[ "$(id -u)" -eq 0 ] || { echo "must run as root" >&2; exit 1; }

echo "1/8 hostname + fresh machine-id"
OLD_HOSTNAME=$(hostname)
hostnamectl set-hostname "$NEW_HOSTNAME"
sed -i "s/\b${OLD_HOSTNAME}\b/${NEW_HOSTNAME}/g" /etc/hosts
grep -q "$NEW_HOSTNAME" /etc/hosts || echo "127.0.1.1 $NEW_HOSTNAME" >> /etc/hosts
if [ ! -f /var/lib/renfield-identity-reset ]; then
  rm -f /etc/machine-id /var/lib/dbus/machine-id
  systemd-machine-id-setup
  ln -sf /etc/machine-id /var/lib/dbus/machine-id
  echo "2/8 fresh SSH host keys (reconnect and re-trust the host afterwards)"
  rm -f /etc/ssh/ssh_host_*
  ssh-keygen -A >/dev/null
  systemctl restart ssh
  touch /var/lib/renfield-identity-reset
else
  echo "2/8 identity already reset - skipped"
fi

echo "3/8 swap off"
systemctl disable --now orangepi-zram-config.service || true
swapoff -a
sed -i -E '/^[^#].*\sswap\s/ s/^/#/' /etc/fstab

echo "4/8 kernel modules + sysctl for k8s"
printf 'overlay\nbr_netfilter\n' > /etc/modules-load.d/k8s.conf
modprobe overlay
modprobe br_netfilter
cat > /etc/sysctl.d/99-kubernetes.conf <<'EOF'
net.bridge.bridge-nf-call-iptables = 1
net.bridge.bridge-nf-call-ip6tables = 1
net.ipv4.ip_forward = 1
EOF
sysctl --system >/dev/null

echo "5/8 containerd: CRI on + SystemdCgroup"
cp -n /etc/containerd/config.toml /etc/containerd/config.toml.orig || true
containerd config default > /etc/containerd/config.toml
sed -i 's/SystemdCgroup = false/SystemdCgroup = true/' /etc/containerd/config.toml
grep -q 'SystemdCgroup = true' /etc/containerd/config.toml
systemctl restart containerd

echo "6/8 kubelet/kubeadm/kubectl $KUBE_VERSION"
mkdir -p /etc/apt/keyrings
curl -fsSL "https://pkgs.k8s.io/core:/stable:/v${KUBE_MINOR}/deb/Release.key" \
  | gpg --batch --yes --dearmor -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg
echo "deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v${KUBE_MINOR}/deb/ /" \
  > /etc/apt/sources.list.d/kubernetes.list
apt-get update -qq
KVER=$(apt-cache madison kubeadm | awk -v v="$KUBE_VERSION" '$3 ~ "^"v"-" {print $3; exit}')
[ -n "$KVER" ] || { echo "kubeadm $KUBE_VERSION not found in repo" >&2; exit 1; }
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq kubelet="$KVER" kubeadm="$KVER" kubectl="$KVER" ipset
apt-mark hold kubelet kubeadm kubectl
systemctl enable kubelet

echo "7/8 hold the kernel (calico modules are built for exactly this kernel)"
apt-mark hold linux-image-current-sun60iw2 linux-dtb-current-sun60iw2

echo "8/8 Bluetooth on + XVF3800 USB re-enumeration at boot"
rfkill unblock bluetooth
install -m644 "$(dirname "$0")/renfield-usb-fix.service" /etc/systemd/system/renfield-usb-fix.service
systemctl daemon-reload
systemctl enable renfield-usb-fix.service

echo "done: $(hostname), kubeadm $(kubeadm version -o short), swap devices: $(swapon --show --noheadings | wc -l)"
