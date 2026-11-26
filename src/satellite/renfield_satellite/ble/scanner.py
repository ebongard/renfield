"""
BLE Scanner for Renfield Satellite

Scans for known BLE devices (phones, watches) and reports RSSI values.
Uses bleak library for cross-platform BLE scanning.

Two modes:
  - Periodic (default, legacy): a discover() burst every scan_interval. Simple,
    works on any adapter (Pi Zero 2 W, BT 4.2).
  - Continuous (continuous=True, BT 5.x / mains-powered nodes): a single
    BleakScanner stays running with a detection callback; per-device RSSI is
    smoothed (EWMA) and reported with a freshness window. Lower presence latency
    and steadier RSSI for room arbitration. Requires no extra adapter features,
    but benefits from BlueZ Experimental (AdvertisementMonitor) being enabled.

What the backend's placement needs beyond the readings (#1551 stage 2 C):
  - ``status()``: the mode, the cutoff (``rssi_threshold`` — below it nothing is
    reported, so a silent receiver means "at or below the cutoff"), whether the
    scanner runs, and the age of the last advertisement of ANY device. That age
    is the BLE sign of life: a heartbeat proves the process, not the radio.
  - ``age_s`` per reading: how old the newest advertisement behind it is. The
    backend dates the reading by it instead of by the moment the report arrives.
  - In continuous mode a device is reported only when a NEW advertisement came
    in since the last report: repeating an old value made it look fresh.
"""

import time

try:
    from bleak import BleakScanner as _BleakScanner
    BLEAK_AVAILABLE = True
except ImportError:
    _BleakScanner = None
    BLEAK_AVAILABLE = False

from .rpa import is_resolvable_private_address, resolve_identity


class BLEScanner:
    """
    Scans for known BLE devices and returns RSSI values.

    Only reports devices whose MAC addresses are in the known_macs whitelist,
    ensuring privacy and efficiency.
    """

    def __init__(
        self,
        scan_duration: float = 5.0,
        rssi_threshold: int = -80,
        smoothing_alpha: float = 0.4,
        freshness_seconds: float = 20.0,
    ):
        self.scan_duration = scan_duration
        self.rssi_threshold = rssi_threshold
        self.smoothing_alpha = smoothing_alpha
        self.freshness_seconds = freshness_seconds

        # Continuous-mode state
        self._scanner = None
        self._known_macs: set[str] = set()
        # identity_name -> IRK (16 bytes, MSO-first) for resolving rotating RPAs
        self._irks: dict[str, bytes] = {}
        # tracking key -> [ewma_rssi, last_seen_monotonic, last_mac, identity,
        #                  last_seen_already_reported]
        # key is the MAC for whitelisted devices, or "id:<name>" for an
        # IRK-resolved identity (stable across the phone's RPA rotation).
        self._readings: dict[str, list] = {}
        # Monotonic time of the last advertisement of ANY device (sign of life),
        # and whether the last periodic burst ran without an error.
        self._last_advert_at: float | None = None
        self._periodic_ok = False

        if not BLEAK_AVAILABLE:
            print("Warning: bleak not installed. BLE scanning disabled.")

    @property
    def available(self) -> bool:
        return BLEAK_AVAILABLE

    # ---- Periodic mode (legacy) -------------------------------------------

    async def scan(self, known_macs: set[str]) -> list[dict]:
        """
        One discover() burst for known BLE devices.

        Args:
            known_macs: Set of MAC addresses (uppercase, colon-separated) to look for.

        Returns:
            List of dicts with 'mac' and 'rssi' for each detected known device.
        """
        if not BLEAK_AVAILABLE or (not known_macs and not self._irks):
            return []
        self._known_macs = {m.upper() for m in known_macs}

        try:
            devices = await _BleakScanner.discover(
                timeout=self.scan_duration,
                return_adv=True,
            )
        except Exception as e:
            self._periodic_ok = False
            print(f"BLE scan error: {e}")
            return []
        self._periodic_ok = True
        if devices:
            self._last_advert_at = time.monotonic()

        results = []
        # devices is dict {BLEDevice: AdvertisementData} when return_adv=True
        for device, adv_data in devices.values():
            mac = (device.address or "").upper()
            key, identity = self._classify(mac)
            if key is None:
                continue
            rssi = adv_data.rssi
            if rssi is not None and rssi >= self.rssi_threshold:
                # Heard within this burst; the burst's length is the bound.
                entry = {"mac": mac, "rssi": rssi, "age_s": 0.0}
                if identity:
                    entry["identity"] = identity
                results.append(entry)

        return results

    # ---- Continuous mode --------------------------------------------------

    def _classify(self, mac: str):
        """Map a discovered address to (tracking_key, identity).

        Returns (mac, None) for a whitelisted MAC, ("id:<name>", "<name>") for
        an IRK-resolved rotating RPA, or (None, None) if it's neither.
        """
        if mac in self._known_macs:
            return mac, None
        if self._irks and is_resolvable_private_address(mac):
            identity = resolve_identity(mac, self._irks)
            if identity is not None:
                return "id:" + identity, identity
        return None, None

    def update_known(self, known_macs: set[str]) -> None:
        """Update the known-MAC whitelist the detection callback filters on
        (the backend pushes updates while the continuous scanner keeps running)."""
        self._known_macs = {m.upper() for m in known_macs}
        # Drop readings for whitelisted MACs no longer tracked (leave id: keys,
        # which are managed by update_irks).
        for key in list(self._readings):
            if not key.startswith("id:") and key not in self._known_macs:
                self._readings.pop(key, None)

    def update_irks(self, irks: dict) -> None:
        """Set the identity->IRK map (16-byte IRKs, MSO-first) used to resolve
        rotating RPAs. Drops readings for identities no longer tracked."""
        self._irks = {
            name: bytes(irk)
            for name, irk in (irks or {}).items()
            if irk and len(irk) == 16
        }
        valid = {"id:" + n for n in self._irks}
        for key in list(self._readings):
            if key.startswith("id:") and key not in valid:
                self._readings.pop(key, None)

    def _on_detection(self, device, adv_data) -> None:
        """bleak detection callback: fold each advertisement into a per-device
        (or per-resolved-identity) EWMA RSSI. Cheap; runs on the event loop.
        Every advertisement, of any device, is the radio's sign of life."""
        now = time.monotonic()
        self._last_advert_at = now
        rssi = getattr(adv_data, "rssi", None)
        if rssi is None:
            return
        mac = (device.address or "").upper()
        key, identity = self._classify(mac)
        if key is None:
            return
        entry = self._readings.get(key)
        if entry is None:
            self._readings[key] = [float(rssi), now, mac, identity, None]
        else:
            a = self.smoothing_alpha
            entry[0] = a * float(rssi) + (1.0 - a) * entry[0]
            entry[1] = now
            entry[2] = mac  # remember the latest (rotating) RPA seen

    async def start_continuous(self, known_macs: set[str]) -> bool:
        """Start a single long-running scanner with the detection callback.
        Returns True if started (or already running), False if unavailable."""
        if not BLEAK_AVAILABLE:
            return False
        self.update_known(known_macs)
        if self._scanner is not None:
            return True
        try:
            self._scanner = _BleakScanner(detection_callback=self._on_detection)
            await self._scanner.start()
            print("BLE continuous scanner started")
            return True
        except Exception as e:
            print(f"BLE continuous start error: {e}")
            self._scanner = None
            return False

    async def stop_continuous(self) -> None:
        if self._scanner is not None:
            try:
                await self._scanner.stop()
            except Exception as e:
                print(f"BLE continuous stop error: {e}")
            self._scanner = None
        self._readings.clear()

    def get_readings(self) -> list[dict]:
        """Above-threshold devices with a NEW advertisement since the last call,
        with the smoothed RSSI and the age of that advertisement. Prunes stale
        entries (unseen > freshness_seconds)."""
        now = time.monotonic()
        results = []
        for key in list(self._readings):
            entry = self._readings[key]
            rssi, last_seen, mac, identity, reported = entry
            if now - last_seen > self.freshness_seconds:
                self._readings.pop(key, None)
                continue
            if reported is not None and last_seen <= reported:
                continue  # nothing new: the backend already has this value
            entry[4] = last_seen
            rssi_i = int(round(rssi))
            if rssi_i >= self.rssi_threshold:
                reading = {"mac": mac, "rssi": rssi_i, "age_s": round(now - last_seen, 2)}
                if identity:
                    reading["identity"] = identity
                results.append(reading)
        return results

    def status(self, mode: str) -> dict:
        """What the backend needs to read a silence: the mode actually running
        (``continuous`` or ``periodic``), the cutoff, whether the scanner runs,
        and the age of the last advertisement of any device (None = none yet)."""
        running = self._scanner is not None if mode == "continuous" else self._periodic_ok
        age = None
        if self._last_advert_at is not None:
            age = round(max(0.0, time.monotonic() - self._last_advert_at), 2)
        return {
            "mode": mode,
            "running": bool(running),
            "cutoff_dbm": self.rssi_threshold,
            "last_advert_age_s": age,
        }
