"""Unit tests for ha_glue.services.led_dimming_service.LedDimmingService.

Covers:
- initialize() seeds night brightness when is_night() True, day when False
- _on_daypart_changed("night") pushes led_config to all connected satellites
- push skips/handles a satellite whose send_json raises (no propagation)
- get_current_led_brightness reflects state
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ha_glue.services.led_dimming_service import LedDimmingService
from utils.config import settings


@pytest.fixture
def patched_brightness(monkeypatch):
    """Pin the day/night brightness levels to known values."""
    monkeypatch.setattr(settings, "led_day_brightness", 20, raising=False)
    monkeypatch.setattr(settings, "led_night_brightness", 5, raising=False)
    return settings


def _fake_satellite(satellite_id: str):
    """Build a satellite stub with an AsyncMock send_json websocket."""
    ws = SimpleNamespace(send_json=AsyncMock())
    return SimpleNamespace(satellite_id=satellite_id, websocket=ws)


def _patch_manager(monkeypatch, satellites: dict):
    """Patch get_satellite_manager() to return a manager with these satellites."""
    manager = SimpleNamespace(satellites=satellites)
    monkeypatch.setattr(
        "ha_glue.services.satellite_manager.get_satellite_manager",
        lambda: manager,
    )
    return manager


@pytest.mark.unit
@pytest.mark.asyncio
async def test_initialize_night_sets_night_brightness(patched_brightness, monkeypatch):
    monkeypatch.setattr(
        "services.daypart_service.is_night", lambda *a, **k: True
    )
    svc = LedDimmingService()
    # Avoid registering a real hook in the global registry.
    monkeypatch.setattr(
        "utils.hooks.is_hook_registered", lambda *a, **k: True
    )
    await svc.initialize()
    assert svc.get_current_led_brightness() == 5


@pytest.mark.unit
@pytest.mark.asyncio
async def test_initialize_day_sets_day_brightness(patched_brightness, monkeypatch):
    monkeypatch.setattr(
        "services.daypart_service.is_night", lambda *a, **k: False
    )
    svc = LedDimmingService()
    monkeypatch.setattr(
        "utils.hooks.is_hook_registered", lambda *a, **k: True
    )
    await svc.initialize()
    assert svc.get_current_led_brightness() == 20


@pytest.mark.unit
@pytest.mark.asyncio
async def test_on_daypart_changed_night_pushes_to_all_satellites(
    patched_brightness, monkeypatch
):
    sat_a = _fake_satellite("sat-a")
    sat_b = _fake_satellite("sat-b")
    _patch_manager(monkeypatch, {"sat-a": sat_a, "sat-b": sat_b})

    svc = LedDimmingService()
    await svc._on_daypart_changed(previous="day", current="night", local_time="22:00")

    assert svc.get_current_led_brightness() == 5
    expected = {"type": "led_config", "brightness": 5}
    sat_a.websocket.send_json.assert_awaited_once_with(expected)
    sat_b.websocket.send_json.assert_awaited_once_with(expected)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_on_daypart_changed_day_pushes_day_brightness(
    patched_brightness, monkeypatch
):
    sat_a = _fake_satellite("sat-a")
    _patch_manager(monkeypatch, {"sat-a": sat_a})

    svc = LedDimmingService()
    await svc._on_daypart_changed(previous="night", current="day", local_time="07:00")

    assert svc.get_current_led_brightness() == 20
    sat_a.websocket.send_json.assert_awaited_once_with(
        {"type": "led_config", "brightness": 20}
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_push_handles_send_failure_without_propagation(
    patched_brightness, monkeypatch
):
    failing = _fake_satellite("sat-bad")
    failing.websocket.send_json.side_effect = RuntimeError("dead link")
    good = _fake_satellite("sat-good")
    _patch_manager(monkeypatch, {"sat-bad": failing, "sat-good": good})

    svc = LedDimmingService()
    # Must NOT raise even though one satellite's send fails.
    await svc.push_brightness_to_all_satellites(5)

    # The healthy satellite still received its push.
    good.websocket.send_json.assert_awaited_once_with(
        {"type": "led_config", "brightness": 5}
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_push_with_no_satellites_is_noop(patched_brightness, monkeypatch):
    _patch_manager(monkeypatch, {})
    svc = LedDimmingService()
    # No satellites → must not raise.
    await svc.push_brightness_to_all_satellites(5)


@pytest.mark.unit
def test_get_current_led_brightness_defaults_to_day(patched_brightness):
    svc = LedDimmingService()
    assert svc.get_current_led_brightness() == 20
