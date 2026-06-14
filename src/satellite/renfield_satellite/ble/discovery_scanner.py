"""
Bluetooth Discovery Scanner for Renfield Satellite.

Unlike the presence scanners (`BLEScanner` / `ClassicBTScanner`), which probe a
KNOWN whitelist of MACs to decide who is home, this scanner answers the chat
question "scan all bluetooth devices": it does a broad, no-filter DISCOVERY of
every advertising/discoverable device in radio range, for both transports:

- BLE: `BleakScanner.discover` (passive advertisement listen).
- Classic BR/EDR: an `hcitool scan` inquiry (only finds DISCOVERABLE devices —
  most phones are not discoverable, so Classic results are usually sparse).

The Pi has a SINGLE Bluetooth controller, so the two sub-scans are run
SEQUENTIALLY — a BLE scan and a Classic inquiry at the same time fight over the
one radio and both come back empty/garbled. discover() NEVER raises: a failing
sub-scan logs and contributes whatever it managed to collect, so the backend
always gets a (possibly partial) list rather than an error.

Each result dict: {"mac": <UPPER>, "name": str|None, "rssi": int|None,
"transport": "BLE"|"Classic"}. RSSI is None for Classic (an inquiry carries no
per-device signal strength on this path).
"""

import asyncio
import re
import shutil

try:
    from bleak import BleakScanner
    BLEAK_AVAILABLE = True
except ImportError:  # pragma: no cover - bleak missing only on non-Pi dev boxes
    BleakScanner = None  # type: ignore
    BLEAK_AVAILABLE = False

# A discoverable device line from `hcitool scan`:  "\t<MAC>\t<Name>".
# The first output line is the "Scanning ..." header, which has no MAC.
_HCITOOL_LINE_RE = re.compile(
    r"^\s*([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})\s+(.*)$"
)


class BTDiscoveryScanner:
    """Broad, no-filter Bluetooth discovery across BLE + Classic transports."""

    @property
    def classic_available(self) -> bool:
        """Classic inquiry needs hcitool; absent on a controller-less dev box."""
        return shutil.which("hcitool") is not None

    async def discover(
        self, ble_duration: float = 10.0, classic_timeout: float = 12.0
    ) -> list[dict]:
        """
        Discover all advertising/discoverable BT devices in range.

        Runs Classic inquiry first, then BLE — sequentially, since the Pi has a
        single BT controller. Never raises: a sub-scan error logs and returns
        what it has.

        Args:
            ble_duration: Seconds to listen for BLE advertisements.
            classic_timeout: Inquiry length hint for `hcitool scan`. The actual
                subprocess wait is classic_timeout + 3 (hcitool overhead).

        Returns:
            List of {"mac", "name", "rssi", "transport"} dicts.
        """
        results: list[dict] = []
        # Classic first, then BLE (one controller — must not overlap).
        results.extend(await self._discover_classic(classic_timeout))
        results.extend(await self._discover_ble(ble_duration))
        return results

    async def _discover_ble(self, ble_duration: float) -> list[dict]:
        """BLE advertisement scan via bleak. No MAC filter — every device."""
        if not BLEAK_AVAILABLE:
            print("BT discovery: bleak not available, skipping BLE scan")
            return []
        out: list[dict] = []
        try:
            # return_adv=True yields {address: (BLEDevice, AdvertisementData)} so
            # we can read the per-advertisement RSSI (device.rssi is deprecated).
            discovered = await BleakScanner.discover(
                timeout=ble_duration, return_adv=True
            )
            for device, adv in discovered.values():
                addr = getattr(device, "address", None)
                if not addr:
                    continue
                out.append({
                    "mac": str(addr).upper(),
                    "name": getattr(device, "name", None) or None,
                    "rssi": getattr(adv, "rssi", None),
                    "transport": "BLE",
                })
        except Exception as e:  # noqa: BLE001 - never propagate out of discover()
            print(f"BT discovery: BLE scan failed: {e}")
        return out

    async def _discover_classic(self, classic_timeout: float) -> list[dict]:
        """Classic BR/EDR inquiry via `hcitool scan`. BLE-only if hcitool absent."""
        if not self.classic_available:
            return []
        out: list[dict] = []
        try:
            proc = await asyncio.create_subprocess_exec(
                "hcitool", "scan", "--length", "8",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, _ = await asyncio.wait_for(
                    proc.communicate(), timeout=classic_timeout + 3
                )
            except asyncio.TimeoutError:
                try:
                    proc.kill()
                    await proc.wait()
                except ProcessLookupError:
                    pass
                print("BT discovery: Classic inquiry timed out")
                return out
            for line in stdout.decode(errors="replace").splitlines():
                m = _HCITOOL_LINE_RE.match(line)
                if not m:
                    # The "Scanning ..." header and blank lines fall here.
                    continue
                mac, name = m.group(1).upper(), m.group(2).strip()
                out.append({
                    "mac": mac,
                    "name": name or None,
                    "rssi": None,
                    "transport": "Classic",
                })
        except Exception as e:  # noqa: BLE001 - never propagate out of discover()
            print(f"BT discovery: Classic inquiry failed: {e}")
        return out
