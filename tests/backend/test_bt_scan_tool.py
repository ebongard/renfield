"""
Tests for the internal.bluetooth_scan agent tool (_bluetooth_scan handler).

Covers:
- bt_scan_enabled=False => early disabled message (no scan run),
- enabled + 0 satellites responded => success False,
- happy path => success True + aggregated data,
- the tool is registered in TOOLS + _HANDLERS.
"""

from unittest.mock import AsyncMock, patch

import pytest

from ha_glue.services.internal_tools import InternalToolService


@pytest.fixture
def internal_tools():
    return InternalToolService()


def test_tool_registered():
    """internal.bluetooth_scan is exposed in TOOLS and dispatched in _HANDLERS."""
    assert "internal.bluetooth_scan" in InternalToolService.TOOLS
    assert InternalToolService._HANDLERS["internal.bluetooth_scan"] == "_bluetooth_scan"
    desc = InternalToolService.TOOLS["internal.bluetooth_scan"]["description"]
    # The description must warn it's slow and not to retry.
    assert "15-30" in desc
    assert "retry" in desc.lower()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_disabled_returns_early(internal_tools):
    """Flag off => disabled message, BtScanService never constructed."""
    with patch("ha_glue.utils.config.ha_glue_settings") as mock_settings, \
         patch("ha_glue.services.bt_scan_service.BtScanService") as mock_svc:
        mock_settings.bt_scan_enabled = False
        result = await internal_tools._bluetooth_scan({})

    assert result["success"] is False
    assert "disabled" in result["message"].lower()
    mock_svc.assert_not_called()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_satellites_responded_is_failure(internal_tools):
    """Enabled but 0 satellites responded => success False (with data echoed)."""
    scan_data = {
        "total_devices": 0,
        "satellites_queried": 2,
        "satellites_responded": 0,
        "devices": [],
    }
    with patch("ha_glue.utils.config.ha_glue_settings") as mock_settings, \
         patch("ha_glue.services.satellite_manager.get_satellite_manager"), \
         patch("ha_glue.services.bt_scan_service.BtScanService") as mock_svc:
        mock_settings.bt_scan_enabled = True
        mock_svc.return_value.scan_all_satellites = AsyncMock(return_value=scan_data)
        result = await internal_tools._bluetooth_scan({})

    assert result["success"] is False
    assert result["data"]["satellites_responded"] == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_happy_path_returns_devices(internal_tools):
    """Enabled + responding satellites => success True + aggregated data."""
    scan_data = {
        "total_devices": 1,
        "satellites_queried": 2,
        "satellites_responded": 2,
        "devices": [{
            "mac": "A4:C3:F0:11:22:33",
            "name": "iPhone",
            "rssi_best": -40,
            "transport": "BLE",
            "vendor": "Apple",
            "rooms": [{"satellite_id": "sat-a", "room": "Kitchen", "rssi": -40}],
        }],
    }
    with patch("ha_glue.utils.config.ha_glue_settings") as mock_settings, \
         patch("ha_glue.services.satellite_manager.get_satellite_manager") as mock_mgr, \
         patch("ha_glue.services.bt_scan_service.BtScanService") as mock_svc:
        mock_settings.bt_scan_enabled = True
        mock_svc.return_value.scan_all_satellites = AsyncMock(return_value=scan_data)
        result = await internal_tools._bluetooth_scan(
            {"ble_duration": 8, "classic_timeout": 10}
        )

    assert result["success"] is True
    assert result["action_taken"] is True
    assert result["data"]["total_devices"] == 1
    assert result["data"]["devices"][0]["vendor"] == "Apple"
    # The satellite manager was resolved and the service invoked.
    mock_mgr.assert_called_once()
    mock_svc.return_value.scan_all_satellites.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_satellites_connected_is_failure(internal_tools):
    """0 satellites queried (none connected) => success False."""
    scan_data = {
        "total_devices": 0,
        "satellites_queried": 0,
        "satellites_responded": 0,
        "devices": [],
    }
    with patch("ha_glue.utils.config.ha_glue_settings") as mock_settings, \
         patch("ha_glue.services.satellite_manager.get_satellite_manager"), \
         patch("ha_glue.services.bt_scan_service.BtScanService") as mock_svc:
        mock_settings.bt_scan_enabled = True
        mock_svc.return_value.scan_all_satellites = AsyncMock(return_value=scan_data)
        result = await internal_tools._bluetooth_scan({})

    assert result["success"] is False
    assert "no satellites" in result["message"].lower()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_durations_clamped_to_20(internal_tools):
    """ble_duration / classic_timeout are clamped to a 20s ceiling."""
    captured = {}

    async def _capture(satellite_manager, **kwargs):
        captured.update(kwargs)
        return {
            "total_devices": 0,
            "satellites_queried": 1,
            "satellites_responded": 1,
            "devices": [],
        }

    with patch("ha_glue.utils.config.ha_glue_settings") as mock_settings, \
         patch("ha_glue.services.satellite_manager.get_satellite_manager"), \
         patch("ha_glue.services.bt_scan_service.BtScanService") as mock_svc:
        mock_settings.bt_scan_enabled = True
        mock_svc.return_value.scan_all_satellites = _capture
        await internal_tools._bluetooth_scan(
            {"ble_duration": 999, "classic_timeout": 999}
        )

    assert captured["ble_duration"] == 20.0
    assert captured["classic_timeout"] == 20.0
