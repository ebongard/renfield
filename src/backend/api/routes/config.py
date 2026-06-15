"""
Config API — frontend-visible feature flags.

The frontend needs to know which optional backend capabilities are enabled so
it can render honest UI (e.g. the Fakten panel distinguishes "no facts found"
from "Schicht-A extraction is disabled"). Backend settings live in
``utils/config.py`` and are NOT otherwise exposed to the browser — this route
is the one narrow, intentional seam.

Only flags that genuinely change frontend behavior belong here. This is a
read-only allowlist (NOT a dump of ``settings``) so a new backend setting never
leaks to the client by accident.

``get_user_or_default`` keeps it working in both auth modes (single-user gets
the default user); it carries no per-user data, so any authenticated caller
sees the same flags.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from models.database import User
from services.auth_service import get_user_or_default
from utils.config import settings

router = APIRouter()


class FeatureFlags(BaseModel):
    """Frontend-visible feature flags. Allowlist — add a field here only when
    the frontend must branch on it."""
    schicht_a_extraction_enabled: bool
    # Gates the unified /wissen workspace nav + routing (frontend). Off => the
    # legacy flat corpus nav. See utils/config.py::wissen_workspace_enabled.
    wissen_workspace_enabled: bool
    # Gates the chat command palette UI (the `/`-trigger + touch button + overlay).
    # Off => no palette elements rendered. See utils/config.py::command_palette_enabled.
    command_palette_enabled: bool


@router.get("/features", response_model=FeatureFlags)
async def get_features(
    _current_user: User = Depends(get_user_or_default),
) -> FeatureFlags:
    """Return the frontend-visible feature-flag allowlist."""
    return FeatureFlags(
        schicht_a_extraction_enabled=settings.schicht_a_extraction_enabled,
        wissen_workspace_enabled=settings.wissen_workspace_enabled,
        command_palette_enabled=settings.command_palette_enabled,
    )
