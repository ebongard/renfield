"""
XVF3800 LED Controller Tests

Tests for renfield_satellite.hardware.led.XVF3800LEDController:
- Pattern-to-effect mapping
- subprocess calls for LED control
- open/close lifecycle
- Error handling
"""

import pytest
from unittest.mock import patch, MagicMock


class TestXVF3800LEDControllerInit:

    @pytest.mark.satellite
    def test_default_parameters(self):
        from renfield_satellite.hardware.led import XVF3800LEDController

        ctrl = XVF3800LEDController()
        assert ctrl._xvf_host_path == "/opt/renfield-satellite/bin/xvf_host"
        assert ctrl.brightness == 20
        assert ctrl.idle_color is None

    @pytest.mark.satellite
    def test_custom_parameters(self):
        from renfield_satellite.hardware.led import XVF3800LEDController

        ctrl = XVF3800LEDController(
            xvf_host_path="/usr/local/bin/xvf_host",
            brightness=50,
            idle_color="green",
        )
        assert ctrl._xvf_host_path == "/usr/local/bin/xvf_host"
        assert ctrl.brightness == 50
        assert ctrl.idle_color == "green"


class TestXVF3800LEDControllerOpen:

    @pytest.mark.satellite
    @patch("os.access", return_value=True)
    @patch("os.path.isfile", return_value=True)
    def test_open_succeeds_when_binary_exists(self, mock_isfile, mock_access):
        from renfield_satellite.hardware.led import XVF3800LEDController

        ctrl = XVF3800LEDController()
        with patch.object(ctrl, "_run"):
            assert ctrl.open() is True

    @pytest.mark.satellite
    @patch("os.path.isfile", return_value=False)
    def test_open_fails_when_binary_missing(self, mock_isfile):
        from renfield_satellite.hardware.led import XVF3800LEDController

        ctrl = XVF3800LEDController()
        assert ctrl.open() is False

    @pytest.mark.satellite
    @patch("os.access", return_value=False)
    @patch("os.path.isfile", return_value=True)
    def test_open_fails_when_not_executable(self, mock_isfile, mock_access):
        from renfield_satellite.hardware.led import XVF3800LEDController

        ctrl = XVF3800LEDController()
        assert ctrl.open() is False


class TestXVF3800LEDControllerPatterns:

    @pytest.mark.satellite
    @patch("subprocess.run")
    def test_set_pattern_listening(self, mock_run):
        from renfield_satellite.hardware.led import XVF3800LEDController, LEDPattern

        ctrl = XVF3800LEDController(xvf_host_path="/bin/xvf_host")
        ctrl.set_pattern(LEDPattern.LISTENING)

        calls = [c.args[0] for c in mock_run.call_args_list]
        assert ["/bin/xvf_host", "LED_COLOR", "0x00ff00"] in calls
        assert ["/bin/xvf_host", "LED_EFFECT", "3"] in calls

    @pytest.mark.satellite
    @patch("subprocess.run")
    def test_set_pattern_boot_rainbow(self, mock_run):
        from renfield_satellite.hardware.led import XVF3800LEDController, LEDPattern

        ctrl = XVF3800LEDController(xvf_host_path="/bin/xvf_host")
        ctrl.set_pattern(LEDPattern.BOOT)

        mock_run.assert_called_once_with(
            ["/bin/xvf_host", "LED_EFFECT", "2"],
            capture_output=True, timeout=5, cwd="/bin",
        )

    @pytest.mark.satellite
    @patch("subprocess.run")
    def test_set_pattern_idle_dim_blue_breath(self, mock_run):
        from renfield_satellite.hardware.led import XVF3800LEDController, LEDPattern

        ctrl = XVF3800LEDController(xvf_host_path="/bin/xvf_host")
        ctrl.set_pattern(LEDPattern.IDLE)

        calls = [c.args[0] for c in mock_run.call_args_list]
        assert ["/bin/xvf_host", "LED_COLOR", "0x000044"] in calls
        assert ["/bin/xvf_host", "LED_EFFECT", "1"] in calls

    @pytest.mark.satellite
    @patch("subprocess.run")
    def test_set_pattern_off(self, mock_run):
        from renfield_satellite.hardware.led import XVF3800LEDController, LEDPattern

        ctrl = XVF3800LEDController(xvf_host_path="/bin/xvf_host")
        ctrl._pattern = LEDPattern.IDLE  # Set non-OFF so change triggers
        ctrl.set_pattern(LEDPattern.OFF)

        mock_run.assert_called_once_with(
            ["/bin/xvf_host", "LED_EFFECT", "0"],
            capture_output=True, timeout=5, cwd="/bin",
        )

    @pytest.mark.satellite
    @patch("subprocess.run")
    def test_set_pattern_error_red(self, mock_run):
        from renfield_satellite.hardware.led import XVF3800LEDController, LEDPattern

        ctrl = XVF3800LEDController(xvf_host_path="/bin/xvf_host")
        ctrl.set_pattern(LEDPattern.ERROR)

        calls = [c.args[0] for c in mock_run.call_args_list]
        assert ["/bin/xvf_host", "LED_COLOR", "0xff0000"] in calls
        assert ["/bin/xvf_host", "LED_EFFECT", "3"] in calls

    @pytest.mark.satellite
    @patch("subprocess.run")
    def test_set_pattern_processing_breath_yellow(self, mock_run):
        from renfield_satellite.hardware.led import XVF3800LEDController, LEDPattern

        ctrl = XVF3800LEDController(xvf_host_path="/bin/xvf_host")
        ctrl.set_pattern(LEDPattern.PROCESSING)

        calls = [c.args[0] for c in mock_run.call_args_list]
        assert ["/bin/xvf_host", "LED_COLOR", "0xffff00"] in calls
        assert ["/bin/xvf_host", "LED_EFFECT", "1"] in calls

    @pytest.mark.satellite
    @patch("subprocess.run")
    def test_duplicate_pattern_skipped(self, mock_run):
        from renfield_satellite.hardware.led import XVF3800LEDController, LEDPattern

        ctrl = XVF3800LEDController(xvf_host_path="/bin/xvf_host")
        ctrl.set_pattern(LEDPattern.LISTENING)
        mock_run.reset_mock()
        ctrl.set_pattern(LEDPattern.LISTENING)
        mock_run.assert_not_called()

    @pytest.mark.satellite
    def test_current_pattern_tracks_state(self):
        from renfield_satellite.hardware.led import XVF3800LEDController, LEDPattern

        ctrl = XVF3800LEDController(xvf_host_path="/bin/xvf_host")
        assert ctrl.current_pattern == LEDPattern.OFF

        with patch("subprocess.run"):
            ctrl.set_pattern(LEDPattern.SPEAKING)
        assert ctrl.current_pattern == LEDPattern.SPEAKING


class TestXVF3800LEDControllerClose:

    @pytest.mark.satellite
    @patch("subprocess.run")
    def test_close_turns_off_leds(self, mock_run):
        from renfield_satellite.hardware.led import XVF3800LEDController, LEDPattern

        ctrl = XVF3800LEDController(xvf_host_path="/bin/xvf_host")
        ctrl._pattern = LEDPattern.BOOT
        ctrl.close()

        mock_run.assert_called_once_with(
            ["/bin/xvf_host", "LED_EFFECT", "0"],
            capture_output=True, timeout=5, cwd="/bin",
        )
        assert ctrl.current_pattern == LEDPattern.OFF


class TestXVF3800LEDControllerErrors:

    @pytest.mark.satellite
    @patch("subprocess.run", side_effect=FileNotFoundError("xvf_host not found"))
    def test_subprocess_error_handled_gracefully(self, mock_run):
        from renfield_satellite.hardware.led import XVF3800LEDController, LEDPattern

        ctrl = XVF3800LEDController(xvf_host_path="/bin/xvf_host")
        # Should not raise
        ctrl.set_pattern(LEDPattern.LISTENING)
        assert ctrl.current_pattern == LEDPattern.LISTENING

    @pytest.mark.satellite
    def test_stop_animation_is_noop(self):
        from renfield_satellite.hardware.led import XVF3800LEDController

        ctrl = XVF3800LEDController()
        ctrl.stop_animation()  # Should not raise
