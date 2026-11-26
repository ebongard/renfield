"""
Classic Bluetooth Scanner for Renfield Satellite

Uses `hcitool name <MAC>` to detect Classic BT (BR/EDR) devices.
Apple devices (iPhone, Apple Watch) have permanent Classic BT MACs
that don't rotate like BLE addresses. This is the technique used by
the well-known 'monitor' project.

Key differences from BLE scanning:
- Presence is binary: a name response means "present". The remote-name
  request works even on non-discoverable devices, which is why it is
  preferred over an inquiry scan.
- Sequential scanning (one device at a time, BT Classic limitation)
- Slower: 1-5 seconds per device
- But: works with Apple devices that randomize BLE MACs

RSSI:
A plain name request carries no signal strength. To let the backend
arbitrate which room a device is in (strongest signal wins), we read a
real RSSI for each present device via a short-lived ACL connection
(`hcitool cc` -> `hcitool rssi` -> `hcitool dc`). This is best-effort:

- The value is the Bluetooth "golden receive power range" RSSI — a
  signed dB offset relative to the ideal range, NOT absolute dBm. It is
  frequently 0 when the link sits inside the golden range, so two
  satellites at similar distance can still tie.
- Some phones (notably iPhones) reject unsolicited ACL connections, so
  `cc` fails and no RSSI is available.

In every failure case we fall back to SYNTHETIC_RSSI so presence
detection is never lost — we only ever *gain* arbitration signal when
the device cooperates. Set `read_rssi=False` (config `ble.classic_rssi`)
to skip the connection attempt entirely and always report the synthetic
value.
"""

import asyncio
import re
import shutil
import time

# "RSSI return value: -4"
_RSSI_RE = re.compile(r"RSSI return value:\s*(-?\d+)")


class ClassicBTScanner:
    """
    Scans for known Classic Bluetooth devices using hcitool name requests.

    Each known MAC is queried sequentially. If the device responds with
    a name, it's considered present. A real RSSI is then read best-effort
    via a short-lived ACL connection; if that fails, SYNTHETIC_RSSI is
    used so presence is still reported.
    """

    SYNTHETIC_RSSI = -50  # Fallback "present" signal, and the golden-range baseline (offset 0)
    PRESENT_FLOOR = -79   # Never report below this: a present device must stay above the
                          # backend's BLE rssi_threshold (-80) or it would be filtered as absent

    def __init__(self, timeout: float = 5.0, read_rssi: bool = True,
                 rssi_interval: float = 300.0):
        self.timeout = timeout
        self.read_rssi = read_rssi
        # Reading RSSI needs an ACL connection, and connecting too often makes
        # the phone stop answering name requests (presence drops to "absent").
        # So read RSSI at most once per rssi_interval per device, cache it, and
        # report the cached value on every scan in between. Presence (name) still
        # runs every scan and is never gated on the RSSI read.
        self.rssi_interval = max(0.0, rssi_interval)  # negative is nonsensical; 0 = read every scan
        self._rssi_cache: dict[str, tuple[int, float]] = {}  # UPPER MAC -> (mapped_rssi, monotonic_ts)

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

        now = time.monotonic()
        known_upper = {m.upper() for m in known_macs}
        results = []
        for mac in known_macs:
            name = await self._query_name(mac)
            if not name:
                continue
            mac_u = mac.upper()
            results.append({
                "mac": mac_u,
                "rssi": await self._resolve_rssi(mac_u, now),
                # Not dBm: a golden-range offset mapped onto -50 (or that -50
                # itself). Presence, yes; a level to compare rooms by, no.
                "transport": "classic",
            })

        # Drop cached RSSI for MACs no longer in the whitelist (bounded growth).
        self._rssi_cache = {m: v for m, v in self._rssi_cache.items() if m in known_upper}
        return results

    async def _resolve_rssi(self, mac: str, now: float) -> int:
        """
        Return the RSSI to report for a present device, throttling real reads.

        Reads a fresh RSSI (via a connection) at most once per rssi_interval per
        device; otherwise returns the last cached value. On a read failure, keeps
        the last cached value if any, else the synthetic baseline. This keeps the
        connection churn low enough that name-based presence stays reliable.
        """
        if not self.read_rssi:
            return self.SYNTHETIC_RSSI

        cached = self._rssi_cache.get(mac)
        if cached is not None and (now - cached[1]) < self.rssi_interval:
            return cached[0]

        golden = await self._read_rssi(mac)
        if golden is not None:
            mapped = self._to_backend_rssi(golden)
            self._rssi_cache[mac] = (mapped, now)
            print(f"Classic BT RSSI {mac}: golden={golden} -> {mapped}")
            return mapped

        if cached is not None:
            print(f"Classic BT RSSI {mac}: read failed, reusing cached {cached[0]}")
            return cached[0]
        print(f"Classic BT RSSI {mac}: read failed, using synthetic {self.SYNTHETIC_RSSI}")
        return self.SYNTHETIC_RSSI

    def _to_backend_rssi(self, golden: int | None) -> int:
        """
        Map a Classic-BT golden-range RSSI offset onto the backend's dBm-ish scale.

        `hcitool rssi` returns a signed offset from the Bluetooth golden receive
        power range (0 = ideal, positive = stronger/closer, negative = weaker),
        NOT absolute dBm. The backend's room arbitration was calibrated for BLE
        dBm (threshold -80, confidence around -50..-90). We center the offset on
        the present-baseline so the value lands on that scale and preserves
        "closer satellite wins" ordering, and floor it so a present device never
        drops below the backend RSSI filter. None (no reading) => baseline.
        """
        if golden is None:
            return self.SYNTHETIC_RSSI
        return max(self.PRESENT_FLOOR, self.SYNTHETIC_RSSI + golden)

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

    async def _read_rssi(self, mac: str) -> int | None:
        """
        Best-effort real RSSI for a present device.

        Opens a short-lived ACL connection (`hcitool cc`), reads the link
        RSSI (`hcitool rssi`), then tears the connection down (`hcitool dc`).
        Returns the golden-range RSSI as a signed int, or None if any step
        fails (device refuses the connection, hcitool error, timeout). The
        caller falls back to SYNTHETIC_RSSI on None.
        """
        try:
            # Create the connection. May already exist (phone connected for
            # the name request); a non-zero exit here just means we couldn't
            # establish one, in which case `rssi` will fail and we bail.
            await self._run("cc", mac)
            out = await self._run("rssi", mac)
            match = _RSSI_RE.search(out or "")
            return int(match.group(1)) if match else None
        except Exception:
            return None
        finally:
            # Always tear down whatever connection we may have opened so we
            # don't leave the phone tied up between scans.
            try:
                await self._run("dc", mac)
            except Exception:
                pass

    async def _run(self, *args: str) -> str:
        """
        Run `sudo -n hcitool <args>`, returning stdout. Raises on failure/timeout.

        Creating an ACL connection / reading RSSI (cc/rssi/dc) needs
        CAP_NET_ADMIN. The satellite service runs as an unprivileged user
        that has passwordless sudo, so these go through `sudo -n`. `-n`
        never prompts: if sudo isn't passwordless the call fails fast and
        the caller falls back to SYNTHETIC_RSSI (presence is never lost).
        """
        proc = await asyncio.create_subprocess_exec(
            "sudo", "-n", "hcitool", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.timeout
            )
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
            raise
        if proc.returncode != 0:
            raise RuntimeError(
                (stderr.decode().strip() if stderr else "") or f"hcitool {args[0]} failed"
            )
        return stdout.decode()
