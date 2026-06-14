"""
Tests for BtScanService — fan-out + aggregate a Bluetooth discovery scan.

Covers:
- MAC dedup keeping the strongest RSSI across satellites,
- the same MAC seen by two satellites collapsing to one device with two rooms,
- OUI vendor lookup from the MAC prefix,
- a raising / timed-out satellite being skipped (satellites_responded reflects it),
- sorting by best RSSI (strongest first).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ha_glue.services.bt_scan_service import BtScanService, _oui_lookup


def _sat(sat_id, room):
    return SimpleNamespace(satellite_id=sat_id, room=room)


def _make_manager(sats, scan_results):
    """Fake satellite_manager: .satellites dict + request_bt_scan returning a
    preset per-satellite-id result (a list, None, or an Exception to raise)."""
    mgr = SimpleNamespace()
    mgr.satellites = {s.satellite_id: s for s in sats}

    async def _request_bt_scan(satellite_id, params, timeout=30.0):
        result = scan_results.get(satellite_id)
        if isinstance(result, Exception):
            raise result
        return result

    mgr.request_bt_scan = AsyncMock(side_effect=_request_bt_scan)
    return mgr


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dedup_keeps_strongest_rssi_and_merges_rooms():
    """Same MAC from two satellites -> one device, two rooms, best RSSI kept."""
    sats = [_sat("sat-a", "Kitchen"), _sat("sat-b", "Office")]
    scan_results = {
        "sat-a": [{"mac": "11:22:33:44:55:66", "name": "Phone", "rssi": -70, "transport": "BLE"}],
        "sat-b": [{"mac": "11:22:33:44:55:66", "name": "Phone", "rssi": -40, "transport": "BLE"}],
    }
    mgr = _make_manager(sats, scan_results)

    out = await BtScanService().scan_all_satellites(mgr)

    assert out["total_devices"] == 1
    assert out["satellites_queried"] == 2
    assert out["satellites_responded"] == 2
    dev = out["devices"][0]
    assert dev["mac"] == "11:22:33:44:55:66"
    assert dev["rssi_best"] == -40  # strongest wins
    rooms = {r["room"] for r in dev["rooms"]}
    assert rooms == {"Kitchen", "Office"}
    assert len(dev["rooms"]) == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_oui_vendor_lookup():
    """An Apple OUI prefix resolves to the Apple vendor label."""
    sats = [_sat("sat-a", "Kitchen")]
    scan_results = {
        "sat-a": [{"mac": "A4:C3:F0:11:22:33", "name": "iPhone", "rssi": -55, "transport": "BLE"}],
    }
    mgr = _make_manager(sats, scan_results)

    out = await BtScanService().scan_all_satellites(mgr)

    assert out["devices"][0]["vendor"] == "Apple"
    # Sanity-check the helper directly too.
    assert _oui_lookup("A4:C3:F0:00:00:00") == "Apple"
    assert _oui_lookup("DE:AD:BE:EF:00:00") == "Unknown"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_raising_and_timed_out_satellites_are_skipped():
    """A satellite that raises (gather exception) or returns None (timeout) is
    not counted as responded, but the rest still aggregate."""
    sats = [
        _sat("sat-good", "Kitchen"),
        _sat("sat-raises", "Office"),
        _sat("sat-timeout", "Bedroom"),
    ]
    scan_results = {
        "sat-good": [{"mac": "AA:BB:CC:DD:EE:FF", "name": "Speaker", "rssi": -60, "transport": "Classic"}],
        "sat-raises": RuntimeError("ws send failed"),
        "sat-timeout": None,  # request_bt_scan returns None on timeout
    }
    mgr = _make_manager(sats, scan_results)

    out = await BtScanService().scan_all_satellites(mgr)

    assert out["satellites_queried"] == 3
    assert out["satellites_responded"] == 1  # only sat-good
    assert out["total_devices"] == 1
    assert out["devices"][0]["mac"] == "AA:BB:CC:DD:EE:FF"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_sorted_by_rssi_desc_none_last():
    """Devices sort strongest-RSSI first; a Classic (None RSSI) device sorts last."""
    sats = [_sat("sat-a", "Kitchen")]
    scan_results = {
        "sat-a": [
            {"mac": "00:00:00:00:00:01", "name": "weak", "rssi": -90, "transport": "BLE"},
            {"mac": "00:00:00:00:00:02", "name": "strong", "rssi": -30, "transport": "BLE"},
            {"mac": "00:00:00:00:00:03", "name": "classic", "rssi": None, "transport": "Classic"},
        ],
    }
    mgr = _make_manager(sats, scan_results)

    out = await BtScanService().scan_all_satellites(mgr)

    macs = [d["mac"] for d in out["devices"]]
    assert macs[0] == "00:00:00:00:00:02"  # strongest first
    assert macs[1] == "00:00:00:00:00:01"
    assert macs[2] == "00:00:00:00:00:03"  # None RSSI last


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_satellites_returns_empty():
    """Zero satellites -> empty aggregate, queried/responded 0."""
    mgr = _make_manager([], {})

    out = await BtScanService().scan_all_satellites(mgr)

    assert out == {
        "total_devices": 0,
        "satellites_queried": 0,
        "satellites_responded": 0,
        "devices": [],
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_name_filled_from_other_satellite():
    """A device seen nameless by one satellite and named by another keeps the name."""
    sats = [_sat("sat-a", "Kitchen"), _sat("sat-b", "Office")]
    scan_results = {
        "sat-a": [{"mac": "12:34:56:78:9A:BC", "name": None, "rssi": -80, "transport": "BLE"}],
        "sat-b": [{"mac": "12:34:56:78:9A:BC", "name": "Headphones", "rssi": -85, "transport": "BLE"}],
    }
    mgr = _make_manager(sats, scan_results)

    out = await BtScanService().scan_all_satellites(mgr)

    assert out["total_devices"] == 1
    assert out["devices"][0]["name"] == "Headphones"
    assert out["devices"][0]["rssi_best"] == -80
