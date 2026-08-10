"""
Display Controller for Whisplay HAT (ST7789P3 240x280 LCD)

Renders satellite state information on the built-in LCD display.
Uses spidev for SPI communication and Pillow for rendering.
"""

import struct
import threading
import time
from typing import Optional

try:
    import spidev
    SPI_AVAILABLE = True
except ImportError:
    spidev = None
    SPI_AVAILABLE = False

try:
    from gpiozero import OutputDevice, PWMLED
    GPIO_AVAILABLE = True
except ImportError:
    OutputDevice = None
    PWMLED = None
    GPIO_AVAILABLE = False


# ── sunxi GPIO backend (Orange Pi A733 etc.) ──────────────────────────────────
# gpiozero is Raspberry-Pi / BCM-numbered and does NOT map to Allwinner pins. On
# a sunxi board the DC/RST/backlight lines are driven via libgpiod by (gpiochip,
# line-offset) — python-periphery gives a clean, version-stable API. These shims
# mimic the gpiozero OutputDevice/PWMLED surface the ST7789 driver uses
# (.on()/.off()/.value/.close()), so the rest of the driver is backend-agnostic.
# NOTE: backlight is on/off here (no dimming) — sunxi PWM dimming needs a hardware
# PWM pin + overlay; wire BL to a plain GPIO for now. Validate pins on-board.
class _SunxiOut:
    """A single output line via python-periphery (libgpiod)."""
    def __init__(self, gpiochip: str, line: int):
        from periphery import GPIO
        self._g = GPIO(gpiochip, int(line), "out")
        self._g.write(False)

    def on(self):
        self._g.write(True)

    def off(self):
        self._g.write(False)

    def close(self):
        try:
            self._g.close()
        except Exception:
            pass


class _SunxiBacklight(_SunxiOut):
    """Backlight line exposing a gpiozero-PWMLED-like `.value` (thresholded on/off)."""
    def __init__(self, gpiochip: str, line: int):
        super().__init__(gpiochip, line)
        self._value = 0.0

    @property
    def value(self) -> float:
        return self._value

    @value.setter
    def value(self, v: float):
        self._value = max(0.0, min(1.0, float(v)))
        self._g.write(self._value > 0.05)   # on/off (no dimming without a PWM pin)

try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_AVAILABLE = True
except ImportError:
    Image = None
    ImageDraw = None
    ImageFont = None
    PIL_AVAILABLE = False


# ST7789 commands
_SWRESET = 0x01
_SLPOUT = 0x11
_COLMOD = 0x3A
_MADCTL = 0x36
_CASET = 0x2A
_RASET = 0x2B
_RAMWR = 0x2C
_INVON = 0x21
_NORON = 0x13
_DISPON = 0x29

# Color constants (RGB tuples for Pillow)
_COLORS = {
    "black": (0, 0, 0),
    "white": (255, 255, 255),
    "green": (0, 200, 0),
    "yellow": (230, 200, 0),
    "cyan": (0, 200, 200),
    "red": (200, 0, 0),
    "blue": (0, 40, 80),
    "dim_blue": (0, 20, 40),
}


class ST7789Display:
    """
    Low-level ST7789P3 LCD driver via SPI.

    Uses gpiozero OutputDevice for DC/RST pins to avoid
    RPi.GPIO conflicts with other gpiozero usage.
    """

    def __init__(
        self,
        width: int = 240,
        height: int = 280,
        spi_bus: int = 0,
        spi_device: int = 0,
        spi_speed_hz: int = 80_000_000,
        dc_pin: int = 27,
        rst_pin: int = 4,
        bl_pin: int = 22,
        gpio_backend: str = "rpi",          # "rpi" (gpiozero/BCM) | "sunxi" (libgpiod)
        gpiochip: str = "/dev/gpiochip0",   # sunxi backend: DC/RST/BL are line offsets on this chip
    ):
        self.width = width
        self.height = height
        self.spi_bus = spi_bus
        self.spi_device = spi_device
        self.spi_speed_hz = spi_speed_hz
        self.dc_pin = dc_pin
        self.rst_pin = rst_pin
        self.bl_pin = bl_pin
        self.gpio_backend = gpio_backend
        self.gpiochip = gpiochip

        self._spi: Optional["spidev.SpiDev"] = None
        self._dc: Optional["OutputDevice"] = None
        self._rst: Optional["OutputDevice"] = None
        self._bl: Optional["PWMLED"] = None

    def open(self) -> bool:
        """Initialize SPI and GPIO, run display init sequence."""
        if not SPI_AVAILABLE:
            print("Display: spidev not available")
            return False

        try:
            if self.gpio_backend == "sunxi":
                # Allwinner: DC/RST/BL are (gpiochip, line) via libgpiod/periphery.
                self._dc = _SunxiOut(self.gpiochip, self.dc_pin)
                self._rst = _SunxiOut(self.gpiochip, self.rst_pin)
                self._bl = _SunxiBacklight(self.gpiochip, self.bl_pin)
            else:
                if not GPIO_AVAILABLE:
                    print("Display: gpiozero not available (rpi backend)")
                    return False
                self._dc = OutputDevice(self.dc_pin)
                self._rst = OutputDevice(self.rst_pin)
                self._bl = PWMLED(self.bl_pin, initial_value=0)

            self._spi = spidev.SpiDev()
            self._spi.open(self.spi_bus, self.spi_device)
            self._spi.max_speed_hz = self.spi_speed_hz
            self._spi.mode = 0

            self._init_display()
            self._bl.value = 0.8  # 80% backlight
            print(f"ST7789 display initialized: {self.width}x{self.height}")
            return True
        except Exception as e:
            print(f"Failed to initialize display: {e}")
            return False

    def close(self):
        """Turn off backlight and release resources."""
        if self._bl:
            try:
                self._bl.value = 0
                self._bl.close()
            except Exception:
                pass
        if self._spi:
            try:
                self._spi.close()
            except Exception:
                pass
        for pin in (self._dc, self._rst):
            if pin:
                try:
                    pin.close()
                except Exception:
                    pass
        self._spi = self._dc = self._rst = self._bl = None

    def _init_display(self):
        """ST7789 initialization sequence (from WhisPlay.py reference)."""
        # Hardware reset
        self._rst.on()
        time.sleep(0.01)
        self._rst.off()
        time.sleep(0.01)
        self._rst.on()
        time.sleep(0.12)

        self._cmd(_SLPOUT)
        time.sleep(0.12)

        # Memory access control (0xC0 = row/col mirrored for Whisplay orientation)
        self._cmd(_MADCTL, bytes([0xC0]))
        # 16-bit color (RGB565)
        self._cmd(_COLMOD, bytes([0x05]))
        # Porch setting
        self._cmd(0xB2, bytes([0x0C, 0x0C, 0x00, 0x33, 0x33]))
        # Gate control
        self._cmd(0xB7, bytes([0x35]))
        # VCOM setting
        self._cmd(0xBB, bytes([0x32]))
        # Power control
        self._cmd(0xC2, bytes([0x01]))
        # VDV/VRH
        self._cmd(0xC3, bytes([0x15]))
        self._cmd(0xC4, bytes([0x20]))
        # Frame rate
        self._cmd(0xC6, bytes([0x0F]))
        # Power control 2
        self._cmd(0xD0, bytes([0xA4, 0xA1]))
        # Positive gamma
        self._cmd(0xE0, bytes([0xD0, 0x08, 0x0E, 0x09, 0x09, 0x05, 0x31, 0x33,
                               0x48, 0x17, 0x14, 0x15, 0x31, 0x34]))
        # Negative gamma
        self._cmd(0xE1, bytes([0xD0, 0x08, 0x0E, 0x09, 0x09, 0x15, 0x31, 0x33,
                               0x48, 0x17, 0x14, 0x15, 0x31, 0x34]))
        # Inversion on (required for ST7789)
        self._cmd(_INVON)
        self._cmd(_DISPON)
        time.sleep(0.01)

    def _cmd(self, command: int, data: Optional[bytes] = None):
        """Send command (and optional data) to display."""
        if not self._spi:
            return
        self._dc.off()  # Command mode
        self._spi.writebytes([command])
        if data:
            self._dc.on()  # Data mode
            self._spi.writebytes(list(data))

    def set_window(self, x0: int, y0: int, x1: int, y1: int):
        """Set the drawing window. Applies Y+20 offset for 240x280 panel."""
        self._cmd(_CASET, struct.pack(">HH", x0, x1))
        self._cmd(_RASET, struct.pack(">HH", y0 + 20, y1 + 20))
        self._cmd(_RAMWR)

    def draw_image(self, image: "Image.Image"):
        """Draw a Pillow RGB image to the full screen."""
        if not self._spi:
            return

        # Convert to RGB565
        rgb565 = self._rgb_to_565(image)

        self.set_window(0, 0, self.width - 1, self.height - 1)
        self._dc.on()  # Data mode

        # Send in chunks (SPI transfer limit)
        chunk_size = 4096
        for i in range(0, len(rgb565), chunk_size):
            self._spi.writebytes(list(rgb565[i:i + chunk_size]))

    def fill_screen(self, r: int, g: int, b: int):
        """Fill entire screen with a solid color."""
        if not PIL_AVAILABLE:
            return
        img = Image.new("RGB", (self.width, self.height), (r, g, b))
        self.draw_image(img)

    @staticmethod
    def _rgb_to_565(image: "Image.Image") -> bytes:
        """Convert Pillow RGB image to RGB565 bytes (big-endian)."""
        pixels = image.tobytes()
        result = bytearray(len(pixels) // 3 * 2)
        idx = 0
        for i in range(0, len(pixels), 3):
            r, g, b = pixels[i], pixels[i + 1], pixels[i + 2]
            rgb565 = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
            result[idx] = (rgb565 >> 8) & 0xFF
            result[idx + 1] = rgb565 & 0xFF
            idx += 2
        return bytes(result)

    def set_backlight(self, value: float):
        """Set backlight brightness (0.0 to 1.0)."""
        if self._bl:
            self._bl.value = max(0.0, min(1.0, value))


class DisplayController:
    """
    High-level display controller that renders satellite state.

    Renders status screens using Pillow and pushes them to the
    ST7789 display. Uses a background thread to avoid blocking.
    """

    def __init__(self, width: int = 240, height: int = 280, room: str = "",
                 hw: Optional[dict] = None):
        self.width = width
        self.height = height
        self.room = room
        # Hardware settings passed to ST7789Display (gpio_backend/spi_bus/spi_device/
        # spi_speed_hz/dc_pin/rst_pin/bl_pin/gpiochip). Empty = Pi/gpiozero defaults.
        self._hw = hw or {}

        self._display: Optional[ST7789Display] = None
        self._lock = threading.Lock()
        self._current_state: Optional[str] = None
        self._font: Optional["ImageFont.FreeTypeFont"] = None
        self._font_large: Optional["ImageFont.FreeTypeFont"] = None

    def open(self) -> bool:
        """Initialize display hardware."""
        if not PIL_AVAILABLE:
            print("Display: Pillow not installed")
            return False

        self._display = ST7789Display(width=self.width, height=self.height, **self._hw)
        if not self._display.open():
            self._display = None
            return False

        # Load fonts (fall back to default if not available)
        try:
            self._font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 20)
            self._font_large = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 28)
        except (OSError, IOError):
            self._font = ImageFont.load_default()
            self._font_large = self._font

        return True

    def close(self):
        """Shut down display."""
        if self._display:
            self._display.fill_screen(0, 0, 0)
            self._display.set_backlight(0)
            self._display.close()
            self._display = None

    def update_state(self, state: str):
        """Update display to reflect new satellite state (non-blocking)."""
        if not self._display or state == self._current_state:
            return
        self._current_state = state
        threading.Thread(target=self._render_state, args=(state,), daemon=True).start()

    def _render_state(self, state: str):
        """Render a state screen and push to display."""
        with self._lock:
            if not self._display:
                return

            screens = {
                "boot": (self._render_boot, 0.8),
                "idle": (self._render_idle, 0.3),
                "listening": (self._render_listening, 0.8),
                "processing": (self._render_processing, 0.8),
                "speaking": (self._render_speaking, 0.8),
                "error": (self._render_error, 0.8),
            }

            renderer, backlight = screens.get(state, (self._render_idle, 0.3))
            image = renderer()
            self._display.set_backlight(backlight)
            self._display.draw_image(image)

    def _new_image(self, bg_color: tuple) -> tuple:
        """Create a new image with draw context."""
        img = Image.new("RGB", (self.width, self.height), bg_color)
        draw = ImageDraw.Draw(img)
        return img, draw

    def _draw_centered_text(self, draw: "ImageDraw.Draw", y: int, text: str, font, fill):
        """Draw text centered horizontally."""
        bbox = draw.textbbox((0, 0), text, font=font)
        text_width = bbox[2] - bbox[0]
        x = (self.width - text_width) // 2
        draw.text((x, y), text, font=font, fill=fill)

    def _render_boot(self) -> "Image.Image":
        img, draw = self._new_image(_COLORS["blue"])
        self._draw_centered_text(draw, 100, "Renfield", self._font_large, _COLORS["white"])
        self._draw_centered_text(draw, 145, self.room, self._font, (180, 180, 180))
        self._draw_centered_text(draw, 220, "Starting...", self._font, (120, 120, 120))
        return img

    def _render_idle(self) -> "Image.Image":
        img, draw = self._new_image(_COLORS["dim_blue"])
        self._draw_centered_text(draw, 120, self.room, self._font_large, (100, 100, 120))
        return img

    def _render_listening(self) -> "Image.Image":
        img, draw = self._new_image(_COLORS["green"])
        self._draw_centered_text(draw, 110, "Listening...", self._font_large, _COLORS["white"])
        self._draw_centered_text(draw, 155, self.room, self._font, (200, 255, 200))
        return img

    def _render_processing(self) -> "Image.Image":
        img, draw = self._new_image(_COLORS["yellow"])
        self._draw_centered_text(draw, 110, "Processing...", self._font_large, _COLORS["black"])
        self._draw_centered_text(draw, 155, self.room, self._font, (80, 80, 0))
        return img

    def _render_speaking(self) -> "Image.Image":
        img, draw = self._new_image(_COLORS["cyan"])
        self._draw_centered_text(draw, 110, "Speaking...", self._font_large, _COLORS["black"])
        self._draw_centered_text(draw, 155, self.room, self._font, (0, 80, 80))
        return img

    def _render_error(self) -> "Image.Image":
        img, draw = self._new_image(_COLORS["red"])
        self._draw_centered_text(draw, 110, "Error", self._font_large, _COLORS["white"])
        self._draw_centered_text(draw, 155, self.room, self._font, (255, 180, 180))
        return img
