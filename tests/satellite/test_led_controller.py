"""
LED Controller Tests

Tests for renfield_satellite.hardware.led.LEDController:
- GPIO power pin initialization and cleanup
- SPI end frame format
- Constructor parameters
- Dual-HAT compatibility (2-mic vs 4-mic)
"""

import pytest
from unittest.mock import MagicMock, patch, PropertyMock


class TestLEDControllerInit:
    """Tests for LEDController constructor and parameters."""

    @pytest.mark.satellite
    def test_default_parameters(self):
        """LEDController defaults: 3 LEDs, SPI 0:0, no power pin."""
        from renfield_satellite.hardware.led import LEDController

        ctrl = LEDController()
        assert ctrl.num_leds == 3
        assert ctrl.spi_bus == 0
        assert ctrl.spi_device == 0
        assert ctrl.brightness == 20
        assert ctrl.led_power_pin is None
        assert ctrl._power is None

    @pytest.mark.satellite
    def test_4mic_hat_parameters(self):
        """LEDController accepts 4-mic HAT config: 12 LEDs, SPI 0:1, GPIO5."""
        from renfield_satellite.hardware.led import LEDController

        ctrl = LEDController(
            num_leds=12,
            spi_bus=0,
            spi_device=1,
            brightness=25,
            led_power_pin=5,
        )
        assert ctrl.num_leds == 12
        assert ctrl.spi_device == 1
        assert ctrl.led_power_pin == 5

    @pytest.mark.satellite
    def test_brightness_clamped_to_31(self):
        """Brightness values above 31 are clamped."""
        from renfield_satellite.hardware.led import LEDController

        ctrl = LEDController(brightness=50)
        assert ctrl.brightness == 31


class TestLEDPowerPin:
    """Tests for GPIO power pin enable/disable logic."""

    @pytest.mark.satellite
    @patch("renfield_satellite.hardware.led.SPI_AVAILABLE", True)
    @patch("renfield_satellite.hardware.led.GPIO_AVAILABLE", True)
    @patch("renfield_satellite.hardware.led.spidev")
    @patch("renfield_satellite.hardware.led.GpioLED")
    def test_open_enables_gpio_power(self, mock_gpio_led_cls, mock_spidev):
        """open() enables GPIO power pin before SPI init when led_power_pin is set."""
        from renfield_satellite.hardware.led import LEDController

        mock_power = MagicMock()
        mock_gpio_led_cls.return_value = mock_power

        mock_spi = MagicMock()
        mock_spidev.SpiDev.return_value = mock_spi

        ctrl = LEDController(num_leds=12, spi_device=1, led_power_pin=5)
        result = ctrl.open()

        assert result is True
        mock_gpio_led_cls.assert_called_once_with(5)
        mock_power.on.assert_called_once()
        assert ctrl._power is mock_power

    @pytest.mark.satellite
    @patch("renfield_satellite.hardware.led.SPI_AVAILABLE", True)
    @patch("renfield_satellite.hardware.led.GPIO_AVAILABLE", True)
    @patch("renfield_satellite.hardware.led.spidev")
    @patch("renfield_satellite.hardware.led.GpioLED")
    def test_no_gpio_when_power_pin_is_none(self, mock_gpio_led_cls, mock_spidev):
        """open() skips GPIO when led_power_pin is None (2-mic HAT)."""
        from renfield_satellite.hardware.led import LEDController

        mock_spi = MagicMock()
        mock_spidev.SpiDev.return_value = mock_spi

        ctrl = LEDController(num_leds=3)
        result = ctrl.open()

        assert result is True
        mock_gpio_led_cls.assert_not_called()
        assert ctrl._power is None

    @pytest.mark.satellite
    @patch("renfield_satellite.hardware.led.SPI_AVAILABLE", True)
    @patch("renfield_satellite.hardware.led.GPIO_AVAILABLE", False)
    @patch("renfield_satellite.hardware.led.spidev")
    def test_warning_when_gpiozero_not_installed(self, mock_spidev, capsys):
        """open() prints warning when gpiozero is missing but power pin is set."""
        from renfield_satellite.hardware.led import LEDController

        mock_spi = MagicMock()
        mock_spidev.SpiDev.return_value = mock_spi

        ctrl = LEDController(num_leds=12, led_power_pin=5)
        result = ctrl.open()

        assert result is True
        captured = capsys.readouterr()
        assert "gpiozero not installed" in captured.out
        assert "GPIO5" in captured.out

    @pytest.mark.satellite
    @patch("renfield_satellite.hardware.led.SPI_AVAILABLE", True)
    @patch("renfield_satellite.hardware.led.GPIO_AVAILABLE", True)
    @patch("renfield_satellite.hardware.led.spidev")
    @patch("renfield_satellite.hardware.led.GpioLED")
    def test_close_disables_gpio_power(self, mock_gpio_led_cls, mock_spidev):
        """close() turns off and closes GPIO power pin."""
        from renfield_satellite.hardware.led import LEDController

        mock_power = MagicMock()
        mock_gpio_led_cls.return_value = mock_power

        mock_spi = MagicMock()
        mock_spidev.SpiDev.return_value = mock_spi

        ctrl = LEDController(num_leds=12, led_power_pin=5)
        ctrl.open()
        ctrl.close()

        mock_power.off.assert_called_once()
        mock_power.close.assert_called_once()
        assert ctrl._power is None

    @pytest.mark.satellite
    @patch("renfield_satellite.hardware.led.SPI_AVAILABLE", True)
    @patch("renfield_satellite.hardware.led.spidev")
    def test_close_without_power_pin(self, mock_spidev):
        """close() works cleanly when no power pin is configured."""
        from renfield_satellite.hardware.led import LEDController

        mock_spi = MagicMock()
        mock_spidev.SpiDev.return_value = mock_spi

        ctrl = LEDController(num_leds=3)
        ctrl.open()
        ctrl.close()  # Should not raise


class TestEndFrame:
    """Tests for APA102 end frame format."""

    @pytest.mark.satellite
    @patch("renfield_satellite.hardware.led.SPI_AVAILABLE", True)
    @patch("renfield_satellite.hardware.led.spidev")
    def test_end_frame_uses_zero_bytes(self, mock_spidev):
        """_write() uses 0x00 end frame bytes (matching 4-mic HAT reference driver)."""
        from renfield_satellite.hardware.led import LEDController, Color

        mock_spi = MagicMock()
        mock_spidev.SpiDev.return_value = mock_spi

        ctrl = LEDController(num_leds=3)
        ctrl.open()
        ctrl.set_all(Color(255, 0, 0))

        # Get the data written to SPI
        written_data = mock_spi.writebytes.call_args[0][0]

        # End frame should be 0x00 bytes, not 0xFF
        # Start frame: 4 bytes of 0x00
        # LED data: 3 * 4 = 12 bytes
        # End frame: at least 4 bytes of 0x00
        end_frame_start = 4 + (3 * 4)  # After start frame + LED data
        end_frame = written_data[end_frame_start:]
        assert all(b == 0x00 for b in end_frame)
        assert len(end_frame) >= 4
