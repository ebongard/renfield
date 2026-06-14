"""Satellite LED night-dimming service.

Backend-driven push (Approach A): on the platform `daypart_changed` hook the
backend computes a target LED brightness (night → ``settings.led_night_brightness``,
otherwise ``settings.led_day_brightness``) and pushes it to every connected
satellite over the existing WebSocket as a ``{"type": "led_config",
"brightness": N}`` message. The satellite scales all its LED animations to that
brightness — animations are never disabled.

The current target brightness is held in process so the satellite WebSocket
handler can ride it in ``register_ack``; a satellite that reconnects mid-night
therefore comes up already dimmed.

Everything here is best-effort: a per-satellite send failure is logged and
swallowed, never raised, so one dead link can't break the push to the others or
the hook chain.
"""

from __future__ import annotations

from loguru import logger

from utils.config import settings


class LedDimmingService:
    """Holds the current satellite LED brightness and pushes it on daypart change.

    Singleton — access via :func:`get_led_dimming_service`. ``initialize`` seeds
    the current brightness from the live daypart and registers the
    ``daypart_changed`` hook handler.
    """

    def __init__(self) -> None:
        # Default to the day level until initialize() reads the real daypart.
        self._current_brightness: int = settings.led_day_brightness
        self._initialized: bool = False

    @staticmethod
    def _brightness_for_daypart(daypart: str) -> int:
        """Resolve the target brightness for a daypart string."""
        if daypart == "night":
            return settings.led_night_brightness
        return settings.led_day_brightness

    async def initialize(self) -> None:
        """Seed current brightness from the live daypart + register the hook.

        Idempotent: registering twice is avoided so a double startup call does
        not push twice per transition.
        """
        from services.daypart_service import is_night

        try:
            night = is_night()
        except Exception:  # noqa: BLE001 — never break startup on a clock error
            logger.opt(exception=True).warning(
                "LedDimmingService: is_night() failed, assuming day"
            )
            night = False

        self._current_brightness = (
            settings.led_night_brightness if night else settings.led_day_brightness
        )

        if not self._initialized:
            from utils.hooks import is_hook_registered, register_hook

            if not is_hook_registered("daypart_changed", self._on_daypart_changed):
                register_hook("daypart_changed", self._on_daypart_changed)
            self._initialized = True

        logger.info(
            f"💡 LedDimmingService initialized: night={night}, "
            f"brightness={self._current_brightness}"
        )

    async def _on_daypart_changed(
        self, *, previous: str | None = None, current: str, local_time: str = "",
    ) -> None:
        """`daypart_changed` hook handler — recompute + push the new brightness."""
        self._current_brightness = self._brightness_for_daypart(current)
        logger.info(
            f"💡 Daypart {previous} → {current} (local {local_time}): "
            f"LED brightness → {self._current_brightness}"
        )
        await self.push_brightness_to_all_satellites(self._current_brightness)

    async def push_brightness_to_all_satellites(self, brightness: int) -> None:
        """Send a ``led_config`` message to every connected satellite.

        Per-satellite send failures are caught and logged; this never raises.
        """
        from ha_glue.services.satellite_manager import get_satellite_manager

        manager = get_satellite_manager()
        message = {"type": "led_config", "brightness": int(brightness)}

        sent = 0
        for sat in list(manager.satellites.values()):
            try:
                await sat.websocket.send_json(message)
                sent += 1
            except Exception as e:  # noqa: BLE001 — one dead link must not break the rest
                logger.warning(
                    f"💡 Failed to push LED brightness to {sat.satellite_id}: {e}"
                )

        logger.info(
            f"💡 Pushed LED brightness {brightness} to {sent} satellite(s)"
        )

    def get_current_led_brightness(self) -> int:
        """Return the current target LED brightness (for register_ack)."""
        return self._current_brightness


# Global singleton instance
_led_dimming_service: LedDimmingService | None = None


def get_led_dimming_service() -> LedDimmingService:
    """Get or create the global LedDimmingService instance."""
    global _led_dimming_service
    if _led_dimming_service is None:
        _led_dimming_service = LedDimmingService()
    return _led_dimming_service
