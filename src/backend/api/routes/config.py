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

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from models.database import User
from services.auth_service import get_user_or_default
from utils.config import settings

router = APIRouter()

# The richer Reva Wissensbasis surface (`/api/wissensbasis/trace` + `/me/mix`)
# is injected by the Reva adapter and is ABSENT in standalone Renfield, which
# only mounts /graph, /focus, /search. We expose its availability here so the
# frontend can hide the Reva-only side panels WITHOUT probing /me/mix — that
# probe 404s by design in standalone and spammed the browser console with red
# "Failed to load resource" + "API Error" lines. Detect by route presence.
_REVA_WISSENSBASIS_PROBE_PATH = "/api/wissensbasis/me/mix"


def _reva_wissensbasis_mounted(request: Request) -> bool:
    return any(
        getattr(r, "path", None) == _REVA_WISSENSBASIS_PROBE_PATH
        for r in request.app.routes
    )


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
    # Gates the chat agent-role badge + role-pin. See utils/config.py::role_surfacing_enabled.
    role_surfacing_enabled: bool
    # Gates the chat message-search UI (sidebar search field + results + jump-to-message).
    # Off => no search field rendered. See utils/config.py::message_search_enabled.
    message_search_enabled: bool
    # Gates chat artifacts Lane A (typed table/list/keyvalue/chart inline renderer).
    # Off => the ArtifactRenderer is inert (artifacts fall back to escaped text).
    # See utils/config.py::artifacts_typed_enabled + docs/design/chat-artifacts-sandbox.md.
    artifacts_typed_enabled: bool
    # Gates the chat room-handoff affordance (item 8): the quiet inline meta line
    # shown when Media Follow moves the user's playback to a new room. Off => the
    # `media_handoff` device-WS frame is never emitted and the indicator never
    # renders. See utils/config.py::room_handoff_enabled.
    room_handoff_enabled: bool
    # Gates the chat message-branching UI (edit/regenerate per-message actions).
    # Off => no edit/regenerate affordances; the backend also ignores any inbound
    # fork_from_message_id. The conversation tree + active-path query are always
    # on. See utils/config.py::chat_branching_enabled.
    chat_branching_enabled: bool
    # Gates the /projects nav + page (business-instance Phase 1). Off => no
    # Projects nav entry rendered. See utils/config.py::projects_enabled.
    projects_enabled: bool
    # Gates the /notes nav + page (Phase 4B, Notes as a 5th atom_type). Off => no
    # Notes nav/page + the note atom-source stays dark. See utils/config.py::notes_enabled.
    notes_enabled: bool
    # Gates the §2 meeting-transcription surface (upload + status + speaker
    # labeling). Off => no Meetings nav/page. See utils/config.py::
    # meeting_transcription_enabled + docs/design/meeting-transcription.md.
    meeting_transcription_enabled: bool
    # Gates the §2 Phase 3 minutes UI (generate/edit/confirm on a completed
    # meeting). Off => no minutes affordance. See utils/config.py::meeting_minutes_enabled.
    meeting_minutes_enabled: bool
    # True when the Reva-only Wissensbasis surface (/trace + /me/mix) is mounted
    # (Reva adapter present). Standalone Renfield => False. Lets the frontend
    # hide the Reva-only side panels without probing an endpoint that 404s.
    wissensbasis_reva_available: bool


@router.get("/features", response_model=FeatureFlags)
async def get_features(
    request: Request,
    _current_user: User = Depends(get_user_or_default),
) -> FeatureFlags:
    """Return the frontend-visible feature-flag allowlist."""
    return FeatureFlags(
        schicht_a_extraction_enabled=settings.schicht_a_extraction_enabled,
        wissen_workspace_enabled=settings.wissen_workspace_enabled,
        command_palette_enabled=settings.command_palette_enabled,
        role_surfacing_enabled=settings.role_surfacing_enabled,
        message_search_enabled=settings.message_search_enabled,
        artifacts_typed_enabled=settings.artifacts_typed_enabled,
        room_handoff_enabled=settings.room_handoff_enabled,
        chat_branching_enabled=settings.chat_branching_enabled,
        projects_enabled=settings.projects_enabled,
        notes_enabled=settings.notes_enabled,
        meeting_transcription_enabled=settings.meeting_transcription_enabled,
        meeting_minutes_enabled=settings.meeting_minutes_enabled,
        wissensbasis_reva_available=_reva_wissensbasis_mounted(request),
    )
