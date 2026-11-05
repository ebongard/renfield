#!/bin/bash
# Build the netfilter modules calico needs (ipset set types, xt_set, ipip) for the
# vendor kernel 6.6.98-sun60iw2, which ships IP_SET core only. Recipe from the
# Esszimmer node (docs/writeups/orangepi-calico-thermal-kernel-fix.md). Run AS ROOT
# after orangepi-node-prep.sh, before kubeadm join. ~15 min on the capped SoC.
set -euo pipefail
KREL=$(uname -r)
[ "$KREL" = "6.6.98-sun60iw2" ] || { echo "unexpected kernel $KREL" >&2; exit 1; }
SRC=/usr/src/ksrc/linux-6.6.98
DEST=/lib/modules/$KREL/kernel/net/netfilter/ipset
BACKUP=/opt/calico-kmods-$KREL

echo "0/6 cap CPU at 1.2 GHz (fanless board; same cap as the Esszimmer)"
cat > /etc/systemd/system/cpu-freq-cap.service <<'UNIT'
[Unit]
Description=Cap CPU frequency at 1.2 GHz (fanless board)

[Service]
Type=oneshot
ExecStart=/bin/sh -c 'for p in /sys/devices/system/cpu/cpufreq/policy*; do echo 1196000 > $p/scaling_max_freq; done'
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now cpu-freq-cap.service

echo "1/6 headers + toolchain"
[ -f "/lib/modules/$KREL/build/Module.symvers" ] || dpkg -i /opt/linux-headers-current-sun60iw2_1.0.0_arm64.deb
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq build-essential bc flex bison libssl-dev libelf-dev xz-utils

echo "2/6 upstream source"
mkdir -p /usr/src/ksrc
if [ ! -d "$SRC" ]; then
  curl -fsSL https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.6.98.tar.xz -o /usr/src/ksrc/linux-6.6.98.tar.xz
  tar -xJf /usr/src/ksrc/linux-6.6.98.tar.xz -C /usr/src/ksrc
fi
cd "$SRC"

echo "3/6 config = running kernel + the missing types; LOCALVERSION for vermagic"
zcat /proc/config.gz > .config
./scripts/config --set-str CONFIG_LOCALVERSION "-sun60iw2" \
  --module CONFIG_NETFILTER_XT_SET --module CONFIG_NET_IPIP \
  --module CONFIG_IP_SET_BITMAP_IP --module CONFIG_IP_SET_BITMAP_IPMAC --module CONFIG_IP_SET_BITMAP_PORT \
  --module CONFIG_IP_SET_HASH_IP --module CONFIG_IP_SET_HASH_IPMARK --module CONFIG_IP_SET_HASH_IPPORT \
  --module CONFIG_IP_SET_HASH_IPPORTIP --module CONFIG_IP_SET_HASH_IPPORTNET --module CONFIG_IP_SET_HASH_IPMAC \
  --module CONFIG_IP_SET_HASH_MAC --module CONFIG_IP_SET_HASH_NETPORTNET --module CONFIG_IP_SET_HASH_NET \
  --module CONFIG_IP_SET_HASH_NETNET --module CONFIG_IP_SET_HASH_NETPORT --module CONFIG_IP_SET_HASH_NETIFACE \
  --module CONFIG_IP_SET_LIST_SET
make olddefconfig >/dev/null
cp "/lib/modules/$KREL/build/Module.symvers" .

echo "4/6 build (modules_prepare + explicit .ko targets)"
make -j4 modules_prepare >/dev/null
KOS=$(grep -E '^CONFIG_IP_SET_(BITMAP|HASH|LIST)_[A-Z_]+=m' .config | sed -E 's/^CONFIG_IP_SET_([A-Z_]+)=m/\1/' \
  | tr 'A-Z' 'a-z' | sed -E 's/^(bitmap|hash|list)_(.*)/net\/netfilter\/ipset\/ip_set_\1_\2.ko/')
make -j4 KBUILD_MODPOST_WARN=1 $KOS net/netfilter/xt_set.ko net/ipv4/ipip.ko >/dev/null

echo "5/6 verify vermagic, install, back up"
WANT=$(modinfo -F vermagic "/lib/modules/$KREL/kernel/net/netfilter/ipset/ip_set.ko")
mkdir -p "$BACKUP"
for ko in $KOS net/netfilter/xt_set.ko net/ipv4/ipip.ko; do
  [ "$(modinfo -F vermagic "$ko")" = "$WANT" ] || { echo "vermagic mismatch: $ko" >&2; exit 1; }
  cp "$ko" "$BACKUP/"
done
for ko in $KOS net/netfilter/xt_set.ko; do install -m644 "$ko" "$DEST/"; done
install -m644 net/ipv4/ipip.ko "/lib/modules/$KREL/kernel/net/ipv4/"
depmod -a

echo "6/6 load + boot persistence + smoke test"
MODS=$(for ko in $KOS; do basename "$ko" .ko; done)
printf '%s\n' $MODS xt_set ipip > /etc/modules-load.d/calico-ipset.conf
for m in $MODS xt_set ipip; do modprobe "$m"; done
ipset create renfield-smoke hash:net && ipset destroy renfield-smoke
echo "OK: $(ls "$BACKUP" | wc -l) modules built, loaded and backed up to $BACKUP"
