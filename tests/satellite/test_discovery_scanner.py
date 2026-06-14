"""
Unit tests for the broad Bluetooth discovery scanner (BTDiscoveryScanner).

Covers the no-filter BLE scan (every advertising device returned), the Classic
`hcitool scan` inquiry parsing, and the never-raise guarantees:
- a BLE bleak error contributes nothing but doesn't crash discover(),
- an hcitool error / timeout yields an empty Classic list (no raise),
- a missing hcitool (shutil.which None) falls back to BLE-only.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from renfield_satellite.ble import discovery_scanner
from renfield_satellite.ble.discovery_scanner import BTDiscoveryScanner


def _ble_pair(address, name, rssi):
    """Build a (BLEDevice, AdvertisementData)-shaped pair for BleakScanner."""
    device = SimpleNamespace(address=address, name=name)
    adv = SimpleNamespace(rssi=rssi)
    return device, adv


def _fake_discover(pairs):
    """Replacement for BleakScanner.discover(return_adv=True): {addr: (dev, adv)}."""
    async def _discover(*args, **kwargs):
        return {p[0].address: p for p in pairs}
    return _discover


def _hcitool_proc(stdout: bytes = b"", returncode: int = 0, raise_timeout: bool = False):
    """A fake asyncio subprocess for `hcitool scan`."""
    class _FakeProc:
        def __init__(self):
            self.returncode = returncode

        async def communicate(self):
            if raise_timeout:
                raise asyncio.TimeoutError()
            return stdout, b""

        async def wait(self):
            return self.returncode

        def kill(self):
            pass

    async def _create(*args, **kwargs):
        return _FakeProc()

    return _create


_HCITOOL_OUTPUT = (
    b"Scanning ...\n"
    b"\t4C:E6:C0:27:52:93\tPixel 7\n"
    b"\tAA:BB:CC:DD:EE:FF\tLiving Room Speaker\n"
)


@pytest.mark.satellite
@pytest.mark.asyncio
async def test_ble_returns_all_devices_no_filter():
    """Every advertising BLE device is returned (no whitelist), transport=BLE."""
    pairs = [
        _ble_pair("11:22:33:44:55:66", "Watch", -40),
        _ble_pair("77:88:99:AA:BB:CC", None, -90),
    ]
    scanner = BTDiscoveryScanner()
    with patch("renfield_satellite.ble.discovery_scanner.shutil.which", return_value=None), \
         patch.object(discovery_scanner, "BLEAK_AVAILABLE", True), \
         patch.object(discovery_scanner, "BleakScanner",
                      SimpleNamespace(discover=_fake_discover(pairs))):
        result = await scanner.discover(ble_duration=1.0, classic_timeout=1.0)

    macs = {d["mac"] for d in result}
    assert macs == {"11:22:33:44:55:66", "77:88:99:AA:BB:CC"}
    assert all(d["transport"] == "BLE" for d in result)
    by_mac = {d["mac"]: d for d in result}
    assert by_mac["11:22:33:44:55:66"]["rssi"] == -40
    assert by_mac["11:22:33:44:55:66"]["name"] == "Watch"
    assert by_mac["77:88:99:AA:BB:CC"]["name"] is None


@pytest.mark.satellite
@pytest.mark.asyncio
async def test_classic_lines_parsed():
    """`hcitool scan` lines parse into Classic devices; the header is skipped."""
    scanner = BTDiscoveryScanner()
    with patch("renfield_satellite.ble.discovery_scanner.shutil.which",
               return_value="/usr/bin/hcitool"), \
         patch.object(discovery_scanner, "BLEAK_AVAILABLE", False), \
         patch("asyncio.create_subprocess_exec",
               side_effect=_hcitool_proc(stdout=_HCITOOL_OUTPUT)):
        result = await scanner.discover(ble_duration=1.0, classic_timeout=1.0)

    classic = [d for d in result if d["transport"] == "Classic"]
    assert {d["mac"] for d in classic} == {
        "4C:E6:C0:27:52:93", "AA:BB:CC:DD:EE:FF"
    }
    # RSSI is None for Classic; the "Scanning ..." header produced no row.
    assert all(d["rssi"] is None for d in classic)
    by_mac = {d["mac"]: d for d in classic}
    assert by_mac["4C:E6:C0:27:52:93"]["name"] == "Pixel 7"


@pytest.mark.satellite
@pytest.mark.asyncio
async def test_classic_error_yields_empty_no_raise():
    """An hcitool error returns an empty Classic list without raising."""
    async def _boom(*args, **kwargs):
        raise OSError("hcitool exploded")

    scanner = BTDiscoveryScanner()
    with patch("renfield_satellite.ble.discovery_scanner.shutil.which",
               return_value="/usr/bin/hcitool"), \
         patch.object(discovery_scanner, "BLEAK_AVAILABLE", False), \
         patch("asyncio.create_subprocess_exec", side_effect=_boom):
        result = await scanner.discover(ble_duration=1.0, classic_timeout=1.0)

    assert result == []  # BLE off + Classic errored -> empty, no exception


@pytest.mark.satellite
@pytest.mark.asyncio
async def test_classic_timeout_yields_empty_no_raise():
    """A Classic inquiry timeout returns empty (the subprocess is killed)."""
    scanner = BTDiscoveryScanner()
    with patch("renfield_satellite.ble.discovery_scanner.shutil.which",
               return_value="/usr/bin/hcitool"), \
         patch.object(discovery_scanner, "BLEAK_AVAILABLE", False), \
         patch("asyncio.create_subprocess_exec",
               side_effect=_hcitool_proc(raise_timeout=True)):
        result = await scanner.discover(ble_duration=1.0, classic_timeout=1.0)

    assert result == []


@pytest.mark.satellite
@pytest.mark.asyncio
async def test_missing_hcitool_is_ble_only():
    """No hcitool installed -> Classic skipped, only BLE devices returned."""
    pairs = [_ble_pair("11:22:33:44:55:66", "Watch", -40)]
    scanner = BTDiscoveryScanner()
    assert scanner.classic_available is False  # shutil.which patched below

    with patch("renfield_satellite.ble.discovery_scanner.shutil.which", return_value=None), \
         patch.object(discovery_scanner, "BLEAK_AVAILABLE", True), \
         patch.object(discovery_scanner, "BleakScanner",
                      SimpleNamespace(discover=_fake_discover(pairs))):
        result = await scanner.discover(ble_duration=1.0, classic_timeout=1.0)

    assert len(result) == 1
    assert result[0]["transport"] == "BLE"
    assert result[0]["mac"] == "11:22:33:44:55:66"


@pytest.mark.satellite
@pytest.mark.asyncio
async def test_ble_error_does_not_raise():
    """A bleak discover() error is swallowed; discover() still returns a list."""
    async def _boom(*args, **kwargs):
        raise RuntimeError("bluez busy")

    scanner = BTDiscoveryScanner()
    with patch("renfield_satellite.ble.discovery_scanner.shutil.which", return_value=None), \
         patch.object(discovery_scanner, "BLEAK_AVAILABLE", True), \
         patch.object(discovery_scanner, "BleakScanner",
                      SimpleNamespace(discover=_boom)):
        result = await scanner.discover(ble_duration=1.0, classic_timeout=1.0)

    assert result == []


@pytest.mark.satellite
@pytest.mark.asyncio
async def test_lowercase_mac_uppercased():
    """BLE device addresses are normalized to upper-case MACs."""
    pairs = [_ble_pair("aa:bb:cc:dd:ee:ff", "x", -50)]
    scanner = BTDiscoveryScanner()
    with patch("renfield_satellite.ble.discovery_scanner.shutil.which", return_value=None), \
         patch.object(discovery_scanner, "BLEAK_AVAILABLE", True), \
         patch.object(discovery_scanner, "BleakScanner",
                      SimpleNamespace(discover=_fake_discover(pairs))):
        result = await scanner.discover(ble_duration=1.0, classic_timeout=1.0)

    assert result[0]["mac"] == "AA:BB:CC:DD:EE:FF"
