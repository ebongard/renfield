"""Tests for the config-driven SPI-display backend (rpi gpiozero vs sunxi libgpiod).

The A733 (Orange Pi) drives the SPI TFT's DC/RST/BL via libgpiod (gpio_backend=sunxi),
not gpiozero/BCM. These pin the config plumbing + back-compat so a Pi satellite stays
byte-identical and the Orange Pi gets the right pins. No hardware: open() is not called
(that's where periphery/spidev would touch /dev).
"""

import pytest

from renfield_satellite.config import Config, load_config
from renfield_satellite.hardware.display import ST7789Display, DisplayController


class TestDisplayConfigDefaults:
    @pytest.mark.satellite
    def test_defaults_are_rpi_backcompat(self):
        cfg = Config()
        assert cfg.display.gpio_backend == "rpi"        # Pi satellites unchanged
        assert cfg.display.spi_bus == 0 and cfg.display.spi_device == 0
        assert (cfg.display.dc_pin, cfg.display.rst_pin, cfg.display.bl_pin) == (27, 4, 22)


class TestDisplayConfigParse:
    @pytest.mark.satellite
    def test_parses_sunxi_display_block(self, tmp_path):
        p = tmp_path / "satellite.yaml"
        p.write_text(
            "satellite:\n  id: sat-x\n  room: X\n"
            "display:\n"
            "  enabled: true\n  gpio_backend: sunxi\n  spi_bus: 1\n  spi_device: 0\n"
            "  dc_pin: 130\n  rst_pin: 131\n  bl_pin: 132\n  gpiochip: /dev/gpiochip0\n"
        )
        cfg = load_config(str(p))
        assert cfg.display.enabled is True
        assert cfg.display.gpio_backend == "sunxi"
        assert cfg.display.spi_bus == 1
        assert (cfg.display.dc_pin, cfg.display.rst_pin, cfg.display.bl_pin) == (130, 131, 132)
        assert cfg.display.gpiochip == "/dev/gpiochip0"


class TestST7789Backend:
    @pytest.mark.satellite
    def test_construction_carries_sunxi_settings(self):
        d = ST7789Display(width=240, height=280, gpio_backend="sunxi",
                          gpiochip="/dev/gpiochip0", dc_pin=130, rst_pin=131, bl_pin=132,
                          spi_bus=1, spi_device=0)
        assert d.gpio_backend == "sunxi"
        assert d.gpiochip == "/dev/gpiochip0"
        assert (d.dc_pin, d.rst_pin, d.bl_pin) == (130, 131, 132)
        assert d.spi_bus == 1

    @pytest.mark.satellite
    def test_controller_passes_hw_through(self):
        # DisplayController must forward hw settings into ST7789Display at open() time.
        ctrl = DisplayController(width=240, height=280,
                                 hw={"gpio_backend": "sunxi", "gpiochip": "/dev/gpiochip1",
                                     "dc_pin": 5})
        assert ctrl._hw["gpio_backend"] == "sunxi"
        assert ctrl._hw["gpiochip"] == "/dev/gpiochip1"
