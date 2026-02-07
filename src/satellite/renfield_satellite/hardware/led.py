"""
LED Controller for ReSpeaker 2-Mics Pi HAT

Controls the 3x APA102 RGB LEDs via SPI.
Provides visual feedback for satellite states.
"""

import asyncio
import colorsys
import math
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Tuple

try:
    import spidev
    SPI_AVAILABLE = True
except ImportError:
    spidev = None
    SPI_AVAILABLE = False
    print("Warning: spidev not installed. LED control disabled.")


class LEDPattern(str, Enum):
    """LED animation patterns for different states"""
    OFF = "off"
    IDLE = "idle"           # Dim blue pulse
    LISTENING = "listening"  # Solid green
    PROCESSING = "processing"  # Yellow chase
    SPEAKING = "speaking"    # Cyan breathe
    ERROR = "error"         # Red blink
    SUCCESS = "success"     # Green flash
    BOOT = "boot"           # Rainbow cycle


@dataclass
class Color:
    """RGB color with brightness"""
    r: int
    g: int
    b: int
    brightness: int = 31  # 0-31 for APA102

    def to_apa102(self) -> bytes:
        """Convert to APA102 format (brightness | B | G | R)"""
        # APA102 format: 0xE0 + brightness (5 bits), then BGR
        bright = 0xE0 | (self.brightness & 0x1F)
        return bytes([bright, self.b, self.g, self.r])


# Predefined colors
COLORS = {
    "off": Color(0, 0, 0, 0),
    "blue": Color(0, 0, 255),
    "green": Color(0, 255, 0),
    "red": Color(255, 0, 0),
    "yellow": Color(255, 255, 0),
    "cyan": Color(0, 255, 255),
    "white": Color(255, 255, 255),
    "purple": Color(128, 0, 255),
}


class LEDController:
    """
    Controls APA102 LEDs on ReSpeaker 2-Mics Pi HAT.

    The HAT has 3 LEDs arranged in a row.
    Uses SPI for communication (bus 0, device 0).
    """

    def __init__(
        self,
        num_leds: int = 3,
        spi_bus: int = 0,
        spi_device: int = 0,
        brightness: int = 20,
    ):
        """
        Initialize LED controller.

        Args:
            num_leds: Number of LEDs (default 3 for ReSpeaker)
            spi_bus: SPI bus number
            spi_device: SPI device number
            brightness: Default brightness 0-31
        """
        self.num_leds = num_leds
        self.spi_bus = spi_bus
        self.spi_device = spi_device
        self.brightness = min(31, max(0, brightness))

        self._spi: Optional["spidev.SpiDev"] = None
        self._pattern: LEDPattern = LEDPattern.OFF
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

        # Current LED states
        self._colors: List[Color] = [
            Color(0, 0, 0, 0) for _ in range(num_leds)
        ]
        # Cache of last written colors to skip redundant SPI writes
        self._last_written: Optional[List[Tuple[int, int, int, int]]] = None

    def open(self) -> bool:
        """
        Open SPI connection.

        Returns:
            True if opened successfully
        """
        if not SPI_AVAILABLE:
            print("SPI not available")
            return False

        try:
            self._spi = spidev.SpiDev()
            self._spi.open(self.spi_bus, self.spi_device)
            self._spi.max_speed_hz = 8000000  # 8 MHz
            self._spi.mode = 0
            print(f"SPI opened: bus {self.spi_bus}, device {self.spi_device}")
            return True
        except Exception as e:
            print(f"Failed to open SPI: {e}")
            return False

    def close(self):
        """Close SPI connection"""
        self.stop_animation()
        self.set_all(Color(0, 0, 0, 0))
        self._write()

        if self._spi:
            try:
                self._spi.close()
            except OSError:
                pass
            self._spi = None

    def _write(self):
        """Write current colors to LEDs, skipping if unchanged"""
        if not self._spi:
            return

        # Skip redundant SPI writes when colors haven't changed
        current = [(c.r, c.g, c.b, c.brightness) for c in self._colors]
        if current == self._last_written:
            return
        self._last_written = current

        try:
            # APA102 protocol: start frame + LED data + end frame
            # Start frame: 4 bytes of 0x00
            data = [0x00, 0x00, 0x00, 0x00]

            # LED data
            for color in self._colors:
                data.extend(color.to_apa102())

            # End frame: ceil(n/2) bytes of 0xFF
            end_bytes = (self.num_leds + 15) // 16
            data.extend([0xFF] * max(4, end_bytes))

            self._spi.writebytes(data)

        except Exception as e:
            print(f"LED write error: {e}")

    def set_led(self, index: int, color: Color):
        """Set color of a single LED"""
        if 0 <= index < self.num_leds:
            with self._lock:
                self._colors[index] = Color(
                    color.r, color.g, color.b,
                    min(self.brightness, color.brightness)
                )
                self._write()

    def set_all(self, color: Color):
        """Set all LEDs to the same color"""
        with self._lock:
            adjusted = Color(
                color.r, color.g, color.b,
                min(self.brightness, color.brightness)
            )
            self._colors = [adjusted for _ in range(self.num_leds)]
            self._write()

    def set_colors(self, colors: List[Color]):
        """Set individual colors for each LED"""
        with self._lock:
            for i, color in enumerate(colors[:self.num_leds]):
                self._colors[i] = Color(
                    color.r, color.g, color.b,
                    min(self.brightness, color.brightness)
                )
            self._write()

    def set_pattern(self, pattern: LEDPattern):
        """
        Start an LED animation pattern.

        Args:
            pattern: Animation pattern to display
        """
        if pattern == self._pattern:
            return

        # Stop current animation
        self.stop_animation()

        self._pattern = pattern

        # Start new animation
        if pattern != LEDPattern.OFF:
            self._running = True
            self._thread = threading.Thread(
                target=self._animation_loop,
                daemon=True
            )
            self._thread.start()

    def stop_animation(self):
        """Stop current animation"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=0.5)
            self._thread = None

    def _animation_loop(self):
        """Background thread running LED animations"""
        frame = 0

        while self._running:
            pattern = self._pattern

            if pattern == LEDPattern.IDLE:
                # Dim blue pulse
                self._animate_pulse(frame, COLORS["blue"], 0.05)

            elif pattern == LEDPattern.LISTENING:
                # Solid green
                self.set_all(COLORS["green"])
                time.sleep(0.1)

            elif pattern == LEDPattern.PROCESSING:
                # Yellow chase
                self._animate_chase(frame, COLORS["yellow"])

            elif pattern == LEDPattern.SPEAKING:
                # Cyan breathe
                self._animate_breathe(frame, COLORS["cyan"])

            elif pattern == LEDPattern.ERROR:
                # Red blink
                self._animate_blink(frame, COLORS["red"])

            elif pattern == LEDPattern.SUCCESS:
                # Green flash
                self._animate_flash(frame, COLORS["green"])

            elif pattern == LEDPattern.BOOT:
                # Rainbow cycle
                self._animate_rainbow(frame)

            else:
                self.set_all(COLORS["off"])
                time.sleep(0.1)

            frame += 1
            time.sleep(0.05)  # 20 FPS

    def _animate_pulse(self, frame: int, color: Color, min_brightness: float):
        """Pulsing brightness animation"""
        # Sine wave for smooth pulse
        phase = (frame % 60) / 60.0 * 2 * math.pi
        brightness_factor = (math.sin(phase) + 1) / 2  # 0 to 1
        brightness_factor = min_brightness + brightness_factor * (1 - min_brightness)

        adjusted = Color(
            int(color.r * brightness_factor),
            int(color.g * brightness_factor),
            int(color.b * brightness_factor),
            self.brightness
        )
        self.set_all(adjusted)

    def _animate_chase(self, frame: int, color: Color):
        """Chase animation - light moves across LEDs"""
        active_idx = frame % self.num_leds
        colors = []
        for i in range(self.num_leds):
            if i == active_idx:
                colors.append(color)
            else:
                colors.append(Color(
                    color.r // 4, color.g // 4, color.b // 4, self.brightness
                ))
        self.set_colors(colors)

    def _animate_breathe(self, frame: int, color: Color):
        """Slow breathing animation"""
        phase = (frame % 80) / 80.0 * 2 * math.pi
        brightness_factor = (math.sin(phase) + 1) / 2
        brightness_factor = 0.2 + brightness_factor * 0.8

        adjusted = Color(
            int(color.r * brightness_factor),
            int(color.g * brightness_factor),
            int(color.b * brightness_factor),
            self.brightness
        )
        self.set_all(adjusted)

    def _animate_blink(self, frame: int, color: Color):
        """Fast blink animation"""
        if (frame // 10) % 2 == 0:
            self.set_all(color)
        else:
            self.set_all(COLORS["off"])

    def _animate_flash(self, frame: int, color: Color):
        """Single flash then off"""
        if frame < 10:
            self.set_all(color)
        else:
            self.set_all(COLORS["off"])
            self._running = False

    def _animate_rainbow(self, frame: int):
        """Rainbow color cycle"""
        colors = []
        for i in range(self.num_leds):
            hue = ((frame * 5) + i * 120) % 360
            r, g, b = self._hsv_to_rgb(hue, 1.0, 1.0)
            colors.append(Color(r, g, b, self.brightness))
        self.set_colors(colors)

    @staticmethod
    def _hsv_to_rgb(h: float, s: float, v: float) -> Tuple[int, int, int]:
        """Convert HSV to RGB"""
        r, g, b = colorsys.hsv_to_rgb(h / 360.0, s, v)
        return int(r * 255), int(g * 255), int(b * 255)

    @property
    def current_pattern(self) -> LEDPattern:
        """Get current pattern"""
        return self._pattern


class LEDControllerAsync:
    """Async wrapper for LED controller"""

    def __init__(self, controller: LEDController):
        self.controller = controller

    async def set_pattern(self, pattern: LEDPattern):
        """Set LED pattern asynchronously"""
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self.controller.set_pattern, pattern)

    def set_pattern_sync(self, pattern: LEDPattern):
        """Set pattern synchronously (for use in callbacks)"""
        self.controller.set_pattern(pattern)
