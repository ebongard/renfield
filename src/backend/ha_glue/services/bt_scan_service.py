"""
Bluetooth Scan Service — aggregate a fan-out discovery scan across satellites.

The backend has NO Bluetooth hardware: a "scan all bluetooth devices" chat
request is fanned out to every connected satellite (each runs a BLE + Classic
discovery via `BTDiscoveryScanner`), and this service merges the per-satellite
device lists into one deduplicated view:

- dedup by MAC (case-insensitive, upper),
- keep the strongest RSSI seen across all satellites,
- record which room/satellite saw the device (and at what RSSI),
- annotate an OUI vendor from the MAC prefix.

The LLM formats the returned dict for the user; this service does no
presentation. It never raises out of scan_all_satellites — a satellite that
times out or errors is simply counted as not-responded.
"""

from loguru import logger

# Common OUI (first 3 octets) → vendor. Not exhaustive — a best-effort label so
# the chat answer is more useful than a bare MAC. Unknown prefixes => "Unknown".
_OUI: dict[str, str] = {
    # Apple
    "A4:C3:F0": "Apple",
    "AC:DE:48": "Apple",
    "F0:18:98": "Apple",
    "3C:15:C2": "Apple",
    "D0:81:7A": "Apple",
    "B8:E8:56": "Apple",
    # Samsung
    "00:12:FB": "Samsung",
    "5C:0A:5B": "Samsung",
    "8C:77:12": "Samsung",
    "C8:19:F7": "Samsung",
    # Google
    "F4:F5:D8": "Google",
    "94:EB:2C": "Google",
    "3C:5A:B4": "Google",
    # Xiaomi
    "28:6C:07": "Xiaomi",
    "64:09:80": "Xiaomi",
    "F8:A4:5F": "Xiaomi",
    # Sony
    "00:13:A9": "Sony",
    "FC:F1:52": "Sony",
    # Intel
    "00:1B:77": "Intel",
    "3C:A9:F4": "Intel",
    "A0:88:69": "Intel",
    # Realtek
    "00:E0:4C": "Realtek",
    "52:54:00": "Realtek",
    # Espressif (ESP32 / ESP8266)
    "24:0A:C4": "Espressif",
    "30:AE:A4": "Espressif",
    "A4:CF:12": "Espressif",
    # Raspberry Pi
    "B8:27:EB": "Raspberry Pi",
    "DC:A6:32": "Raspberry Pi",
    "E4:5F:01": "Raspberry Pi",
    # Nordic Semiconductor (BLE beacons / dev kits)
    "C0:98:E5": "Nordic",
    "EB:30:1E": "Nordic",
    # Bose
    "04:52:C7": "Bose",
    # Microsoft
    "00:50:F2": "Microsoft",
}


def _oui_lookup(mac: str) -> str:
    """Map a MAC's first 3 octets to a vendor name ("Unknown" if not in _OUI)."""
    if not mac:
        return "Unknown"
    prefix = ":".join(mac.upper().split(":")[:3])
    return _OUI.get(prefix, "Unknown")


def _rssi_of(dev: dict):
    """RSSI as int or None (treat malformed values as missing)."""
    v = dev.get("rssi")
    if v is None:
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


class BtScanService:
    """Fan-out + aggregate a Bluetooth discovery scan across all satellites."""

    async def scan_all_satellites(
        self,
        satellite_manager,
        ble_duration: float = 10.0,
        classic_timeout: float = 12.0,
        per_sat_timeout: float = 30.0,
    ) -> dict:
        """
        Request a discovery scan from every connected satellite, in parallel, and
        merge the results.

        Returns:
            {
              "total_devices": int,
              "satellites_queried": int,
              "satellites_responded": int,
              "devices": [
                {"mac", "name", "rssi_best", "transport", "vendor", "rooms": [
                    {"satellite_id", "room", "rssi"}, ...
                ]}, ...  # sorted by rssi_best desc (None last)
              ],
            }
        """
        import asyncio

        sats = list(satellite_manager.satellites.values())
        params = {"ble_duration": ble_duration, "classic_timeout": classic_timeout}

        async def _scan(sat):
            return await satellite_manager.request_bt_scan(
                sat.satellite_id, params, timeout=per_sat_timeout
            )

        results = await asyncio.gather(
            *[_scan(sat) for sat in sats], return_exceptions=True
        )

        # MAC -> aggregate record.
        agg: dict[str, dict] = {}
        responded = 0
        for sat, result in zip(sats, results):
            if isinstance(result, Exception) or result is None:
                # Timed out / raised / unknown satellite => not responded.
                continue
            responded += 1
            for dev in result:
                mac = (dev.get("mac") or "").upper()
                if not mac:
                    continue
                rssi = _rssi_of(dev)
                entry = agg.get(mac)
                if entry is None:
                    entry = {
                        "mac": mac,
                        "name": dev.get("name") or None,
                        "rssi_best": rssi,
                        "transport": dev.get("transport") or None,
                        "vendor": _oui_lookup(mac),
                        "rooms": [],
                    }
                    agg[mac] = entry
                else:
                    # Fill a missing name / keep the strongest RSSI across rooms.
                    if not entry["name"] and dev.get("name"):
                        entry["name"] = dev.get("name")
                    if rssi is not None and (
                        entry["rssi_best"] is None or rssi > entry["rssi_best"]
                    ):
                        entry["rssi_best"] = rssi
                entry["rooms"].append({
                    "satellite_id": sat.satellite_id,
                    "room": sat.room,
                    "rssi": rssi,
                })

        # Strongest signal first; devices with no RSSI (e.g. Classic) sort last.
        devices = sorted(
            agg.values(),
            key=lambda d: (d["rssi_best"] is not None, d["rssi_best"] or -9999),
            reverse=True,
        )

        logger.info(
            f"BT scan: {len(devices)} devices from {responded}/{len(sats)} satellites"
        )
        return {
            "total_devices": len(devices),
            "satellites_queried": len(sats),
            "satellites_responded": responded,
            "devices": devices,
        }
