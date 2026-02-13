"""
BLE Scanner for Renfield Satellite

Scans for known BLE devices (phones, watches) and reports RSSI values.
Uses bleak library for cross-platform BLE scanning.
Pi Zero 2 W has Bluetooth 4.2 built-in.
"""

try:
    from bleak import BleakScanner as _BleakScanner
    BLEAK_AVAILABLE = True
except ImportError:
    _BleakScanner = None
    BLEAK_AVAILABLE = False


class BLEScanner:
    """
    Scans for known BLE devices and returns RSSI values.

    Only reports devices whose MAC addresses are in the known_macs whitelist,
    ensuring privacy and efficiency.
    """

    def __init__(self, scan_duration: float = 5.0, rssi_threshold: int = -80):
        self.scan_duration = scan_duration
        self.rssi_threshold = rssi_threshold

        if not BLEAK_AVAILABLE:
            print("Warning: bleak not installed. BLE scanning disabled.")

    @property
    def available(self) -> bool:
        return BLEAK_AVAILABLE

    async def scan(self, known_macs: set[str]) -> list[dict]:
        """
        Scan for known BLE devices.

        Args:
            known_macs: Set of MAC addresses (uppercase, colon-separated) to look for.

        Returns:
            List of dicts with 'mac' and 'rssi' for each detected known device.
        """
        if not BLEAK_AVAILABLE or not known_macs:
            return []

        try:
            devices = await _BleakScanner.discover(
                timeout=self.scan_duration,
                return_adv=True,
            )
        except Exception as e:
            print(f"BLE scan error: {e}")
            return []

        results = []
        # devices is dict {BLEDevice: AdvertisementData} when return_adv=True
        for device, adv_data in devices.values():
            mac = (device.address or "").upper()
            if mac in known_macs:
                rssi = adv_data.rssi
                if rssi is not None and rssi >= self.rssi_threshold:
                    results.append({"mac": mac, "rssi": rssi})

        return results
