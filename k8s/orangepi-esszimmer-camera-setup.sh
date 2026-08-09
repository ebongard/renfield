#!/usr/bin/env bash
# Reproducible HOST-side camera bring-up for the Esszimmer A733 satellite node
# (Orange Pi Zero 3W / sun60iw2). Run as root ON THE NODE from a repo checkout:
#
#     sudo ./k8s/orangepi-esszimmer-camera-setup.sh
#
# WHY THIS EXISTS
# ---------------
# The Esszimmer satellite runs as a k8s pod (k8s/satellite-esszimmer.yaml, git-managed).
# But a CSI camera needs HOST-level pieces a pod can't provide — a device-tree overlay,
# a kernel module autoload, and the Allwinner ISP userspace. Those were bring-up'd by hand
# during development; this script makes them reproducible so a reflash/replacement of the
# node restores the camera. It mirrors the committed-node-script pattern of
# k8s/orangepi-node-resilience.sh (this node's host is NOT the bare-metal-satellite Ansible
# target — those Pi Zero 2 W sats have no A733/ISP and no camera).
#
# Idempotent: safe to re-run. A reboot is required after first run (for the DT overlay).
#
# Layer split:
#   HOST (this script):  DT overlay + module autoload + /opt/awisp (tool + AW ISP libs)
#   POD (k8s manifest):  hostPath-mounts /dev/video0, /dev/media0, /opt/awisp (already in git)
set -euo pipefail

[[ $EUID -eq 0 ]] || { echo "run as root (sudo)"; exit 1; }
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PROTO="$REPO/prototypes/npu-occupancy"
OVL_DIR=/boot/dtb/allwinner/overlay
ENVFILE=/boot/orangepiEnv.txt
AWISP=/opt/awisp

echo "== 1. Device-tree overlay (IMX219 on CAM1/CSI0) =="
# Compile the in-repo .dts → .dtbo and install under the overlay_prefix convention.
tmp=$(mktemp -d)
cpp -nostdinc -undef -x assembler-with-cpp \
    "$PROTO/dts/sun60i-a733-orangepi-zero3w-cam-imx219.dts" \
  | dtc -@ -I dts -O dtb -o "$tmp/sun60i-a733-cam-imx219.dtbo" - 2>/dev/null
install -D -m0644 "$tmp/sun60i-a733-cam-imx219.dtbo" "$OVL_DIR/sun60i-a733-cam-imx219.dtbo"
rm -rf "$tmp"
# Enable it (overlay_prefix=sun60i-a733 → entry is the suffix "cam-imx219"). Idempotent.
if grep -q '^overlays=' "$ENVFILE"; then
  grep -qw cam-imx219 "$ENVFILE" || sed -i 's/^overlays=\(.*\)/overlays=\1 cam-imx219/' "$ENVFILE"
else
  echo 'overlays=cam-imx219' >> "$ENVFILE"
fi
echo "   installed overlay + $(grep '^overlays=' "$ENVFILE")"

echo "== 2. Module autoload (sunxi-vin is =m and does NOT autoload) =="
cat > /etc/modules-load.d/renfield-camera.conf <<'EOF'
vin_io
imx219
vin_v4l2
EOF
echo "   wrote /etc/modules-load.d/renfield-camera.conf"

echo "== 3. Allwinner ISP userspace + capture tool (/opt/awisp) =="
install -d "$AWISP/lib"
# 3a. AW ISP libs — Allwinner PROPRIETARY, extracted from the OPi Zero 3W *desktop* image
#     (pkg libawispapi-isp-602: libAWIspApi.so, libisp.so, libisp_ini.so + AWIspApi.h).
#     They are NOT in git. Stage them next to this script (or already present in /opt/awisp/lib).
need_libs=(libAWIspApi.so libisp.so libisp_ini.so)
missing=0; for l in "${need_libs[@]}"; do [[ -f "$AWISP/lib/$l" ]] || missing=1; done
if [[ $missing -eq 1 ]]; then
  if [[ -d "$REPO/private/awisp/lib" ]]; then
    cp -a "$REPO/private/awisp/lib/." "$AWISP/lib/"
    [[ -f "$REPO/private/awisp/AWIspApi.h" ]] && cp "$REPO/private/awisp/AWIspApi.h" "$AWISP/"
    echo "   installed AW ISP libs from private/awisp/"
  else
    echo "   !! AW ISP libs missing. Extract from the OPi Zero 3W desktop image:"
    echo "      7z x <img.7z>; loop-mount; copy /usr/lib/aarch64-linux-gnu/{libAWIspApi,libisp,libisp_ini}.so"
    echo "      + /usr/include/AWIspApi.h  →  $AWISP/lib/ and $AWISP/  (or repo private/awisp/)."
    echo "      (proprietary — never commit to git). See docs/design/a733-satellite-camera.md."
    exit 2
  fi
fi
# 3b. Build the capture tool from the in-repo source against the AW header + lib.
cp "$PROTO/isp_capture/renfield_isp_capture.c" "$AWISP/"
gcc -O2 -o "$AWISP/renfield_isp_capture" "$AWISP/renfield_isp_capture.c" \
    -I"$AWISP" -L"$AWISP/lib" -lAWIspApi -Wl,-rpath,"$AWISP/lib"
echo "   built $AWISP/renfield_isp_capture"

echo
echo "DONE. If the overlay was newly added, REBOOT for /dev/video0 to appear:"
echo "   reboot"
echo "Then verify:  ls /dev/video0 ; LD_LIBRARY_PATH=$AWISP/lib $AWISP/renfield_isp_capture /dev/video0 1920 1080 /tmp/f.i420 8"
echo "The pod already hostPath-mounts /dev/video0 + /dev/media0 + /opt/awisp (k8s/satellite-esszimmer.yaml)."
