"""Explicit hook-registration entry point for ha_glue.

This module exists so that registering ha-glue hook handlers requires
an EXPLICIT call from platform startup code (currently
`api/lifecycle.py`). Importing the `ha_glue` package itself is
side-effect-free, which matters because the legacy compat re-export
in `models/database.py::__getattr__` does `from ha_glue.models import
database` to satisfy `from models.database import Room`. If hook
registration lived in `ha_glue/__init__.py` as an import side effect,
every platform service that touched an ha-glue model through the
compat shim would unconditionally register HA behavior — bypassing
the `settings.features["smart_home"]` gate that controls whether
RENFIELD_EDITION=pro deployments activate HA features.

The platform-side bootstrap is a single line:

    from ha_glue.bootstrap import register
    register()

wrapped in try/except so a missing or broken ha_glue package degrades
cleanly to "no HA fallback" without breaking Renfield startup.
"""

from __future__ import annotations

from loguru import logger


def register() -> None:
    """Register all ha_glue hook handlers with the platform hook system.

    Idempotent at the system level — calling twice would register the
    same handlers twice, so the hook would fire twice with first-result
    semantics still picking the same answer. We trust the caller to
    only invoke this once during startup.

    Failures are logged but never propagate. A broken handler module
    must not break Renfield startup.
    """
    try:
        from utils.hooks import register_hook

        from ha_glue.services.intent_fallback import ha_intent_fallback

        register_hook("intent_fallback_resolve", ha_intent_fallback)
        logger.info("ha_glue.bootstrap: registered intent_fallback_resolve handler")
    except Exception:  # noqa: BLE001 — startup must never break on plugin error
        logger.opt(exception=True).warning(
            "ha_glue.bootstrap: hook registration failed — HA fallback disabled"
        )
