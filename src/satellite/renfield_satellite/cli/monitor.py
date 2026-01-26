#!/usr/bin/env python3
"""
Renfield Satellite Monitor CLI

Real-time monitoring and debugging tool for satellite voice assistants.
Displays audio levels, wake word detection, connection status, and more.

Usage:
    renfield-monitor              # Live monitoring mode
    renfield-monitor --test-mic   # Test microphone with level meter
    renfield-monitor --logs       # Show structured logs
    renfield-monitor --status     # Show current status and exit
"""

import argparse
import asyncio
import os
import signal
import sys
import time
from datetime import datetime
from typing import Optional

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class Colors:
    """ANSI color codes for terminal output"""
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"

    # Foreground colors
    BLACK = "\033[30m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    WHITE = "\033[37m"

    # Background colors
    BG_RED = "\033[41m"
    BG_GREEN = "\033[42m"
    BG_YELLOW = "\033[43m"
    BG_BLUE = "\033[44m"

    @classmethod
    def disable(cls):
        """Disable colors for non-TTY output"""
        for attr in dir(cls):
            if not attr.startswith('_') and isinstance(getattr(cls, attr), str):
                setattr(cls, attr, "")


def clear_screen():
    """Clear terminal screen"""
    os.system('clear' if os.name == 'posix' else 'cls')


def move_cursor(row: int, col: int):
    """Move cursor to position"""
    print(f"\033[{row};{col}H", end="")


def hide_cursor():
    """Hide terminal cursor"""
    print("\033[?25l", end="")


def show_cursor():
    """Show terminal cursor"""
    print("\033[?25h", end="")


def get_terminal_size():
    """Get terminal dimensions"""
    try:
        size = os.get_terminal_size()
        return size.columns, size.lines
    except OSError:
        return 80, 24


def format_duration(seconds: float) -> str:
    """Format duration in human-readable form"""
    if seconds < 60:
        return f"{seconds:.1f}s"
    elif seconds < 3600:
        mins = int(seconds // 60)
        secs = int(seconds % 60)
        return f"{mins}m {secs}s"
    else:
        hours = int(seconds // 3600)
        mins = int((seconds % 3600) // 60)
        return f"{hours}h {mins}m"


def create_bar(value: float, max_value: float, width: int, char: str = "█") -> str:
    """Create a progress bar string"""
    if max_value <= 0:
        return " " * width
    filled = int((value / max_value) * width)
    filled = max(0, min(filled, width))
    return char * filled + "░" * (width - filled)


def get_level_color(db: float) -> str:
    """Get color based on dB level"""
    if db > -6:
        return Colors.RED  # Clipping
    elif db > -12:
        return Colors.YELLOW  # Hot
    elif db > -30:
        return Colors.GREEN  # Good
    else:
        return Colors.DIM  # Quiet


class SatelliteMonitor:
    """Real-time satellite monitoring display"""

    def __init__(self):
        self.running = False
        self.audio_capture = None
        self.wake_detector = None
        self.ws_client = None
        self.satellite = None

        # Metrics
        self.audio_rms = 0.0
        self.audio_db = -96.0
        self.is_speech = False
        self.state = "unknown"
        self.last_wakeword = None
        self.connection_state = "disconnected"
        self.session_count = 0
        self.error_count = 0

        # System metrics
        self.cpu_percent = 0.0
        self.memory_percent = 0.0
        self.temperature = 0.0

    def _get_system_metrics(self):
        """Get system metrics (CPU, memory, temperature)"""
        try:
            import psutil
            self.cpu_percent = psutil.cpu_percent(interval=None)
            self.memory_percent = psutil.virtual_memory().percent

            # Try to get temperature (Raspberry Pi specific)
            try:
                with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
                    self.temperature = float(f.read().strip()) / 1000.0
            except (OSError, ValueError):
                self.temperature = 0.0

        except ImportError:
            pass

    def _rms_to_db(self, rms: float) -> float:
        """Convert RMS to dB (relative to max 16-bit value)"""
        if rms <= 0:
            return -96.0
        return 20 * (rms / 32768.0).__class__.__bases__[0].__getattribute__(
            (rms / 32768.0).__class__.__bases__[0], "__log10__"
        ) if hasattr((rms / 32768.0).__class__.__bases__[0], "__log10__") else -96.0

    def _calculate_rms(self, audio_chunk: bytes) -> float:
        """Calculate RMS from raw audio bytes"""
        import struct
        if len(audio_chunk) < 2:
            return 0.0

        # Convert bytes to 16-bit samples
        samples = struct.unpack(f"<{len(audio_chunk)//2}h", audio_chunk)
        if not samples:
            return 0.0

        # Calculate RMS
        sum_squares = sum(s * s for s in samples)
        return (sum_squares / len(samples)) ** 0.5

    def _calculate_db(self, rms: float) -> float:
        """Convert RMS to dB"""
        import math
        if rms <= 0:
            return -96.0
        return 20 * math.log10(rms / 32768.0)

    def render_header(self):
        """Render the header section"""
        cols, _ = get_terminal_size()
        now = datetime.now().strftime("%H:%M:%S")

        print(f"{Colors.BOLD}{Colors.CYAN}╔{'═' * (cols - 2)}╗{Colors.RESET}")
        title = "RENFIELD SATELLITE MONITOR"
        padding = (cols - len(title) - 4) // 2
        print(f"{Colors.BOLD}{Colors.CYAN}║{' ' * padding}{title}{' ' * (cols - padding - len(title) - 3)}║{Colors.RESET}")
        print(f"{Colors.BOLD}{Colors.CYAN}╚{'═' * (cols - 2)}╝{Colors.RESET}")
        print(f" {Colors.DIM}Time: {now}{Colors.RESET}")

    def render_connection_status(self):
        """Render connection status section"""
        print()
        print(f"{Colors.BOLD} CONNECTION{Colors.RESET}")
        print(f" {'─' * 40}")

        if self.connection_state == "connected":
            status_color = Colors.GREEN
            status_icon = "●"
        elif self.connection_state == "connecting":
            status_color = Colors.YELLOW
            status_icon = "○"
        else:
            status_color = Colors.RED
            status_icon = "○"

        print(f" Status: {status_color}{status_icon} {self.connection_state.upper()}{Colors.RESET}")
        print(f" State:  {Colors.CYAN}{self.state.upper()}{Colors.RESET}")

    def render_audio_levels(self):
        """Render audio level meters"""
        cols, _ = get_terminal_size()
        bar_width = min(50, cols - 20)

        print()
        print(f"{Colors.BOLD} AUDIO LEVELS{Colors.RESET}")
        print(f" {'─' * 40}")

        # RMS bar
        rms_normalized = min(self.audio_rms / 10000, 1.0)
        rms_bar = create_bar(rms_normalized, 1.0, bar_width)
        print(f" RMS:  [{rms_bar}] {self.audio_rms:.0f}")

        # dB bar and value
        db_normalized = (self.audio_db + 96) / 96  # -96dB to 0dB range
        db_color = get_level_color(self.audio_db)
        db_bar = create_bar(db_normalized, 1.0, bar_width)
        print(f" dB:   [{db_color}{db_bar}{Colors.RESET}] {self.audio_db:.1f} dB")

        # VAD indicator
        vad_status = f"{Colors.GREEN}SPEECH{Colors.RESET}" if self.is_speech else f"{Colors.DIM}silence{Colors.RESET}"
        print(f" VAD:  {vad_status}")

    def render_wakeword_status(self):
        """Render wake word detection status"""
        print()
        print(f"{Colors.BOLD} WAKE WORD{Colors.RESET}")
        print(f" {'─' * 40}")

        if self.last_wakeword:
            kw = self.last_wakeword
            ago = time.time() - kw.get("timestamp", 0)
            print(f" Last:       {Colors.GREEN}{kw.get('keyword', 'unknown')}{Colors.RESET}")
            print(f" Confidence: {kw.get('confidence', 0):.1%}")
            print(f" Ago:        {format_duration(ago)}")
        else:
            print(f" {Colors.DIM}No wake word detected yet{Colors.RESET}")

    def render_session_stats(self):
        """Render session statistics"""
        print()
        print(f"{Colors.BOLD} SESSIONS{Colors.RESET}")
        print(f" {'─' * 40}")
        print(f" Total (1h):  {self.session_count}")
        print(f" Errors (1h): {Colors.RED if self.error_count > 0 else ''}{self.error_count}{Colors.RESET}")

    def render_system_metrics(self):
        """Render system metrics"""
        print()
        print(f"{Colors.BOLD} SYSTEM{Colors.RESET}")
        print(f" {'─' * 40}")

        # CPU bar
        cpu_color = Colors.RED if self.cpu_percent > 80 else Colors.YELLOW if self.cpu_percent > 50 else Colors.GREEN
        print(f" CPU:  {cpu_color}{self.cpu_percent:5.1f}%{Colors.RESET}")

        # Memory bar
        mem_color = Colors.RED if self.memory_percent > 80 else Colors.YELLOW if self.memory_percent > 50 else Colors.GREEN
        print(f" Mem:  {mem_color}{self.memory_percent:5.1f}%{Colors.RESET}")

        # Temperature
        if self.temperature > 0:
            temp_color = Colors.RED if self.temperature > 70 else Colors.YELLOW if self.temperature > 60 else Colors.GREEN
            print(f" Temp: {temp_color}{self.temperature:5.1f}°C{Colors.RESET}")

    def render_footer(self):
        """Render footer with help"""
        cols, lines = get_terminal_size()
        print()
        print(f" {Colors.DIM}Press Ctrl+C to exit | q to quit{Colors.RESET}")

    def render(self):
        """Render the full monitor display"""
        clear_screen()
        move_cursor(1, 1)

        self.render_header()
        self.render_connection_status()
        self.render_audio_levels()
        self.render_wakeword_status()
        self.render_session_stats()
        self.render_system_metrics()
        self.render_footer()

        sys.stdout.flush()

    def _on_audio_chunk(self, audio_bytes: bytes):
        """Callback for audio chunks - updates metrics"""
        self.audio_rms = self._calculate_rms(audio_bytes)
        self.audio_db = self._calculate_db(self.audio_rms)
        # Simple VAD: speech if RMS > threshold
        self.is_speech = self.audio_rms > 500

    async def update_metrics_from_audio(self):
        """Update metrics by reading from audio capture"""
        try:
            from ..audio.capture import AudioCapture

            self.audio_capture = AudioCapture(
                sample_rate=16000,
                chunk_size=1024,
                channels=1,
            )
            self.audio_capture.start(self._on_audio_chunk)

            # Check if capture actually started
            if self.audio_capture._running:
                self.connection_state = "standalone"
                self.state = "monitoring"
            else:
                self.connection_state = "error"
                self.state = "capture failed"
                return

            # Keep running while self.running is True
            while self.running:
                await asyncio.sleep(0.1)

            # Stop capture when done
            self.audio_capture.stop()

        except ImportError as e:
            self.connection_state = f"error: {e}"
            self.state = "no audio"
        except Exception as e:
            self.connection_state = f"error"
            self.state = str(e)[:30]

    async def update_system_metrics(self):
        """Periodically update system metrics"""
        while self.running:
            self._get_system_metrics()
            await asyncio.sleep(2.0)

    async def run_live_monitor(self):
        """Run the live monitoring display"""
        self.running = True
        hide_cursor()

        # Set up signal handlers
        def signal_handler(sig, frame):
            self.running = False

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        try:
            # Start background tasks
            tasks = [
                asyncio.create_task(self.update_metrics_from_audio()),
                asyncio.create_task(self.update_system_metrics()),
            ]

            # Main render loop
            while self.running:
                self.render()
                await asyncio.sleep(0.1)

            # Cancel background tasks
            for task in tasks:
                task.cancel()

        finally:
            show_cursor()
            clear_screen()
            print("Monitor stopped.")

    def run_mic_test(self, duration: int = 10):
        """Run microphone test with level display"""
        print(f"{Colors.BOLD}Microphone Test{Colors.RESET}")
        print(f"Testing for {duration} seconds...")
        print()

        try:
            from ..audio.capture import AudioCapture

            # Shared state for audio metrics
            test_state = {"rms": 0.0, "db": -96.0, "max_rms": 0.0, "max_db": -96.0}

            def on_audio(audio_bytes: bytes):
                rms = self._calculate_rms(audio_bytes)
                db = self._calculate_db(rms)
                test_state["rms"] = rms
                test_state["db"] = db
                test_state["max_rms"] = max(test_state["max_rms"], rms)
                test_state["max_db"] = max(test_state["max_db"], db)

            capture = AudioCapture(
                sample_rate=16000,
                chunk_size=1024,
                channels=1,
            )
            capture.start(on_audio)

            start = time.time()

            while time.time() - start < duration:
                rms = test_state["rms"]
                db = test_state["db"]

                # Create level bar
                bar_width = 50
                db_normalized = (db + 96) / 96
                bar = create_bar(db_normalized, 1.0, bar_width)
                color = get_level_color(db)

                print(f"\r [{color}{bar}{Colors.RESET}] {db:6.1f} dB  RMS: {rms:7.0f}", end="")
                sys.stdout.flush()

                time.sleep(0.05)

            capture.stop()

            max_rms = test_state["max_rms"]
            max_db = test_state["max_db"]

            print()
            print()
            print(f"Test complete!")
            print(f"  Max RMS: {max_rms:.0f}")
            print(f"  Max dB:  {max_db:.1f} dB")

            if max_db < -50:
                print(f"  {Colors.RED}⚠ Audio level very low - check microphone{Colors.RESET}")
            elif max_db < -30:
                print(f"  {Colors.YELLOW}⚠ Audio level low{Colors.RESET}")
            else:
                print(f"  {Colors.GREEN}✓ Audio levels OK{Colors.RESET}")

        except ImportError as e:
            print(f"{Colors.RED}Error: Could not import audio module: {e}{Colors.RESET}")
            print(f"{Colors.DIM}Make sure PyAudio is installed: pip install pyaudio{Colors.RESET}")
        except Exception as e:
            print(f"{Colors.RED}Error: {e}{Colors.RESET}")

    def show_status(self):
        """Show current status and exit"""
        print(f"{Colors.BOLD}Renfield Satellite Status{Colors.RESET}")
        print()

        # Try to load config
        try:
            from config import load_config
            config = load_config()
            print(f"  Satellite ID: {config.satellite_id}")
            print(f"  Room:         {config.room}")
            print(f"  Server:       {config.server_url}")
        except Exception:
            print(f"  {Colors.DIM}(Config not loaded){Colors.RESET}")

        print()

        # System metrics
        self._get_system_metrics()
        print(f"  CPU:    {self.cpu_percent:.1f}%")
        print(f"  Memory: {self.memory_percent:.1f}%")
        if self.temperature > 0:
            print(f"  Temp:   {self.temperature:.1f}°C")


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(
        description="Renfield Satellite Monitor - Real-time debugging tool",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  renfield-monitor              Live monitoring mode
  renfield-monitor --test-mic   Test microphone levels
  renfield-monitor --status     Show current status
        """
    )

    parser.add_argument(
        "--test-mic",
        action="store_true",
        help="Test microphone with level meter"
    )
    parser.add_argument(
        "--test-duration",
        type=int,
        default=10,
        help="Duration for mic test in seconds (default: 10)"
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Show current status and exit"
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable colored output"
    )

    args = parser.parse_args()

    # Disable colors if requested or not a TTY
    if args.no_color or not sys.stdout.isatty():
        Colors.disable()

    monitor = SatelliteMonitor()

    if args.test_mic:
        monitor.run_mic_test(args.test_duration)
    elif args.status:
        monitor.show_status()
    else:
        # Live monitoring mode
        asyncio.run(monitor.run_live_monitor())


if __name__ == "__main__":
    main()
