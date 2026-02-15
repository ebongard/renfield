"""
Classic Bluetooth Scanner for Renfield Satellite

Uses `hcitool name <MAC>` to detect Classic BT (BR/EDR) devices.
Apple devices (iPhone, Apple Watch) have permanent Classic BT MACs
that don't rotate like BLE addresses. This is the technique used by
the well-known 'monitor' project.

Key differences from BLE scanning:
- No RSSI available — we use a fixed value (-50) as "present"
- Sequential scanning (one device at a time, BT Classic limitation)
- Slower: 1-5 seconds per device
- But: works with Apple devices that randomize BLE MACs
"""

import asyncio
import shutil


class ClassicBTScanner:
    """
    Scans for known Classic Bluetooth devices using hcitool name requests.

    Each known MAC is queried sequentially. If the device responds with
    a name, it's considered present. No RSSI is available — a fixed
    value of -50 is used as a synthetic "present" signal.
    """

    SYNTHETIC_RSSI = -50  # Fixed RSSI for "device is present"

    def __init__(self, timeout: float = 5.0):
        self.timeout = timeout

    @property
    def available(self) -> bool:
        """Check if hcitool is installed."""
        return shutil.which("hcitool") is not None

    async def scan(self, known_macs: set[str]) -> list[dict]:
        """
        Scan for known Classic BT devices.

        Args:
            known_macs: Set of MAC addresses (uppercase, colon-separated).

        Returns:
            List of dicts with 'mac' and 'rssi' for each detected device.
        """
        if not known_macs or not self.available:
            return []

        results = []
        for mac in known_macs:
            name = await self._query_name(mac)
            if name:
                results.append({"mac": mac.upper(), "rssi": self.SYNTHETIC_RSSI})

        return results

    async def _query_name(self, mac: str) -> str | None:
        """
        Query a Classic BT device name via hcitool.

        Returns the device name if it responds, None if timeout or error.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "hcitool", "name", mac,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=self.timeout
            )
            name = stdout.decode().strip()
            return name if name else None
        except asyncio.TimeoutError:
            # Device not in range or not responding
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
            return None
        except Exception as e:
            print(f"Classic BT query error for {mac}: {e}")
            return None
