"""
Enviro pHAT Sensor Tests

Tests for renfield_satellite.hardware.enviro.EnviroSensor:
- Lazy import handling
- Sensor reading with individual error tolerance
- Lifecycle (open/close)
"""

import pytest
from unittest.mock import patch, MagicMock
import sys


class TestEnviroSensorInit:

    @pytest.mark.satellite
    def test_not_available_by_default(self):
        from renfield_satellite.hardware.enviro import EnviroSensor

        sensor = EnviroSensor()
        assert sensor.available is False
        assert sensor.read() is None


class TestEnviroSensorOpen:

    @pytest.mark.satellite
    def test_open_succeeds_with_library(self):
        from renfield_satellite.hardware.enviro import EnviroSensor

        mock_weather = MagicMock()
        mock_light = MagicMock()
        mock_motion = MagicMock()

        mock_module = MagicMock()
        mock_module.weather = mock_weather
        mock_module.light = mock_light
        mock_module.motion = mock_motion

        with patch.dict(sys.modules, {"envirophat": mock_module}):
            sensor = EnviroSensor()
            result = sensor.open()

        assert result is True
        assert sensor.available is True

    @pytest.mark.satellite
    def test_open_fails_without_library(self):
        from renfield_satellite.hardware.enviro import EnviroSensor

        sensor = EnviroSensor()
        with patch.dict(sys.modules, {"envirophat": None}):
            # Force ImportError by making the import fail
            with patch("builtins.__import__", side_effect=ImportError("No module named 'envirophat'")):
                result = sensor.open()

        assert result is False
        assert sensor.available is False


class TestEnviroSensorRead:

    def _make_sensor(self):
        from renfield_satellite.hardware.enviro import EnviroSensor

        sensor = EnviroSensor()
        sensor._available = True
        sensor._weather = MagicMock()
        sensor._light = MagicMock()
        sensor._motion = MagicMock()
        return sensor

    @pytest.mark.satellite
    def test_read_returns_all_sensor_data(self):
        sensor = self._make_sensor()
        sensor._weather.temperature.return_value = 22.3
        sensor._weather.pressure.return_value = 1013.25
        sensor._light.light.return_value = 450
        sensor._motion.heading.return_value = 180.5

        data = sensor.read()

        assert data["temperature"] == 22.3
        assert data["pressure"] == 1013.2  # rounded to 1 decimal
        assert data["light"] == 450
        assert data["heading"] == 180.5

    @pytest.mark.satellite
    def test_read_handles_individual_sensor_failure(self):
        sensor = self._make_sensor()
        sensor._weather.temperature.side_effect = OSError("I2C read failed")
        sensor._weather.pressure.return_value = 1013.0
        sensor._light.light.return_value = 200
        sensor._motion.heading.side_effect = OSError("I2C read failed")

        data = sensor.read()

        assert "temperature" not in data
        assert data["pressure"] == 1013.0
        assert data["light"] == 200
        assert "heading" not in data

    @pytest.mark.satellite
    def test_read_returns_none_when_all_fail(self):
        sensor = self._make_sensor()
        sensor._weather.temperature.side_effect = OSError()
        sensor._weather.pressure.side_effect = OSError()
        sensor._light.light.side_effect = OSError()
        sensor._motion.heading.side_effect = OSError()

        data = sensor.read()
        assert data is None

    @pytest.mark.satellite
    def test_read_returns_none_when_not_available(self):
        from renfield_satellite.hardware.enviro import EnviroSensor

        sensor = EnviroSensor()
        assert sensor.read() is None


class TestEnviroSensorClose:

    @pytest.mark.satellite
    def test_close_resets_state(self):
        from renfield_satellite.hardware.enviro import EnviroSensor

        sensor = EnviroSensor()
        sensor._available = True
        sensor._weather = MagicMock()

        sensor.close()

        assert sensor.available is False
        assert sensor._weather is None
