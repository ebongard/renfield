"""
Satellite LED night-dimming tests.

Tests for Satellite._on_led_config_update (the backend-pushed brightness
handler) and the WebSocketClient led_config dispatch:
- sets leds.brightness
- clamps >31 to 31 and <0 to 0
- does NOT change the running animation pattern (only scales brightness)
- XVF3800 path calls _run("LED_BRIGHTNESS", ...)
- ws_client dispatches a led_config message / register_ack led_brightness

The Satellite class is heavy to fully construct, so _on_led_config_update is
exercised as an unbound method against a lightweight fake `self` holding only
a fake LED controller (duck-typing matches the real attribute access).
"""

import pytest
from unittest.mock import MagicMock


class FakeAPA102LEDs:
    """Minimal stand-in for LEDController/GPIOLEDController.

    Reads ``self.brightness`` live per animation frame, so changing the
    attribute is the whole runtime mechanism (no _run). Tracks a pattern
    attribute so the test can assert the animation is left untouched.
    """

    def __init__(self, brightness=20):
        self.brightness = brightness
        self.current_pattern = "idle"  # must stay unchanged by a brightness push


class FakeXVF3800LEDs:
    """Minimal stand-in for XVF3800LEDController.

    The XMOS chip renders effects in hardware, so a runtime brightness change
    needs an explicit ``_run("LED_BRIGHTNESS", n)`` call.
    """

    def __init__(self, brightness=20):
        self.brightness = brightness
        self.current_pattern = "speaking"
        self._run = MagicMock()


def _call_update(leds, brightness):
    """Invoke Satellite._on_led_config_update against a fake self holding leds."""
    from renfield_satellite.satellite import Satellite

    fake_self = MagicMock()
    fake_self.leds = leds
    Satellite._on_led_config_update(fake_self, brightness)


class TestOnLedConfigUpdate:

    @pytest.mark.satellite
    def test_sets_brightness(self):
        leds = FakeAPA102LEDs(brightness=20)
        _call_update(leds, 5)
        assert leds.brightness == 5

    @pytest.mark.satellite
    def test_clamps_above_31(self):
        leds = FakeAPA102LEDs(brightness=20)
        _call_update(leds, 99)
        assert leds.brightness == 31

    @pytest.mark.satellite
    def test_clamps_below_0(self):
        leds = FakeAPA102LEDs(brightness=20)
        _call_update(leds, -10)
        assert leds.brightness == 0

    @pytest.mark.satellite
    def test_does_not_change_pattern(self):
        leds = FakeAPA102LEDs(brightness=20)
        before = leds.current_pattern
        _call_update(leds, 3)
        # Only brightness scaled — animation pattern untouched.
        assert leds.current_pattern == before
        assert leds.brightness == 3

    @pytest.mark.satellite
    def test_xvf3800_calls_run_led_brightness(self):
        leds = FakeXVF3800LEDs(brightness=20)
        _call_update(leds, 7)
        assert leds.brightness == 7
        leds._run.assert_called_once_with("LED_BRIGHTNESS", "7")
        # Pattern still untouched.
        assert leds.current_pattern == "speaking"

    @pytest.mark.satellite
    def test_xvf3800_clamps_before_run(self):
        leds = FakeXVF3800LEDs(brightness=20)
        _call_update(leds, 50)
        assert leds.brightness == 31
        leds._run.assert_called_once_with("LED_BRIGHTNESS", "31")


class TestWebSocketClientLedConfigDispatch:

    def _make_client(self):
        from renfield_satellite.network.websocket_client import WebSocketClient

        return WebSocketClient(satellite_id="sat-test", room="Test Room")

    @pytest.mark.satellite
    @pytest.mark.asyncio
    async def test_led_config_message_invokes_callback(self):
        client = self._make_client()
        received = []
        client.on_led_config(lambda b: received.append(b))

        await client._handle_message({"type": "led_config", "brightness": 5})
        assert received == [5]

    @pytest.mark.satellite
    @pytest.mark.asyncio
    async def test_led_config_missing_brightness_is_safe(self):
        client = self._make_client()
        received = []
        client.on_led_config(lambda b: received.append(b))

        # No "brightness" key — must not raise and must not invoke the callback.
        await client._handle_message({"type": "led_config"})
        assert received == []

    @pytest.mark.satellite
    @pytest.mark.asyncio
    async def test_led_config_invalid_brightness_is_safe(self):
        client = self._make_client()
        received = []
        client.on_led_config(lambda b: received.append(b))

        # Non-int garbage — guarded, no raise, no callback.
        await client._handle_message({"type": "led_config", "brightness": "nope"})
        assert received == []

    @pytest.mark.satellite
    @pytest.mark.asyncio
    async def test_led_config_coerces_string_int(self):
        client = self._make_client()
        received = []
        client.on_led_config(lambda b: received.append(b))

        await client._handle_message({"type": "led_config", "brightness": "8"})
        assert received == [8]
