#!/bin/bash
# Orange Pi Zero 3W: the ReSpeaker XVF3800 (2886:001a) sometimes fails to
# enumerate after a warm reboot. Rebind the xHCI controller until it shows up;
# a no-op when it is already there.
for attempt in 1 2 3; do
  lsusb | grep -q "2886:001a" && exit 0
  for dev in $(ls /sys/bus/platform/drivers/xhci-hcd/ 2>/dev/null | grep "auto$"); do
    echo "$dev" > /sys/bus/platform/drivers/xhci-hcd/unbind 2>/dev/null || true
    sleep 2
    echo "$dev" > /sys/bus/platform/drivers/xhci-hcd/bind   2>/dev/null || true
  done
  sleep 3
done
lsusb | grep -q "2886:001a"
