"""
Button Handler for ReSpeaker 2-Mics Pi HAT

Handles the user button on GPIO17.
Supports lgpio (recommended for Bookworm/kernel 6.x) and RPi.GPIO as fallback.
"""

import asyncio
import os
import threading
import time
from typing import Callable, Optional

# Try lgpio first (works better on newer Pi OS with kernel 6.x)
LGPIO_AVAILABLE = False
RPIGPIO_AVAILABLE = False

try:
    import lgpio
    LGPIO_AVAILABLE = True
except ImportError:
    lgpio = None

# Fall back to RPi.GPIO if lgpio not available
if not LGPIO_AVAILABLE:
    try:
        import RPi.GPIO as GPIO
        RPIGPIO_AVAILABLE = True
    except ImportError:
        GPIO = None

GPIO_AVAILABLE = LGPIO_AVAILABLE or RPIGPIO_AVAILABLE

if not GPIO_AVAILABLE:
    print("Warning: Neither lgpio nor RPi.GPIO installed. Button control disabled.")
    print("Install with: pip install lgpio  (recommended for Bookworm)")
    print("         or: pip install RPi.GPIO")


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

        # lgpio specific
        self._lgpio_handle: Optional[int] = None
        self._lgpio_callback = None

    def _unexport_sysfs_gpio(self):
        """
        Unexport GPIO from sysfs if it was exported.

        This fixes "Failed to add edge detection" when GPIO was previously
        exported via /sys/class/gpio by another process or previous run.
        """
        sysfs_gpio_path = f"/sys/class/gpio/gpio{self.gpio_pin}"
        unexport_path = "/sys/class/gpio/unexport"

        if os.path.exists(sysfs_gpio_path):
            try:
                with open(unexport_path, 'w') as f:
                    f.write(str(self.gpio_pin))
                print(f"Unexported GPIO{self.gpio_pin} from sysfs")
                time.sleep(0.1)  # Give kernel time to cleanup
            except (IOError, PermissionError) as e:
                print(f"Could not unexport GPIO{self.gpio_pin}: {e}")

    def _cleanup_existing_edge_detection(self):
        """Remove any existing edge detection from previous runs (RPi.GPIO only)"""
        if not RPIGPIO_AVAILABLE:
            return

        # First try to unexport from sysfs
        self._unexport_sysfs_gpio()

        # Set GPIO mode
        try:
            GPIO.setmode(GPIO.BCM)
        except ValueError:
            # Mode already set, that's fine
            pass

        # Try to remove existing edge detection
        try:
            GPIO.remove_event_detect(self.gpio_pin)
            print(f"Removed existing edge detection on GPIO{self.gpio_pin}")
        except (RuntimeError, ValueError):
            # No edge detection to remove, that's fine
            pass

        # Cleanup the pin completely
        try:
            GPIO.cleanup(self.gpio_pin)
        except (RuntimeWarning, ValueError):
            pass

    def setup(self) -> bool:
        """
        Setup GPIO for button input.

        Returns:
            True if setup successful
        """
        if not GPIO_AVAILABLE:
            print("GPIO not available")
            return False

        # Use lgpio if available (works on Bookworm/kernel 6.x)
        if LGPIO_AVAILABLE:
            return self._setup_lgpio()
        else:
            return self._setup_rpigpio()

    def _setup_lgpio(self) -> bool:
        """Setup using lgpio library (recommended for newer Pi OS)"""
        try:
            # Open GPIO chip
            self._lgpio_handle = lgpio.gpiochip_open(0)

            # Claim pin as input with pull-up and edge detection
            lgpio.gpio_claim_alert(
                self._lgpio_handle,
                self.gpio_pin,
                lgpio.BOTH_EDGES,
                lgpio.SET_PULL_UP
            )

            # Set debounce
            lgpio.gpio_set_debounce_micros(
                self._lgpio_handle,
                self.gpio_pin,
                self.debounce_ms * 1000
            )

            # Register callback
            self._lgpio_callback = lgpio.callback(
                self._lgpio_handle,
                self.gpio_pin,
                lgpio.BOTH_EDGES,
                self._lgpio_edge_callback
            )

            self._setup_complete = True
            print(f"Button setup on GPIO{self.gpio_pin} (lgpio)")
            return True

        except Exception as e:
            print(f"Failed to setup GPIO with lgpio: {e}")
            print("  Button control will be disabled, but satellite will work.")
            if self._lgpio_handle is not None:
                try:
                    lgpio.gpiochip_close(self._lgpio_handle)
                except Exception:
                    pass
                self._lgpio_handle = None
            return False

    def _lgpio_edge_callback(self, chip, gpio, level, timestamp):
        """lgpio edge detection callback"""
        # level: 0 = low (pressed), 1 = high (released), 2 = watchdog
        if level == 2:
            return

        current_time = time.time()

        if level == 0:  # Button pressed (active low)
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

        else:  # Button released (level == 1)
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

    def _setup_rpigpio(self) -> bool:
        """Setup using RPi.GPIO library (fallback for older Pi OS)"""
        try:
            # First, cleanup any existing state
            self._cleanup_existing_edge_detection()

            # Small delay after cleanup
            time.sleep(0.1)

            # Set mode
            GPIO.setmode(GPIO.BCM)

            # Setup pin with pull-up
            GPIO.setup(self.gpio_pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

            # Add edge detection with debounce
            GPIO.add_event_detect(
                self.gpio_pin,
                GPIO.BOTH,
                callback=self._gpio_callback,
                bouncetime=self.debounce_ms
            )

            self._setup_complete = True
            print(f"Button setup on GPIO{self.gpio_pin} (RPi.GPIO)")
            return True

        except RuntimeError as e:
            error_msg = str(e)
            print(f"Failed to setup GPIO: {e}")

            if "Failed to add edge detection" in error_msg:
                print("\n  Possible fixes:")
                print("  1. Install lgpio instead: pip install lgpio")
                print("  2. Add user to gpio group: sudo usermod -aG gpio $USER")
                print("  3. Reboot the Raspberry Pi: sudo reboot")
                print("  4. Check if another process uses GPIO17:")
                print("     sudo fuser /dev/gpiomem")
                print("     sudo lsof /dev/gpiomem")

            print("\n  Button control will be disabled, but satellite will work.")
            return False

        except Exception as e:
            print(f"Failed to setup GPIO: {e}")
            print("  Button control will be disabled, but satellite will work.")
            return False

    def cleanup(self):
        """Cleanup GPIO"""
        self._running = False
        self._setup_complete = False

        # Cleanup lgpio
        if LGPIO_AVAILABLE and self._lgpio_handle is not None:
            try:
                if self._lgpio_callback:
                    self._lgpio_callback.cancel()
                    self._lgpio_callback = None
            except Exception:
                pass
            try:
                lgpio.gpio_free(self._lgpio_handle, self.gpio_pin)
            except Exception:
                pass
            try:
                lgpio.gpiochip_close(self._lgpio_handle)
            except Exception:
                pass
            self._lgpio_handle = None
            return

        # Cleanup RPi.GPIO
        if RPIGPIO_AVAILABLE:
            try:
                GPIO.remove_event_detect(self.gpio_pin)
            except Exception:
                pass
            try:
                GPIO.cleanup(self.gpio_pin)
            except RuntimeWarning:
                # "No channels have been set up yet" - this is fine
                pass
            except Exception:
                pass

    def _gpio_callback(self, channel: int):
        """GPIO edge detection callback (RPi.GPIO)"""
        if not RPIGPIO_AVAILABLE:
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
