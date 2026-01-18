"""
Button Handler for ReSpeaker 2-Mics Pi HAT

Handles the user button on GPIO17.
"""

import asyncio
import threading
import time
from typing import Callable, Optional

try:
    import RPi.GPIO as GPIO
    GPIO_AVAILABLE = True
except ImportError:
    GPIO = None
    GPIO_AVAILABLE = False
    print("Warning: RPi.GPIO not installed. Button control disabled.")


class ButtonHandler:
    """
    Handles button input from GPIO.

    The ReSpeaker 2-Mics Pi HAT has a user button on GPIO17.
    Supports:
    - Press (single click)
    - Long press (hold > 1 second)
    - Double press (two clicks within 0.5s)
    """

    def __init__(
        self,
        gpio_pin: int = 17,
        debounce_ms: int = 50,
        long_press_ms: int = 1000,
        double_press_ms: int = 500,
    ):
        """
        Initialize button handler.

        Args:
            gpio_pin: GPIO pin number (BCM mode)
            debounce_ms: Debounce time in milliseconds
            long_press_ms: Time to trigger long press
            double_press_ms: Max time between double press clicks
        """
        self.gpio_pin = gpio_pin
        self.debounce_ms = debounce_ms
        self.long_press_ms = long_press_ms
        self.double_press_ms = double_press_ms

        self._on_press: Optional[Callable[[], None]] = None
        self._on_long_press: Optional[Callable[[], None]] = None
        self._on_double_press: Optional[Callable[[], None]] = None
        self._on_release: Optional[Callable[[], None]] = None

        self._last_press_time: float = 0
        self._press_start_time: float = 0
        self._press_count: int = 0
        self._is_pressed: bool = False
        self._running: bool = False
        self._thread: Optional[threading.Thread] = None
        self._setup_complete: bool = False

    def setup(self) -> bool:
        """
        Setup GPIO for button input.

        Returns:
            True if setup successful
        """
        if not GPIO_AVAILABLE:
            print("GPIO not available")
            return False

        try:
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(self.gpio_pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

            # Add edge detection with debounce
            GPIO.add_event_detect(
                self.gpio_pin,
                GPIO.BOTH,
                callback=self._gpio_callback,
                bouncetime=self.debounce_ms
            )

            self._setup_complete = True
            print(f"Button setup on GPIO{self.gpio_pin}")
            return True

        except RuntimeError as e:
            # Common error: "Failed to add edge detection"
            # This usually means permission issues - user not in gpio group
            print(f"GPIO edge detection not available: {e}")
            print("  Hint: Add user to gpio group: sudo usermod -aG gpio $USER")
            print("  Button control will be disabled, but satellite will work.")
            return False

        except Exception as e:
            print(f"Failed to setup GPIO: {e}")
            print("  Button control will be disabled, but satellite will work.")
            return False

    def cleanup(self):
        """Cleanup GPIO"""
        self._running = False
        self._setup_complete = False

        if GPIO_AVAILABLE:
            try:
                GPIO.remove_event_detect(self.gpio_pin)
            except:
                pass
            try:
                GPIO.cleanup(self.gpio_pin)
            except RuntimeWarning:
                # "No channels have been set up yet" - this is fine
                pass
            except:
                pass

    def _gpio_callback(self, channel: int):
        """GPIO edge detection callback"""
        if not GPIO_AVAILABLE:
            return

        # Read current state (active low - pressed = 0)
        state = GPIO.input(self.gpio_pin)
        current_time = time.time()

        if state == 0:  # Button pressed
            self._is_pressed = True
            self._press_start_time = current_time

            # Check for double press
            if current_time - self._last_press_time < self.double_press_ms / 1000:
                self._press_count += 1
            else:
                self._press_count = 1

            # Start long press detection thread
            if not self._running:
                self._running = True
                self._thread = threading.Thread(
                    target=self._check_long_press,
                    daemon=True
                )
                self._thread.start()

        else:  # Button released
            self._is_pressed = False
            press_duration = current_time - self._press_start_time

            # Check press type
            if press_duration >= self.long_press_ms / 1000:
                # Long press already handled in thread
                pass
            elif self._press_count >= 2:
                # Double press
                if self._on_double_press:
                    self._on_double_press()
                self._press_count = 0
            else:
                # Single press - delay to check for double
                threading.Thread(
                    target=self._delayed_single_press,
                    args=(current_time,),
                    daemon=True
                ).start()

            self._last_press_time = current_time
            self._running = False

            # Call release callback
            if self._on_release:
                self._on_release()

    def _check_long_press(self):
        """Check if button is held for long press"""
        start = time.time()

        while self._running and self._is_pressed:
            if time.time() - start >= self.long_press_ms / 1000:
                if self._on_long_press:
                    self._on_long_press()
                break
            time.sleep(0.05)

    def _delayed_single_press(self, press_time: float):
        """Delay single press to allow for double press detection"""
        time.sleep(self.double_press_ms / 1000 + 0.05)

        # Only trigger if no additional presses occurred
        if self._press_count == 1 and time.time() - self._last_press_time >= self.double_press_ms / 1000:
            if self._on_press:
                self._on_press()
            self._press_count = 0

    def on_press(self, callback: Callable[[], None]):
        """Register single press callback"""
        self._on_press = callback

    def on_long_press(self, callback: Callable[[], None]):
        """Register long press callback"""
        self._on_long_press = callback

    def on_double_press(self, callback: Callable[[], None]):
        """Register double press callback"""
        self._on_double_press = callback

    def on_release(self, callback: Callable[[], None]):
        """Register button release callback"""
        self._on_release = callback

    @property
    def is_pressed(self) -> bool:
        """Check if button is currently pressed"""
        return self._is_pressed


class ButtonHandlerAsync:
    """Async wrapper for button handler"""

    def __init__(self, handler: ButtonHandler):
        self.handler = handler
        self._press_event = asyncio.Event()
        self._long_press_event = asyncio.Event()
        self._double_press_event = asyncio.Event()

        # Wire up sync callbacks to set async events
        handler.on_press(lambda: self._set_event(self._press_event))
        handler.on_long_press(lambda: self._set_event(self._long_press_event))
        handler.on_double_press(lambda: self._set_event(self._double_press_event))

    def _set_event(self, event: asyncio.Event):
        """Thread-safe event setter"""
        try:
            loop = asyncio.get_running_loop()
            loop.call_soon_threadsafe(event.set)
        except RuntimeError:
            pass

    async def wait_for_press(self, timeout: Optional[float] = None) -> bool:
        """
        Wait for button press.

        Args:
            timeout: Maximum wait time in seconds

        Returns:
            True if button was pressed, False if timeout
        """
        self._press_event.clear()
        try:
            await asyncio.wait_for(self._press_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def wait_for_long_press(self, timeout: Optional[float] = None) -> bool:
        """Wait for long press"""
        self._long_press_event.clear()
        try:
            await asyncio.wait_for(self._long_press_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def wait_for_double_press(self, timeout: Optional[float] = None) -> bool:
        """Wait for double press"""
        self._double_press_event.clear()
        try:
            await asyncio.wait_for(self._double_press_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False
