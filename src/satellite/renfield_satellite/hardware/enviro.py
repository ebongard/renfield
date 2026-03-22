"""
Enviro pHAT sensor reader.

Reads temperature, pressure, light, and compass heading via I2C.
The envirophat library is imported lazily to avoid I2C init on unsupported hardware.
"""

from typing import Optional


class EnviroSensor:
    """Reads environmental data from a Pimoroni Enviro pHAT."""

    def __init__(self):
        self._available = False
        self._weather = None
        self._light = None
        self._motion = None

    def open(self) -> bool:
        try:
            from envirophat import weather, light, motion
            self._weather = weather
            self._light = light
            self._motion = motion
            self._available = True
            print("Enviro pHAT initialized")
            return True
        except Exception as e:
            print(f"Enviro pHAT not available: {e}")
            self._available = False
            return False

    def close(self):
        self._available = False
        self._weather = None
        self._light = None
        self._motion = None

    @property
    def available(self) -> bool:
        return self._available

    def read(self) -> Optional[dict]:
        if not self._available:
            return None

        data = {}

        try:
            data["temperature"] = round(self._weather.temperature(), 1)
        except Exception:
            pass

        try:
            data["pressure"] = round(self._weather.pressure(), 1)
        except Exception:
            pass

        try:
            data["light"] = self._light.light()
        except Exception:
            pass

        try:
            data["heading"] = round(self._motion.heading(), 1)
        except Exception:
            pass

        return data if data else None
