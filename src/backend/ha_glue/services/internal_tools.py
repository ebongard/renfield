"""
ha_glue Internal Agent Tools — Home-automation tools for the Agent Loop.

Moved from `services/internal_tools.py` in Phase 1 W4 follow-up (see
ebongard/renfield#358). Every tool in this file touches ha_glue
subsystems — room resolution, HA media players, DLNA renderers, BLE
presence, TuneIn radio. Platform-only deploys should never see these.

The `internal.knowledge_search` tool that used to share this module
moved to `services/knowledge_tool.py` — it is pure-RAG, has no
ha-glue dependencies, and belongs on the platform.

Registration / dispatch:
- `ha_glue/bootstrap.py::ha_glue_register_tools` hooks the `register_tools`
  event and adds every tool in `InternalToolService.TOOLS` to the agent's
  `ToolRegistry`. Platform-only deploys (no handler) see none of them.
- `ha_glue/bootstrap.py::ha_glue_execute_tool` hooks `execute_tool` and
  dispatches any `internal.*` intent to `InternalToolService.execute()`.

Platform callers (agent_tools.py, action_executor.py, chat_handler.py)
no longer import this module directly. They go through the hook system
so platform-only deploys never reach the ha_glue package.
"""
import asyncio
import time

from loguru import logger


class InternalToolService:
    """Provider-agnostic internal tools for the Agent Loop."""

    TOOLS: dict[str, dict] = {
        "internal.resolve_room_player": {
            "description": "Find the media_player entity for a room by name",
            "parameters": {
                "room_name": "Room name to look up (required)",
            },
        },
        "internal.play_in_room": {
            "description": "Play a media URL on the audio device in a room via Home Assistant. If the device is busy, returns status 'busy' — ask the user and retry with force=true.",
            "parameters": {
                "media_url": "Playable media URL (required)",
                "room_name": "Target room name (required)",
                "media_type": "Content type: music, video, playlist (default: music)",
                "force": "Set to 'true' to interrupt current playback (default: false)",
                "title": "Display title for the media player (optional)",
                "thumb": "Thumbnail/album art URL for the media player (optional)",
                # "queue" parameter reserved for future multi-track support.
            },
        },
        "internal.get_user_location": {
            "description": "Get the current or last known room location of a user. Accepts username or first/last name.",
            "parameters": {
                "user_name": "Name of the user to locate (username, first name, or last name)",
            },
        },
        "internal.announce_in_room": {
            "description": "Speak a text message out loud (TTS) on a room's audio device. Use it to relay a message to a person: find their room first (internal.get_user_location), then announce. Privacy: set privacy='personal' for personal/confidential content (when in doubt, do). A personal message is spoken ONLY if everyone currently in the room is an intended recipient (list them in for_users) — so two recipients together get it, but a non-recipient present blocks it (you get blocked='not_private'). When blocked: announce a NEUTRAL note that a message is waiting (privacy='public', NO content), and if the recipient says go ahead, call this again with force=true.",
            "parameters": {
                "text": "The message to speak aloud (required)",
                "room_name": "The room to announce in (required) — e.g. the room the person is currently in",
                "privacy": "'public' (default, anyone may overhear) or 'personal' (confidential)",
                "for_users": "Intended recipient(s) — a name or list of names. A personal message plays only if everyone present is in this list.",
                "force": "'true' to announce a personal message even if non-recipients are present — ONLY after the recipient has explicitly consented (default false)",
            },
        },
        "internal.get_all_presence": {
            "description": "Get all currently present users and their room locations. Use this when asked 'where is everyone?' or 'who is home?'.",
            "parameters": {},
        },
        "internal.media_control": {
            "description": "Control media playback in a room: stop, pause, resume, next, previous, volume, mute, unmute, seek, play_mode, or query status (what's playing). Works with both Home Assistant media players and DLNA renderers.",
            "parameters": {
                "action": "Control action: stop, pause, resume, next, previous, volume, mute, unmute, status, seek, play_mode (required)",
                "room_name": "Target room name (required)",
                "volume": "Absolute volume 0-100 (use for 'set volume to X'). Mutually exclusive with volume_step.",
                "volume_step": "Relative volume change in percentage points, e.g. -20 for 20% quieter, +10 for louder (use for 'leiser/lauter um X'). Mutually exclusive with volume.",
                "position_seconds": "Target offset in seconds from the track start (required when action is 'seek')",
                "mode": "Play mode: normal, repeat_one, repeat_all, shuffle, random (required when action is 'play_mode')",
            },
        },
        "internal.play_album_on_dlna": {
            "description": "Play a Jellyfin album on a DLNA renderer with gapless queue. Fetches tracks from Jellyfin and sends them all to the DLNA renderer in one step. Provide either renderer_name (direct) or room_name (resolves via room configuration).",
            "parameters": {
                "album_id": "Jellyfin album ID from search_media results (required)",
                "renderer_name": "DLNA renderer name, e.g. 'Arbeitszimmer' (optional if room_name is given)",
                "room_name": "Room name to resolve the DLNA renderer from room config (optional if renderer_name is given)",
                "album_name": "Album title for display metadata (optional, from search_media results)",
            },
        },
        "internal.play_video_on_dlna": {
            "description": "Play a Jellyfin movie or episode on a DLNA renderer (Smart TV). Fetches the video stream URL from Jellyfin and plays it with video DIDL metadata. Provide either renderer_name (direct) or room_name (resolves via visual output config).",
            "parameters": {
                "item_id": "Jellyfin item ID for the movie or episode (required)",
                "renderer_name": "DLNA renderer name (optional if room_name given)",
                "room_name": "Room name to resolve visual DLNA renderer (optional if renderer_name given)",
                "title": "Display title (optional)",
                "image_url": "Thumbnail URL (optional)",
            },
        },
        "internal.play_from_server": {
            "description": "Play a library object (album/playlist/folder/track) from a DLNA MediaServer (e.g. a NAS/Jellyfin library) on a room's audio device. Use AFTER mcp.dlna.list_servers + browse_server/search_server to obtain a server_name + object_id — no content URLs needed.",
            "parameters": {
                "server_name": "DLNA MediaServer name from mcp.dlna.list_servers (required)",
                "object_id": "Library object id from mcp.dlna.browse_server/search_server (required)",
                "room_name": "Target room name whose DLNA renderer to play on (required)",
            },
        },
        "internal.play_radio": {
            "description": "Play a radio station in a room. Resolves the stream URL from a station ID and plays it on the room's audio device. ALWAYS call mcp.radio.search_stations FIRST to obtain the station_id — TuneIn IDs are opaque and cannot be known without searching.",
            "parameters": {
                "station_id": "Opaque TuneIn station ID taken from a mcp.radio.search_stations result in THIS request (required). NEVER invent, guess, copy from an example, or reuse an ID from memory/context — always search first.",
                "room_name": "Target room name (required)",
                "station_name": "Station display name for the media player UI (optional)",
                "station_image": "Station logo URL for the media player UI (optional)",
                "force": "Set to 'true' to interrupt current playback (default: false)",
            },
        },
        "internal.save_radio_favorite": {
            "description": "Save a radio station as a favorite for the current user. Idempotent — saving the same station twice is a no-op.",
            "parameters": {
                "station_id": "TuneIn station ID (required)",
                "station_name": "Station name (required)",
                "station_image": "Station logo URL (optional)",
                "genre": "Genre name (optional)",
            },
        },
        "internal.list_radio_favorites": {
            "description": "List the current user's saved radio station favorites.",
            "parameters": {},
        },
        "internal.remove_radio_favorite": {
            "description": "Remove a radio station from the current user's favorites.",
            "parameters": {
                "station_id": "TuneIn station ID to remove (required)",
            },
        },
        "internal.presence_history": {
            "description": "Query a user's PERSISTED presence history (survives restarts, unlike the live current-location tools). Use for 'where was X earlier/yesterday', 'when was X last in the kitchen', or 'who was in the living room this morning'. Accepts username or first/last name.",
            "parameters": {
                "user_name": "Name of the user (username, first or last name). Required for 'timeline' and 'last_seen_by_room'.",
                "room_name": "Room name to focus on. Required for 'who_was_in_room'; optional filter for 'timeline'.",
                "since": "Start of the window as ISO8601 (e.g. 2026-06-14T08:00:00). Default: 24h ago.",
                "until": "End of the window as ISO8601. Default: now.",
                "query_type": "'timeline' (a user's room enter/leave events, default), 'last_seen_by_room' (per-room last seen for a user), or 'who_was_in_room' (everyone's events in a room over the window).",
            },
        },
    }

    _HANDLERS = {
        "internal.resolve_room_player": "_resolve_room_player",
        "internal.play_in_room": "_play_in_room",
        "internal.get_user_location": "_get_user_location",
        "internal.get_all_presence": "_get_all_presence",
        "internal.announce_in_room": "_announce_in_room",
        "internal.media_control": "_media_control",
        "internal.play_album_on_dlna": "_play_album_on_dlna",
        "internal.play_video_on_dlna": "_play_video_on_dlna",
        "internal.play_from_server": "_play_from_server",
        "internal.play_radio": "_play_radio",
        "internal.save_radio_favorite": "_save_radio_favorite",
        "internal.list_radio_favorites": "_list_radio_favorites",
        "internal.remove_radio_favorite": "_remove_radio_favorite",
        "internal.presence_history": "_presence_history",
    }

    async def execute(self, intent: str, parameters: dict) -> dict:
        """Route to the correct internal tool handler."""
        handler_name = self._HANDLERS.get(intent)
        if not handler_name:
            return {
                "success": False,
                "message": f"Unknown internal tool: {intent}",
                "action_taken": False,
            }

        handler = getattr(self, handler_name)
        return await handler(parameters)

    async def _resolve_room_player(self, params: dict) -> dict:
        """
        Resolve room_name → {entity_id, room_name, device_name}.

        Uses RoomService (name/alias lookup) + OutputRoutingService (best audio device).
        """
        room_name = params.get("room_name", "").strip()
        if not room_name:
            return {
                "success": False,
                "message": "Parameter 'room_name' is required",
                "action_taken": False,
            }

        try:
            from services.database import AsyncSessionLocal
            from ha_glue.services.output_routing_service import OutputRoutingService
            from ha_glue.services.room_service import RoomService

            async with AsyncSessionLocal() as db:
                room_service = RoomService(db)

                # Try exact name first, then alias
                room = await room_service.get_room_by_name(room_name)
                if not room:
                    room = await room_service.get_room_by_alias(room_name)

                if not room:
                    return {
                        "success": False,
                        "message": f"Room '{room_name}' not found",
                        "action_taken": False,
                    }

                # Find best audio output device for the room
                routing_service = OutputRoutingService(db)
                decision = await routing_service.get_audio_output_for_room(room.id)

                if decision.reason == "no_output_devices_configured":
                    return {
                        "success": False,
                        "message": f"No audio output device configured for room '{room.name}'",
                        "action_taken": False,
                    }

                if decision.reason == "all_devices_unavailable":
                    # Device exists but is busy/off — tell the agent so it
                    # can inform the user and ask whether to interrupt.
                    # Re-fetch the first enabled device to include its info.
                    from sqlalchemy import select as sa_select

                    from models.database import OUTPUT_TYPE_AUDIO, RoomOutputDevice
                    stmt = (
                        sa_select(RoomOutputDevice)
                        .where(RoomOutputDevice.room_id == room.id)
                        .where(RoomOutputDevice.output_type == OUTPUT_TYPE_AUDIO)
                        .where(RoomOutputDevice.is_enabled.is_(True))
                        .order_by(RoomOutputDevice.priority)
                        .limit(1)
                    )
                    result = await db.execute(stmt)
                    busy_device = result.scalar_one_or_none()
                    device_name = busy_device.device_name if busy_device else "unknown"
                    data = {
                        "room_name": room.name,
                        "device_name": device_name,
                        "status": "busy",
                    }
                    if busy_device:
                        # Branch on dual-read target_type so generic-provider rows
                        # (samsung/sonos) report their real type + id, not a null HA
                        # entity. Legacy renfield/HA/dlna rows resolve identically.
                        tt = busy_device.target_type
                        data["target_type"] = tt
                        if tt == "dlna":
                            data["dlna_renderer_name"] = busy_device.dlna_renderer_name
                        elif tt == "homeassistant":
                            data["entity_id"] = busy_device.ha_entity_id
                        else:
                            data["output_target_id"] = busy_device.target_id
                    else:
                        data["target_type"] = "homeassistant"
                        data["entity_id"] = None
                    return {
                        "success": False,
                        "message": f"The audio device '{device_name}' in room '{room.name}' is currently busy (playing). Ask the user if they want to interrupt the current playback.",
                        "action_taken": False,
                        "data": data,
                    }

                if not decision.output_device:
                    return {
                        "success": False,
                        "message": f"No audio output device available for room '{room.name}'",
                        "action_taken": False,
                    }

                # Generic output provider (samsung, sonos, …) — registry-driven
                # dispatch. Gated on the flag; dlna/HA/renfield keep their own
                # branches below. Surfaces the (target_type, output_target_id)
                # pair for _play_in_room's registry path.
                from utils.config import settings as _root_settings
                if (
                    _root_settings.output_providers_enabled
                    and decision.target_type not in ("renfield", "homeassistant", "dlna")
                ):
                    return {
                        "success": True,
                        "message": f"Found {decision.target_type} output for {room.name}: {decision.target_id}",
                        "action_taken": True,
                        "data": {
                            "target_type": decision.target_type,
                            "output_target_id": decision.target_id,
                            "room_name": room.name,
                            "device_name": decision.output_device.device_name or decision.target_id,
                        },
                    }

                # DLNA renderer — return target_type + renderer name
                if decision.target_type == "dlna":
                    return {
                        "success": True,
                        "message": f"Found DLNA renderer for {room.name}: {decision.output_device.dlna_renderer_name}",
                        "action_taken": True,
                        "data": {
                            "target_type": "dlna",
                            "dlna_renderer_name": decision.output_device.dlna_renderer_name,
                            "room_name": room.name,
                            "device_name": decision.output_device.device_name or decision.output_device.dlna_renderer_name,
                        },
                    }

                # We need an HA entity for media playback
                entity_id = decision.output_device.ha_entity_id
                if not entity_id:
                    return {
                        "success": False,
                        "message": f"Room '{room.name}' has no Home Assistant media player configured",
                        "action_taken": False,
                    }

                return {
                    "success": True,
                    "message": f"Found media player for {room.name}: {entity_id}",
                    "action_taken": True,
                    "data": {
                        "entity_id": entity_id,
                        "target_type": "homeassistant",
                        "room_name": room.name,
                        "device_name": decision.output_device.device_name or entity_id,
                    },
                }

        except Exception as e:
            logger.error(f"Error resolving room player for '{room_name}': {e}")
            return {
                "success": False,
                "message": f"Error resolving room: {e!s}",
                "action_taken": False,
            }

    async def _get_room_id(self, room_name: str) -> int | None:
        """Resolve room_name → room_id via RoomService."""
        try:
            from services.database import AsyncSessionLocal
            from ha_glue.services.room_service import RoomService

            async with AsyncSessionLocal() as db:
                rs = RoomService(db)
                room = await rs.get_room_by_name(room_name)
                if not room:
                    room = await rs.get_room_by_alias(room_name)
                return room.id if room else None
        except Exception:
            return None

    async def _announce_in_room(self, params: dict) -> dict:
        """Speak a text message (TTS) on a room's audio device.

        Privacy gate (FAIL-CLOSED): a message marked ``privacy="personal"`` is
        spoken aloud ONLY if the room is private (the target is alone). If other
        people are present it is NOT announced — we return a ``blocked`` status so
        the agent can relay that back instead of broadcasting confidential content
        to everyone in the room. ``privacy="public"`` always announces.

        This is a single primitive (synthesize → resolve room device → play). The
        person→room resolution + ordering is left to the agent (it calls
        internal.get_user_location first), so nothing about the relay flow is
        hardcoded here.
        """
        text = (params.get("text") or "").strip()
        room_name = (params.get("room_name") or "").strip()
        privacy = (params.get("privacy") or "public").strip().lower()
        force = str(params.get("force", "false")).lower() in ("true", "1", "yes")

        # Intended recipients (a name or list of names). A personal message is
        # private-enough only if everyone present is one of these.
        _fu = params.get("for_users")
        if isinstance(_fu, str):
            for_users = [n.strip() for n in _fu.split(",") if n.strip()]
        elif isinstance(_fu, list):
            for_users = [str(n).strip() for n in _fu if str(n).strip()]
        else:
            for_users = []

        if not text:
            return {"success": False, "message": "Parameter 'text' is required", "action_taken": False}
        if not room_name:
            return {"success": False, "message": "Parameter 'room_name' is required", "action_taken": False}

        try:
            import base64

            from services.database import AsyncSessionLocal
            from services.piper_service import PiperService
            from ha_glue.services.audio_output_service import get_audio_output_service
            from ha_glue.services.device_manager import get_device_manager
            from ha_glue.services.output_routing_service import OutputRoutingService
            from ha_glue.services.presence_service import get_presence_service
            from ha_glue.services.room_service import RoomService

            async with AsyncSessionLocal() as db:
                room_service = RoomService(db)
                room = await room_service.get_room_by_name(room_name) \
                    or await room_service.get_room_by_alias(room_name)
                if not room:
                    return {"success": False, "message": f"Room '{room_name}' not found", "action_taken": False}

                # --- Privacy gate (FAIL-CLOSED) ---
                # A personal message is spoken aloud ONLY with POSITIVE proof the
                # room is private: we know the recipients (allowed_ids), at least
                # one person is tracked in the room, and EVERY tracked person is a
                # recipient. Anything else (no/unresolvable recipients, nobody
                # tracked, or a non-recipient present) blocks — we never fall back
                # to a weaker "probably alone" guess. force=true bypasses (the
                # recipient consented after being told a message is waiting).
                # INHERENT LIMIT: presence only sees people with a tracked BLE
                # device, so this can't detect an untracked bystander — it is
                # best-effort, not a guarantee against every eavesdropper.
                if privacy != "public" and not force:
                    presence = get_presence_service()
                    occupants = presence.get_room_occupants(room.id)
                    allowed_ids = {
                        uid for n in for_users
                        if (uid := presence.find_user_by_name(n)) is not None
                    }
                    everyone_present_is_recipient = (
                        bool(allowed_ids)
                        and bool(occupants)
                        and all(o.user_id in allowed_ids for o in occupants)
                    )
                    if not everyone_present_is_recipient:
                        if not allowed_ids:
                            why = "die Empfaenger sind nicht bekannt (gib sie in for_users an)"
                        elif not occupants:
                            why = f"in {room.name} ist niemand (mit getracktem Geraet) erkennbar anwesend"
                        else:
                            non_rec = sum(1 for o in occupants if o.user_id not in allowed_ids)
                            why = f"in {room.name} ist/sind {non_rec} Nicht-Empfaenger-Person(en) anwesend"
                        return {
                            "success": False,
                            "action_taken": False,
                            "blocked": "not_private",
                            "message": (
                                f"Persönliche Nachricht NICHT laut angesagt ({why}). Gib NICHT den "
                                f"Inhalt preis. Sage NUR neutral an, dass eine Nachricht wartet (OHNE "
                                f"Inhalt), und frage, ob trotzdem vorgelesen werden soll. Bei "
                                f"ausdrücklicher Zustimmung des Empfängers rufe announce_in_room erneut "
                                f"mit force=true auf."
                            ),
                            "data": {
                                "room_name": room.name,
                                "occupants": len(occupants),
                                "recipients_present": len(allowed_ids & {o.user_id for o in occupants}),
                            },
                        }

                    # BLE gate passed. BLE only sees people with a tracked device —
                    # so, if a camera is in the room, take a snapshot and count
                    # people via the vision model to catch an UNTRACKED bystander.
                    # (Snapshot is transient — never stored.) Fail policy on a
                    # missing camera / failed snapshot or vision is configurable.
                    from ha_glue.utils.config import ha_glue_settings as _ha
                    if _ha.announce_camera_occupancy_check:
                        from ha_glue.services.satellite_manager import get_satellite_manager
                        sat_mgr = get_satellite_manager()
                        cam_sat = sat_mgr.get_camera_satellite_for_room(room.id)
                        if cam_sat is not None:
                            img = await sat_mgr.request_snapshot(
                                cam_sat.satellite_id, timeout=_ha.announce_snapshot_timeout
                            )
                            people = None
                            if img:
                                try:
                                    from main import app as _app
                                    _ollama = getattr(_app.state, "ollama", None)
                                    if _ollama is not None:
                                        # Bound the vision inference so a slow model
                                        # can't hang the announce indefinitely; a
                                        # timeout falls into the fail policy below.
                                        people = await asyncio.wait_for(
                                            _ollama.count_people_in_image(img),
                                            timeout=_ha.announce_snapshot_timeout,
                                        )
                                except Exception:  # noqa: BLE001 (incl. TimeoutError)
                                    people = None
                            if people is not None and people > len(occupants):
                                return {
                                    "success": False, "action_taken": False,
                                    "blocked": "not_private",
                                    "message": (
                                        f"Persönliche Nachricht NICHT laut angesagt — die Kamera in "
                                        f"{room.name} sieht {people} Person(en), mehr als die "
                                        f"{len(occupants)} bekannten Empfänger; es ist also jemand "
                                        f"Unbekanntes im Raum. Sage NUR neutral an, dass eine Nachricht "
                                        f"wartet (OHNE Inhalt); bei Zustimmung des Empfängers erneut "
                                        f"mit force=true."
                                    ),
                                    "data": {
                                        "room_name": room.name,
                                        "people_seen": people,
                                        "tracked_recipients": len(occupants),
                                    },
                                }
                            if people is None and _ha.announce_camera_check_fail_closed:
                                return {
                                    "success": False, "action_taken": False,
                                    "blocked": "not_private",
                                    "message": (
                                        f"Persönliche Nachricht NICHT laut angesagt — der Kamera-Check in "
                                        f"{room.name} war nicht möglich (kein Bild/keine Auswertung) und "
                                        f"die Policy ist fail-closed. Sage neutral an, dass eine Nachricht "
                                        f"wartet; bei Zustimmung erneut mit force=true."
                                    ),
                                    "data": {"room_name": room.name, "camera_check": "failed"},
                                }
                            # else: fail-open — the BLE decision stands, announce.

                tts_audio = await PiperService().synthesize_to_bytes(text)
                if not tts_audio:
                    return {"success": False, "message": "TTS synthesis failed", "action_taken": False}

                session_id = f"announce-{room.id}"
                decision = await OutputRoutingService(db).get_audio_output_for_room(room.id)
                if decision.output_device and not decision.fallback_to_input:
                    ok = await get_audio_output_service().play_audio(
                        audio_bytes=tts_audio,
                        output_device=decision.output_device,
                        session_id=session_id,
                    )
                    if ok:
                        return {
                            "success": True, "action_taken": True,
                            "message": f"Nachricht in {room.name} angesagt",
                            "data": {"room_name": room.name, "text": text, "privacy": privacy},
                        }

            # Fallback: send TTS to every speaker in the room directly.
            device_manager = get_device_manager()
            audio_b64 = base64.b64encode(tts_audio).decode("utf-8")
            for device in device_manager.get_devices_in_room(room_name):
                if device.capabilities.has_speaker:
                    try:
                        await device.websocket.send_json({
                            "type": "tts_audio",
                            "session_id": f"announce-{room_name}",
                            "audio": audio_b64,
                            "is_final": True,
                        })
                        return {
                            "success": True, "action_taken": True,
                            "message": f"Nachricht in {room_name} angesagt",
                            "data": {"room_name": room_name, "text": text, "privacy": privacy},
                        }
                    except Exception:  # noqa: BLE001
                        continue
            return {"success": False, "message": f"Kein Lautsprecher in {room_name} verfügbar", "action_taken": False}

        except Exception as e:  # noqa: BLE001
            logger.error(f"Error announcing in room '{room_name}': {e}")
            return {"success": False, "message": f"Error announcing: {e!s}", "action_taken": False}

    def _presence_room_user(self, room_id: int) -> int | None:
        """The single user presence currently places in ``room_id``, else None.

        Used to attribute a media session when there is no authenticated chat
        user (AUTH disabled / single-user mode) — Media Follow Me keys sessions
        on user_id and presence_leave_room fires with the presence-resolved
        user_id, so the playback side must agree. Returns None when the room has
        zero or ambiguous (>1) occupants, so we never attribute (and later stop)
        the wrong person's music.
        """
        try:
            from ha_glue.services.presence_service import get_presence_service

            occupants = get_presence_service().get_room_occupants(room_id)
            if len(occupants) == 1:
                return occupants[0].user_id
            return None
        except Exception:
            return None

    async def _register_media_follow(
        self, params: dict, room_name: str, media_type, **kwargs
    ) -> None:
        """Register playback with MediaFollowService if enabled (async version)."""
        from ha_glue.utils.config import ha_glue_settings as _settings

        if not _settings.media_follow_enabled:
            return
        try:
            rid = await self._get_room_id(room_name)
            if rid is None:
                return
            # Session owner: in practice always the presence-resolved occupant of
            # the playback room. `params` never carries a user_id today (the
            # dispatcher passes it as a kwarg, not in params, and the LLM tool
            # schema has no user_id arg), so the `or` first operand is currently
            # inert — kept only for a future authenticated-caller path. Without
            # the presence fallback, AUTH-disabled playback has user_id=None → no
            # session registered → leaving never stops the music, even though
            # presence_leave_room fires with that same presence-resolved user.
            # Caveat: presence is BLE-cadence (~30-60s), so "play then walk away
            # immediately" may register no session if the scan hasn't yet placed
            # the user in the room; >1 occupants → None (no follow, by design).
            user_id = params.get("user_id") or self._presence_room_user(rid)
            if not user_id:
                return

            from ha_glue.services.media_follow_service import MediaType, get_media_follow_service

            mf = get_media_follow_service()
            # Convert string to enum if needed
            if isinstance(media_type, str):
                media_type = MediaType(media_type)
            mf.register_playback(
                user_id=int(user_id),
                room_id=rid,
                room_name=room_name,
                media_type=media_type,
                **kwargs,
            )
        except Exception as e:
            logger.debug(f"Media follow registration failed: {e}")

    async def _play_in_room(self, params: dict) -> dict:
        """
        Play a media URL on the audio device in a room.

        1. Resolve room → entity_id (via _resolve_room_player)
        2. Call HA REST API: media_player.play_media
        """
        media_url = params.get("media_url", "").strip()
        room_name = params.get("room_name", "").strip()
        media_type = params.get("media_type", "music").strip()
        force = str(params.get("force", "false")).lower() in ("true", "1", "yes")
        title = (params.get("title") or "").strip() or None
        thumb = (params.get("thumb") or "").strip() or None

        # Pass media_type directly as HA media_content_type.
        ha_content_type = media_type

        if not media_url:
            return {
                "success": False,
                "message": "Parameter 'media_url' is required",
                "action_taken": False,
            }
        if not room_name:
            return {
                "success": False,
                "message": "Parameter 'room_name' is required",
                "action_taken": False,
            }

        # Step 1: Resolve room to entity_id
        resolve_result = await self._resolve_room_player({"room_name": room_name})

        if not resolve_result.get("success"):
            # If device is busy and force is set, use the entity from the
            # busy-device data to proceed anyway.
            if force and resolve_result.get("data", {}).get("status") == "busy":
                entity_id = resolve_result["data"].get("entity_id")
                if not entity_id:
                    return resolve_result
                resolve_result = {
                    "success": True,
                    "data": resolve_result["data"],
                }
                logger.info(f"Force-playing on busy device {entity_id} in {room_name}")
            else:
                return resolve_result

        data = resolve_result["data"]

        # Generic output provider (samsung, sonos, …): route through the registry
        # adapter with bounded power-on. Gated on the flag; the resolver only
        # returns these target_types when output_providers_enabled is on.
        from utils.config import settings as _root_settings
        if _root_settings.output_providers_enabled:
            provider = self._get_output_provider(data.get("target_type"))
            if provider is not None:
                return await self._play_via_provider(
                    provider, data, media_url=media_url, title=title,
                    room_name=room_name, params=params,
                )

        # DLNA renderers have no HA entity_id and must be driven through the
        # DLNA MCP path, not media_player.play_media. Without this branch the
        # entity_id lookup below KeyErrors on every DLNA room (the agent's
        # default playback tool was unusable for DLNA-only rooms).
        if data.get("target_type") == "dlna":
            return await self._play_url_on_dlna(
                renderer_name=data.get("dlna_renderer_name"),
                media_url=media_url,
                title=title,
                thumb=thumb,
                room_name=data.get("room_name", room_name),
                device_name=data.get("device_name"),
                params=params,
            )

        entity_id = data["entity_id"]
        resolved_room_name = data["room_name"]
        device_name = data["device_name"]

        # Step 2: Call HA media_player.play_media
        try:
            import asyncio as _asyncio

            from ha_glue.integrations.homeassistant import HomeAssistantClient

            ha_client = HomeAssistantClient()

            # Build service_data with optional metadata for HA media player UI.
            service_data = {
                "media_content_id": media_url,
                "media_content_type": ha_content_type,
            }
            if title or thumb:
                extra = {}
                if title:
                    extra["title"] = title
                if thumb:
                    extra["thumb"] = thumb
                service_data["extra"] = extra

            # Fire the play_media command.  Some HA integrations (HomePod,
            # Apple TV) return HTTP 500 or timeout even though the action
            # succeeds.  We therefore ignore the call_service result and
            # verify playback by checking the player state afterwards.
            try:
                await ha_client.call_service(
                    domain="media_player",
                    service="play_media",
                    entity_id=entity_id,
                    service_data=service_data,
                    timeout=15.0,
                )
            except Exception as exc:
                # Timeout or HTTP error — command may still have been
                # dispatched.  Log and continue to state check.
                logger.info(f"HA play_media raised {type(exc).__name__} for {entity_id} — checking player state")

            # Give the player time to start — AirPlay/HomePod needs
            # up to ~6s to set up the RTSP stream from a network URL.
            await _asyncio.sleep(6)
            state = await ha_client.get_state(entity_id)
            player_state = (state or {}).get("state", "unknown")

            if player_state in ("playing", "buffering", "paused"):
                await self._register_media_follow(
                    params, resolved_room_name, "single_url",
                    media_url=media_url, title=title, thumb=thumb,
                )
                return {
                    "success": True,
                    "message": f"Playing on {device_name} in {resolved_room_name}",
                    "action_taken": True,
                    "data": {
                        "entity_id": entity_id,
                        "room_name": resolved_room_name,
                        "device_name": device_name,
                        "media_url": media_url,
                        "media_type": media_type,
                    },
                }

            # --- Transcode fallback for incompatible audio formats ---
            # If the player stayed idle and the URL is a Jellyfin static stream,
            # retry once with server-side transcoding to MP3 (AirPlay-compatible).
            if player_state == "idle" and "static=true" in media_url:
                transcode_url = media_url.replace(
                    "static=true",
                    "audioCodec=mp3&audioBitRate=320000",
                )
                logger.info(
                    f"Playback idle with static URL — retrying with transcode: {entity_id}"
                )
                transcode_service_data = {
                    "media_content_id": transcode_url,
                    "media_content_type": ha_content_type,
                }
                transcode_extra = {}
                if title:
                    transcode_extra["title"] = title
                if thumb:
                    transcode_extra["thumb"] = thumb
                if transcode_extra:
                    transcode_service_data["extra"] = transcode_extra

                try:
                    await ha_client.call_service(
                        domain="media_player",
                        service="play_media",
                        entity_id=entity_id,
                        service_data=transcode_service_data,
                        timeout=15.0,
                    )
                except Exception:
                    pass  # Check state regardless

                await _asyncio.sleep(8)
                state = await ha_client.get_state(entity_id)
                player_state = (state or {}).get("state", "unknown")

                if player_state in ("playing", "buffering", "paused"):
                    await self._register_media_follow(
                        params, resolved_room_name, "single_url",
                        media_url=transcode_url, title=title, thumb=thumb,
                    )
                    return {
                        "success": True,
                        "message": f"Playing (transcoded) on {device_name} in {resolved_room_name}",
                        "action_taken": True,
                        "data": {
                            "entity_id": entity_id,
                            "room_name": resolved_room_name,
                            "device_name": device_name,
                            "media_url": transcode_url,
                            "media_type": media_type,
                        },
                    }

            return {
                "success": False,
                "message": f"Playback failed — player state is '{player_state}'",
                "action_taken": False,
            }

        except Exception as e:
            logger.error(f"Error playing media in '{room_name}': {e}")
            return {
                "success": False,
                "message": f"Error playing media: {e!s}",
                "action_taken": False,
            }

    # --- Generic output-provider dispatch (output_providers_enabled) ---------

    def _get_output_provider(self, target_type: str | None):
        """Look up a registry McpOutputProvider for a target_type (None if absent
        or the registry can't be built). Built fresh per call from the live
        MCPManager so a re-registered server is picked up without restart."""
        if not target_type:
            return None
        try:
            from main import app
            from ha_glue.services.output_providers import build_mcp_output_providers
            mcp_manager = getattr(app.state, "mcp_manager", None)
            if not mcp_manager:
                return None
            return build_mcp_output_providers(mcp_manager).get(target_type)
        except Exception as e:  # never let provider lookup break playback
            logger.error(f"output provider lookup failed for '{target_type}': {e}")
            return None

    # Explicitly-not-ready states. Drives the power-on trigger AND the readiness
    # poll symmetrically. Deliberately does NOT include "unknown": a provider whose
    # status tool has no state field (e.g. samsung tv_info) reports "unknown" while
    # fully awake — treating that as not-ready would trap an on TV in a never-wake
    # loop. So "responded with anything but off/standby" == ready. The harder case
    # (a standby device that returns a success envelope with a non-off state) is
    # device-specific and needs real-hardware tuning of the stanza's status mapping
    # — tracked for validation before the flag is enabled in prod.
    _NOT_READY_STATES = frozenset({"off", "standby"})

    async def _poll_provider_ready(self, provider, target_id: str) -> bool:
        """Poll status() until the target reports a ready state, bounded by the
        provider's boot_timeout. Returns True when ready, False on timeout.
        Checks BEFORE sleeping (an already-awake device returns immediately);
        iteration-bounded (no wall-clock dependency) so it is test-friendly."""
        import asyncio as _asyncio

        interval = 2.0
        max_polls = max(1, int((provider.boot_timeout or interval) / interval))
        for i in range(max_polls):
            try:
                st = await provider.status(target_id)
                if st.state not in self._NOT_READY_STATES:
                    return True
            except Exception:  # transient during boot — keep polling
                pass
            if i < max_polls - 1:
                await _asyncio.sleep(interval)
        return False

    async def _play_via_provider(
        self, provider, data: dict, *, media_url: str, title: str | None,
        room_name: str, params: dict,
    ) -> dict:
        """Play through a generic output provider: power-on (bounded poll) if the
        device is off and the provider can power, then play. Honest errors — a TV
        that won't wake returns failure, never a fake success."""
        from ha_glue.services.output_providers import MediaRef, OutputProviderError

        target_id = data.get("output_target_id") or data.get("target_id") or ""
        device_name = data.get("device_name") or target_id
        resolved_room = data.get("room_name", room_name)

        # Power-on before play if supported and the device is off OR unreachable.
        # A TV in standby typically can't be probed at all (status() raises), so a
        # failed status is itself the strongest "needs waking" signal — plus an
        # explicit off/standby state. WoL is idempotent, so over-triggering on an
        # already-awake device is harmless (the readiness poll returns immediately).
        if provider.has_capability("power"):
            try:
                st = await provider.status(target_id)
            except OutputProviderError as e:
                logger.info(f"{provider.key} pre-play status failed ({e}); treating as off → power-on")
                st = None
            if st is None or st.state in self._NOT_READY_STATES:
                try:
                    await provider.control(target_id, "on")
                except OutputProviderError as e:
                    return {
                        "success": False,
                        "message": f"Could not power on {device_name}: {e}",
                        "action_taken": False,
                    }
                if not await self._poll_provider_ready(provider, target_id):
                    return {
                        "success": False,
                        "message": (
                            f"Could not wake {device_name} in {resolved_room} within "
                            f"{int(provider.boot_timeout)}s — it may be unplugged or "
                            f"Wake-on-LAN is off."
                        ),
                        "action_taken": False,
                    }

        try:
            res = await provider.play(
                target_id, [MediaRef(url=media_url, title=title)], mode="now"
            )
        except OutputProviderError as e:
            return {
                "success": False,
                "message": f"Playback failed on {device_name}: {e}",
                "action_taken": False,
            }
        if not res.ok:
            detail = res.message or res.state or "unknown error"
            return {
                "success": False,
                "message": f"Playback failed on {device_name}: {detail}",
                "action_taken": False,
            }

        # NOTE: media-follow is intentionally NOT registered for generic providers
        # in v1. The follow engine's SINGLE_URL replay strategy re-dispatches via
        # the HA media_player path, which would mis-route a samsung-originated
        # stream on a room change. Provider-aware media-follow is a follow-up.
        return {
            "success": True,
            "message": f"Playing on {device_name} in {resolved_room}",
            "action_taken": True,
            "data": {
                "target_type": provider.key,
                "output_target_id": target_id,
                "room_name": resolved_room,
                "device_name": device_name,
                "media_url": media_url,
            },
        }

    async def _play_url_on_dlna(
        self,
        *,
        renderer_name: str | None,
        media_url: str,
        title: str | None,
        thumb: str | None,
        room_name: str,
        device_name: str | None,
        params: dict,
    ) -> dict:
        """Play a single already-resolved media URL on a DLNA renderer.

        The DLNA counterpart of the HA `media_player.play_media` path in
        `_play_in_room`: a room that resolves to a DLNA renderer is played via
        `mcp.dlna.play_tracks` (a one-item queue), mirroring how
        `_play_album_on_dlna` sends tracks. Avoids the entity_id assumption that
        made `_play_in_room` crash on DLNA rooms.
        """
        if not renderer_name:
            return {
                "success": False,
                "message": f"No DLNA renderer name for room '{room_name}'",
                "action_taken": False,
            }
        try:
            import json as _json

            from main import app

            mcp_manager = getattr(app.state, "mcp_manager", None)
            if not mcp_manager:
                return {
                    "success": False,
                    "message": "MCP manager not available",
                    "action_taken": False,
                }

            track = {"url": media_url, "title": title or "", "artist": "", "album": ""}
            if thumb:
                # play_tracks renders cover art from `art_url` (same field the
                # video path uses); forward it so DLNA single-URL playback shows
                # the thumbnail instead of a blank tile.
                track["art_url"] = thumb
            result = await mcp_manager.execute_tool(
                "mcp.dlna.play_tracks",
                {"renderer_name": renderer_name, "tracks": _json.dumps([track])},
            )
            if not result.get("success"):
                return {
                    "success": False,
                    "message": f"DLNA playback failed: {result.get('message', 'unknown error')}",
                    "action_taken": False,
                }

            await self._register_media_follow(
                params, room_name, "single_url",
                media_url=media_url, title=title, thumb=thumb,
            )
            return {
                "success": True,
                "message": f"Playing on {device_name or renderer_name} in {room_name} (DLNA: {renderer_name})",
                "action_taken": True,
                "data": {
                    "renderer_name": renderer_name,
                    "room_name": room_name,
                    "device_name": device_name,
                    "media_url": media_url,
                    "target_type": "dlna",
                },
            }
        except Exception as e:
            logger.error(f"Error playing URL on DLNA renderer '{renderer_name}': {e}")
            return {
                "success": False,
                "message": f"Error playing on DLNA: {e!s}",
                "action_taken": False,
            }

    _MEDIA_ACTION_MAP = {
        "stop": "media_stop",
        "pause": "media_pause",
        "resume": "media_play",
        "next": "media_next_track",
        "previous": "media_previous_track",
    }

    _DLNA_ACTION_MAP = {
        "stop": "mcp.dlna.stop",
        "pause": "mcp.dlna.pause",
        "resume": "mcp.dlna.resume",
        "next": "mcp.dlna.next_track",
        "previous": "mcp.dlna.previous_track",
    }

    @staticmethod
    def _resolve_target_volume(params: dict, current_pct: int | None) -> tuple[int | None, dict | None]:
        """Compute absolute target volume 0-100 from params. Returns (target, error).

        Exactly one of (target, error) is non-None.
        - Rejects passing BOTH 'volume' and 'volume_step'.
        - 'volume' (absolute): clamp 0-100.
        - 'volume_step' (relative, PERCENTAGE POINTS): requires current_pct;
          if current_pct is None -> clear error (can't read current volume).
          target = clamp(current_pct + step, 0, 100).
        """
        has_step = params.get("volume_step") is not None
        raw_abs = params.get("volume")
        has_abs = raw_abs is not None

        def _err(message: str) -> tuple[None, dict]:
            return None, {
                "success": False,
                "message": message,
                "action_taken": False,
            }

        if has_step and has_abs:
            return _err("Pass either 'volume' (absolute 0-100) or 'volume_step' (relative), not both")
        if not has_step and not has_abs:
            return _err("Parameter 'volume' or 'volume_step' is required for volume action")

        if has_abs:
            try:
                v = int(raw_abs)
            except (ValueError, TypeError):
                return _err(f"Invalid volume value: {raw_abs}")
            return max(0, min(100, v)), None

        # Relative (volume_step, percentage points)
        try:
            step = int(params.get("volume_step"))
        except (ValueError, TypeError):
            return _err(f"Invalid volume_step value: {params.get('volume_step')}")
        if current_pct is None:
            return _err(
                "Could not read the current volume for this device, so I can't change it "
                "relatively — please tell me an absolute level (0-100)."
            )
        return max(0, min(100, current_pct + step)), None

    @staticmethod
    def _extract_mcp_json(res: dict) -> dict:
        """Parse the JSON payload dict out of an execute_tool result.

        execute_tool returns `data` as a LIST of content blocks
        (`[{"type":"text","text":"<json>"}]`), NOT the deserialized object — so
        the real payload must be parsed from the text block (falling back to
        `message`). Tolerates a flat dict `data` too (tests / direct callers).
        Returns {} when nothing parses.
        """
        import json as _json

        data = res.get("data")
        if isinstance(data, dict):
            return data
        raw_text = ""
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("type") == "text":
                    raw_text = item.get("text", "")
                    break
        if not raw_text:
            raw_text = res.get("message", "") or ""
        try:
            parsed = _json.loads(raw_text)
        except (ValueError, TypeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _extract_dlna_volume(vol_res: dict) -> int | None:
        """Pull a 0-100 volume int out of an mcp.dlna.get_volume result.

        The MCP wrapper (MCPManager.execute_tool) may surface the value either
        flat on the result (`vol_res["volume"]`) or nested inside the `data`
        content blocks / `message` as JSON (see the project memory note that the
        wrapper can nest the real payload — same shape `_play_album_on_dlna`
        parses for Jellyfin track lists). Returns None when the renderer can't
        report a volume (value missing / None / unparseable).
        """
        # 1) Flat value on the result.
        v = vol_res.get("volume")
        # 2) Nested JSON payload (data content blocks, else message string).
        if v is None:
            import json as _json

            raw_text = ""
            data = vol_res.get("data", [])
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and item.get("type") == "text":
                        raw_text = item.get("text", "")
                        break
            if not raw_text:
                raw_text = vol_res.get("message", "")
            try:
                parsed = _json.loads(raw_text)
            except (ValueError, TypeError):
                parsed = {}
            if isinstance(parsed, dict):
                v = parsed.get("volume")
        if v is None:
            return None
        try:
            return int(v)
        except (ValueError, TypeError):
            return None

    async def _media_control(self, params: dict) -> dict:
        """
        Control media playback in a room (stop, pause, resume, next, previous, volume).

        Supports both HA media players and DLNA renderers — branches by target_type.
        """
        action = (params.get("action") or "").strip().lower()
        room_name = (params.get("room_name") or "").strip()

        if not action:
            return {
                "success": False,
                "message": "Parameter 'action' is required",
                "action_taken": False,
            }

        valid_actions = set(self._MEDIA_ACTION_MAP) | {"volume", "mute", "unmute", "status", "seek", "play_mode"}
        if action not in valid_actions:
            return {
                "success": False,
                "message": f"Invalid action '{action}'. Must be one of: {', '.join(sorted(valid_actions))}",
                "action_taken": False,
            }

        if not room_name:
            return {
                "success": False,
                "message": "Parameter 'room_name' is required",
                "action_taken": False,
            }

        # Resolve room → device.  For media control we *want* to target a
        # busy device (it's playing and we want to stop/pause/skip it).
        resolve_result = await self._resolve_room_player({"room_name": room_name})

        if resolve_result.get("success"):
            device_data = resolve_result["data"]
            resolved_room_name = device_data["room_name"]
        elif resolve_result.get("data", {}).get("status") == "busy":
            device_data = resolve_result.get("data", {})
            resolved_room_name = device_data.get("room_name", room_name)
        else:
            return resolve_result

        target_type = device_data.get("target_type", "homeassistant")

        # Generic output provider (samsung, sonos, …) routes through the registry
        # when the flag is on; dlna/HA/renfield keep their legacy handlers. Flag
        # off => provider is None => byte-identical legacy branching.
        from utils.config import settings as _root_settings
        provider = (
            self._get_output_provider(target_type)
            if _root_settings.output_providers_enabled else None
        )
        if provider is not None:
            result = await self._media_control_via_provider(
                action, device_data, resolved_room_name, params, provider
            )
        elif target_type == "dlna":
            result = await self._media_control_dlna(action, device_data, resolved_room_name, params)
        else:
            result = await self._media_control_ha(action, device_data, resolved_room_name, params)

        # Clear media follow session on explicit USER stop — but NOT when the stop
        # is Media Follow's own suspend (_stop_playback sets _media_follow_internal),
        # which would delete the session we're about to resume in the next room.
        if action == "stop" and result.get("success") and not params.get("_media_follow_internal"):
            from ha_glue.utils.config import ha_glue_settings as _settings
            if _settings.media_follow_enabled:
                try:
                    from ha_glue.services.media_follow_service import get_media_follow_service
                    mf = get_media_follow_service()
                    room_id = await self._get_room_id(resolved_room_name)
                    if room_id is not None:
                        mf.clear_session_by_room(room_id)
                except Exception:
                    pass

        return result

    async def _media_control_via_provider(
        self, action: str, device_data: dict, room_name: str, params: dict, provider
    ) -> dict:
        """Execute a media-control action through a generic output provider.

        Translates the internal.media_control vocabulary onto the provider's
        contract: `status` → provider.status(); `volume` → control('volume', value);
        everything else → control(action). Actions the provider's stanza doesn't
        map (e.g. next/seek/play_mode on a single-item TV) raise and surface a
        graceful 'not supported' — never a misroute.
        """
        from ha_glue.services.output_providers import OutputProviderError

        target_id = device_data.get("output_target_id") or device_data.get("target_id") or ""
        device_name = device_data.get("device_name") or target_id
        try:
            if action == "status":
                st = await provider.status(target_id)
                return {
                    "success": True,
                    "message": f"{device_name} in {room_name}: {st.state}",
                    "action_taken": False,
                    "data": {
                        "target_type": provider.key,
                        "output_target_id": target_id,
                        "room_name": room_name,
                        "state": st.state,
                        "position": st.position,
                    },
                }
            if action == "volume":
                # The contract carries absolute volume only. Relative (volume_step)
                # would need a read-modify-write; defer it with a clear message.
                vol = params.get("volume")
                if vol is None:
                    return {
                        "success": False,
                        "message": (
                            f"Relative volume isn't supported for {device_name} yet — "
                            f"give an absolute volume (0-100)."
                        ),
                        "action_taken": False,
                    }
                res = await provider.control(target_id, "volume", value=int(vol))
            else:
                res = await provider.control(target_id, action)
        except OutputProviderError as e:
            return {
                "success": False,
                "message": f"'{action}' isn't supported on {device_name}: {e}",
                "action_taken": False,
            }

        if not getattr(res, "ok", True):
            return {
                "success": False,
                "message": f"{action} failed on {device_name}: {res.message}",
                "action_taken": False,
            }
        return {
            "success": True,
            "message": f"{action} on {device_name} in {room_name}",
            "action_taken": True,
            "data": {
                "target_type": provider.key,
                "output_target_id": target_id,
                "room_name": room_name,
                "action": action,
            },
        }

    async def _media_control_dlna(self, action: str, device_data: dict, room_name: str, params: dict) -> dict:
        """Execute media control action on a DLNA renderer via MCP."""
        renderer_name = device_data.get("dlna_renderer_name")
        if not renderer_name:
            return {
                "success": False,
                "message": f"No DLNA renderer name for room '{room_name}'",
                "action_taken": False,
            }

        try:
            from main import app
            mcp_manager = getattr(app.state, "mcp_manager", None)
            if not mcp_manager:
                return {
                    "success": False,
                    "message": "MCP manager not available",
                    "action_taken": False,
                }

            if action == "status":
                # Read-only: what's playing on this renderer (track/state/queue).
                status_res = await mcp_manager.execute_tool(
                    "mcp.dlna.get_status", {"renderer_name": renderer_name}
                )
                if not status_res.get("success"):
                    return {
                        "success": False,
                        "message": f"Could not get status for {room_name}: "
                                   f"{status_res.get('message', 'unknown error')}",
                        "action_taken": False,
                    }
                # execute_tool nests the real payload in content blocks — parse it.
                return {
                    "success": True,
                    "message": f"Playback status for {room_name} (DLNA: {renderer_name})",
                    "action_taken": False,
                    "data": {
                        "room_name": room_name,
                        "renderer_name": renderer_name,
                        "target_type": "dlna",
                        "status": self._extract_mcp_json(status_res),
                    },
                }

            if action == "seek":
                pos = params.get("position_seconds")
                if pos is None:
                    return {"success": False, "message": "Parameter 'position_seconds' is required for seek", "action_taken": False}
                try:
                    pos = int(pos)
                except (ValueError, TypeError):
                    return {"success": False, "message": f"Invalid position_seconds: {params.get('position_seconds')}", "action_taken": False}
                res = await mcp_manager.execute_tool(
                    "mcp.dlna.seek", {"renderer_name": renderer_name, "position_seconds": max(0, pos)}
                )
                if not res.get("success"):
                    return {"success": False, "message": f"Seek failed on {room_name}: {res.get('message', 'unknown error')}", "action_taken": False}
                return {"success": True, "message": f"Seeked to {max(0, pos)}s on {room_name}", "action_taken": True,
                        "data": {"room_name": room_name, "renderer_name": renderer_name, "action": action, "position_seconds": max(0, pos)}}

            if action == "play_mode":
                mode = (params.get("mode") or "").strip()
                if not mode:
                    return {"success": False, "message": "Parameter 'mode' is required for play_mode (normal/repeat_one/repeat_all/shuffle/random)", "action_taken": False}
                res = await mcp_manager.execute_tool(
                    "mcp.dlna.set_play_mode", {"renderer_name": renderer_name, "mode": mode}
                )
                if not res.get("success"):
                    return {"success": False, "message": f"Play mode '{mode}' failed on {room_name}: {res.get('message', 'unknown error')}", "action_taken": False}
                return {"success": True, "message": f"Play mode set to {mode} on {room_name}", "action_taken": True,
                        "data": {"room_name": room_name, "renderer_name": renderer_name, "action": action, "mode": mode}}

            applied_volume = None
            if action == "volume":
                # Read current volume only for the relative path — a wasted MCP
                # round-trip on absolute sets is avoided.
                current_pct = None
                if params.get("volume_step") is not None and params.get("volume") is None:
                    vol_res = await mcp_manager.execute_tool(
                        "mcp.dlna.get_volume", {"renderer_name": renderer_name}
                    )
                    if vol_res.get("success"):
                        current_pct = self._extract_dlna_volume(vol_res)
                    # If the tool errored or isn't deployed yet, current_pct stays
                    # None -> _resolve_target_volume returns the D2 clear error.

                target, err = self._resolve_target_volume(params, current_pct)
                if err:
                    return err
                applied_volume = target
                tool_name = "mcp.dlna.set_volume"
                tool_params = {"renderer_name": renderer_name, "volume": target}
            elif action in ("mute", "unmute"):
                # Native RenderingControl SetMute — the renderer restores the
                # prior volume on unmute, so no level is stored.
                tool_name = "mcp.dlna.set_mute"
                tool_params = {"renderer_name": renderer_name, "mute": action == "mute"}
            else:
                tool_name = self._DLNA_ACTION_MAP.get(action)
                if not tool_name:
                    return {
                        "success": False,
                        "message": f"Action '{action}' not supported for DLNA",
                        "action_taken": False,
                    }
                tool_params = {"renderer_name": renderer_name}

            result = await mcp_manager.execute_tool(tool_name, tool_params)

            if not result.get("success"):
                return {
                    "success": False,
                    "message": f"DLNA {action} failed: {result.get('message', 'unknown error')}",
                    "action_taken": False,
                }

            data = {
                "renderer_name": renderer_name,
                "room_name": room_name,
                "action": action,
                "target_type": "dlna",
            }
            if action == "volume":
                # Echo the resulting level so the agent sees a concrete completed
                # state and gives final_answer instead of re-issuing the call
                # (which, for a relative volume_step, would re-apply the delta).
                data["volume"] = applied_volume
                message = f"Volume in {room_name} set to {applied_volume}%."
            elif action in ("mute", "unmute"):
                message = f"{room_name} {'muted' if action == 'mute' else 'unmuted'}."
            else:
                message = f"Media {action} executed on {room_name} (DLNA: {renderer_name})"
            return {
                "success": True,
                "message": message,
                "action_taken": True,
                "data": data,
            }

        except Exception as e:
            logger.error(f"Error executing DLNA media {action} in '{room_name}': {e}")
            return {
                "success": False,
                "message": f"Error executing media {action}: {e!s}",
                "action_taken": False,
            }

    async def _media_control_ha(self, action: str, device_data: dict, room_name: str, params: dict) -> dict:
        """Execute media control action on an HA media player."""
        entity_id = device_data.get("entity_id")
        if not entity_id:
            return {
                "success": False,
                "message": f"No Home Assistant entity_id for room '{room_name}'",
                "action_taken": False,
            }

        try:
            from ha_glue.integrations.homeassistant import HomeAssistantClient

            ha_client = HomeAssistantClient()

            if action == "status":
                # Read-only: what's playing on this HA media_player.
                state = await ha_client.get_state(entity_id)
                attrs = (state or {}).get("attributes", {})
                return {
                    "success": True,
                    "message": f"Playback status for {room_name}",
                    "action_taken": False,
                    "data": {
                        "room_name": room_name,
                        "entity_id": entity_id,
                        "target_type": "homeassistant",
                        "status": {
                            "state": (state or {}).get("state"),
                            "title": attrs.get("media_title"),
                            "artist": attrs.get("media_artist"),
                            "album": attrs.get("media_album_name"),
                        },
                    },
                }

            if action == "seek":
                pos = params.get("position_seconds")
                if pos is None:
                    return {"success": False, "message": "Parameter 'position_seconds' is required for seek", "action_taken": False}
                try:
                    pos = max(0, int(pos))
                except (ValueError, TypeError):
                    return {"success": False, "message": f"Invalid position_seconds: {params.get('position_seconds')}", "action_taken": False}
                await ha_client.call_service(
                    domain="media_player", service="media_seek", entity_id=entity_id,
                    service_data={"seek_position": pos},
                )
                return {"success": True, "message": f"Seeked to {pos}s on {room_name}", "action_taken": True,
                        "data": {"room_name": room_name, "entity_id": entity_id, "action": action, "position_seconds": pos}}

            if action == "play_mode":
                # HA exposes shuffle + repeat as SEPARATE services, not the single
                # UPnP play-mode enum. Map the common ones; reject the rest clearly.
                mode = (params.get("mode") or "").strip().lower()
                if not mode:
                    return {"success": False, "message": "Parameter 'mode' is required for play_mode (normal/repeat_one/repeat_all/shuffle)", "action_taken": False}
                if mode in ("shuffle", "random"):
                    await ha_client.call_service(domain="media_player", service="shuffle_set",
                                                 entity_id=entity_id, service_data={"shuffle": True})
                elif mode in ("repeat_one",):
                    await ha_client.call_service(domain="media_player", service="repeat_set",
                                                 entity_id=entity_id, service_data={"repeat": "one"})
                elif mode in ("repeat_all",):
                    await ha_client.call_service(domain="media_player", service="repeat_set",
                                                 entity_id=entity_id, service_data={"repeat": "all"})
                elif mode == "normal":
                    await ha_client.call_service(domain="media_player", service="shuffle_set",
                                                 entity_id=entity_id, service_data={"shuffle": False})
                    await ha_client.call_service(domain="media_player", service="repeat_set",
                                                 entity_id=entity_id, service_data={"repeat": "off"})
                else:
                    return {"success": False, "message": f"Unknown play mode '{mode}' (use normal/repeat_one/repeat_all/shuffle)", "action_taken": False}
                return {"success": True, "message": f"Play mode set to {mode} on {room_name}", "action_taken": True,
                        "data": {"room_name": room_name, "entity_id": entity_id, "action": action, "mode": mode}}

            applied_volume = None
            if action == "volume":
                # Read current volume only for the relative path — avoids a
                # wasted HTTP read on absolute sets.
                current_pct = None
                if params.get("volume_step") is not None and params.get("volume") is None:
                    state = await ha_client.get_state(entity_id)  # dict | None (swallows errors)
                    level = (state or {}).get("attributes", {}).get("volume_level")  # 0.0-1.0 or None
                    current_pct = round(level * 100) if level is not None else None

                target, err = self._resolve_target_volume(params, current_pct)
                if err:
                    return err
                applied_volume = target
                volume_level = max(0.0, min(1.0, target / 100.0))
                await ha_client.call_service(
                    domain="media_player",
                    service="volume_set",
                    entity_id=entity_id,
                    service_data={"volume_level": volume_level},
                )
            elif action in ("mute", "unmute"):
                # Native media_player.volume_mute — the player restores the prior
                # volume on unmute, so no level is stored.
                await ha_client.call_service(
                    domain="media_player",
                    service="volume_mute",
                    entity_id=entity_id,
                    service_data={"is_volume_muted": action == "mute"},
                )
            else:
                ha_service = self._MEDIA_ACTION_MAP[action]
                await ha_client.call_service(
                    domain="media_player",
                    service=ha_service,
                    entity_id=entity_id,
                )

            data = {
                "entity_id": entity_id,
                "room_name": room_name,
                "action": action,
            }
            if action == "volume":
                # Echo the resulting level so the agent sees a concrete completed
                # state and gives final_answer instead of re-issuing the call.
                data["volume"] = applied_volume
                message = f"Volume in {room_name} set to {applied_volume}%."
            elif action in ("mute", "unmute"):
                message = f"{room_name} {'muted' if action == 'mute' else 'unmuted'}."
            else:
                message = f"Media {action} executed on {room_name}"
            return {
                "success": True,
                "message": message,
                "action_taken": True,
                "data": data,
            }

        except Exception as e:
            logger.error(f"Error executing media {action} in '{room_name}': {e}")
            return {
                "success": False,
                "message": f"Error executing media {action}: {e!s}",
                "action_taken": False,
            }

    async def _play_album_on_dlna(self, params: dict) -> dict:
        """
        Play a Jellyfin album on a DLNA renderer.

        Combines get_album_tracks + dlna.play_tracks into one server-side step
        so the LLM doesn't have to generate the massive tracks JSON (which
        exceeds num_predict token limits for albums with many tracks).

        Accepts either renderer_name (direct) or room_name (resolved via room config).
        """
        album_id = (params.get("album_id") or "").strip()
        renderer_name = (params.get("renderer_name") or "").strip()
        room_name = (params.get("room_name") or "").strip()
        album_name_param = (params.get("album_name") or "").strip()

        if not album_id:
            return {
                "success": False,
                "message": "Parameter 'album_id' is required",
                "action_taken": False,
            }

        # Resolve renderer_name from room config if not provided directly
        if not renderer_name and room_name:
            resolve_result = await self._resolve_room_player({"room_name": room_name})
            if not resolve_result.get("success"):
                return resolve_result
            data = resolve_result.get("data", {})
            if data.get("target_type") != "dlna":
                return {
                    "success": False,
                    "message": f"Room '{room_name}' has no DLNA renderer configured (found {data.get('target_type', 'unknown')} device instead)",
                    "action_taken": False,
                }
            renderer_name = data.get("dlna_renderer_name", "")

        if not renderer_name:
            return {
                "success": False,
                "message": "Either 'renderer_name' or 'room_name' is required",
                "action_taken": False,
            }

        try:
            from main import app
            mcp_manager = getattr(app.state, "mcp_manager", None)
            if not mcp_manager:
                return {
                    "success": False,
                    "message": "MCP manager not available",
                    "action_taken": False,
                }

            # Step 1: Get album tracks from Jellyfin
            tracks_result = await mcp_manager.execute_tool(
                "mcp.jellyfin.get_album_tracks",
                {"album_id": album_id},
            )
            if not tracks_result.get("success"):
                return {
                    "success": False,
                    "message": f"Failed to get album tracks: {tracks_result.get('message', 'unknown error')}",
                    "action_taken": False,
                }

            # Parse tracks from the Jellyfin MCP response
            import json as _json
            tracks_data = tracks_result.get("data", [])
            # MCP returns data as list of content blocks
            raw_text = ""
            if isinstance(tracks_data, list):
                for item in tracks_data:
                    if isinstance(item, dict) and item.get("type") == "text":
                        raw_text = item.get("text", "")
                        break
            if not raw_text:
                raw_text = tracks_result.get("message", "")

            try:
                parsed = _json.loads(raw_text)
            except (ValueError, TypeError):
                parsed = {}

            jellyfin_tracks = parsed.get("items", parsed.get("tracks", []))
            if not jellyfin_tracks:
                return {
                    "success": False,
                    "message": "No tracks found in album",
                    "action_taken": False,
                }

            # Step 2: Format tracks for DLNA play_tracks
            # Album name: param > top-level response > per-track > fallback
            album_title = album_name_param or parsed.get("album", "")
            dlna_tracks = []
            for t in jellyfin_tracks:
                dlna_tracks.append({
                    "url": t.get("api_stream", ""),
                    "title": t.get("name", ""),
                    "artist": t.get("artist", ""),
                    "album": t.get("album", album_title),
                })

            # Step 3: Call DLNA play_tracks
            dlna_result = await mcp_manager.execute_tool(
                "mcp.dlna.play_tracks",
                {
                    "renderer_name": renderer_name,
                    "tracks": _json.dumps(dlna_tracks),
                },
            )

            if not dlna_result.get("success"):
                return {
                    "success": False,
                    "message": f"DLNA playback failed: {dlna_result.get('message', 'unknown error')}",
                    "action_taken": False,
                }

            album_name = album_title or (jellyfin_tracks[0].get("album", "Unknown Album") if jellyfin_tracks else "Unknown")
            artist_name = jellyfin_tracks[0].get("artist", "Unknown Artist") if jellyfin_tracks else "Unknown"

            # Resolve the room name for media follow (might come from renderer_name or room_name param)
            follow_room = room_name or renderer_name
            await self._register_media_follow(
                params, follow_room, "dlna_album",
                album_id=album_id, album_name=album_name,
                renderer_name=renderer_name,
                total_tracks=len(dlna_tracks),
                title=f"{album_name} - {artist_name}",
            )

            return {
                "success": True,
                "message": f"Playing '{album_name}' by {artist_name} on {renderer_name} ({len(dlna_tracks)} tracks)",
                "action_taken": True,
                "data": {
                    "album": album_name,
                    "artist": artist_name,
                    "renderer": renderer_name,
                    "track_count": len(dlna_tracks),
                    "first_track": dlna_tracks[0]["title"] if dlna_tracks else None,
                },
            }

        except Exception as e:
            logger.error(f"Error playing album on DLNA: {e}")
            return {
                "success": False,
                "message": f"Error playing album on DLNA: {e!s}",
                "action_taken": False,
            }

    async def _play_from_server(self, params: dict) -> dict:
        """Play a DLNA MediaServer library object on a room's DLNA renderer.

        Resolves room → DLNA renderer, then calls mcp.dlna.play_from_server,
        which resolves the object's playable items server-side and plays them as
        a gapless queue (no content URLs from the caller). Pairs with
        mcp.dlna.list_servers + browse_server/search_server.
        """
        server_name = (params.get("server_name") or "").strip()
        object_id = (params.get("object_id") or "").strip()
        room_name = (params.get("room_name") or "").strip()

        if not server_name:
            return {"success": False, "message": "Parameter 'server_name' is required", "action_taken": False}
        if not object_id:
            return {"success": False, "message": "Parameter 'object_id' is required", "action_taken": False}
        if not room_name:
            return {"success": False, "message": "Parameter 'room_name' is required", "action_taken": False}

        resolve_result = await self._resolve_room_player({"room_name": room_name})
        if not resolve_result.get("success"):
            # A busy device resolves to success=False with the generic "ask to
            # interrupt" message — but play_from_server has no force param, so
            # give a clear, actionable error instead of leaking resolve vocab.
            if resolve_result.get("data", {}).get("status") == "busy":
                return {
                    "success": False,
                    "message": f"Room '{room_name}' is currently playing — stop it first before playing from a media server.",
                    "action_taken": False,
                }
            return resolve_result
        data = resolve_result["data"]
        if data.get("target_type") != "dlna":
            return {
                "success": False,
                "message": f"Room '{room_name}' has no DLNA renderer configured (found {data.get('target_type', 'unknown')})",
                "action_taken": False,
            }
        renderer_name = data.get("dlna_renderer_name", "")
        resolved_room = data.get("room_name", room_name)

        try:
            from main import app
            mcp_manager = getattr(app.state, "mcp_manager", None)
            if not mcp_manager:
                return {"success": False, "message": "MCP manager not available", "action_taken": False}

            res = await mcp_manager.execute_tool(
                "mcp.dlna.play_from_server",
                {"server_name": server_name, "object_id": object_id, "renderer_name": renderer_name},
            )
            if not res.get("success"):
                return {
                    "success": False,
                    "message": f"Play from server failed: {res.get('message', 'unknown error')}",
                    "action_taken": False,
                }
            return {
                "success": True,
                "message": f"Playing from {server_name} on {resolved_room}",
                "action_taken": True,
                "data": {
                    "room_name": resolved_room,
                    "renderer_name": renderer_name,
                    "server_name": server_name,
                    "object_id": object_id,
                    "result": self._extract_mcp_json(res),
                },
            }
        except Exception as e:
            logger.error(f"Error playing from server '{server_name}' object '{object_id}' in '{room_name}': {e}")
            return {"success": False, "message": f"Error playing from server: {e!s}", "action_taken": False}

    async def _resolve_room_visual_player(self, params: dict) -> dict:
        """
        Resolve room_name → visual DLNA renderer (Smart TV).

        Analogous to _resolve_room_player but uses get_visual_output_for_room
        instead of get_audio_output_for_room.
        """
        room_name = params.get("room_name", "").strip()
        if not room_name:
            return {
                "success": False,
                "message": "Parameter 'room_name' is required",
                "action_taken": False,
            }

        try:
            from services.database import AsyncSessionLocal
            from ha_glue.services.output_routing_service import OutputRoutingService
            from ha_glue.services.room_service import RoomService

            async with AsyncSessionLocal() as db:
                room_service = RoomService(db)
                room = await room_service.get_room_by_name(room_name)
                if not room:
                    room = await room_service.get_room_by_alias(room_name)

                if not room:
                    return {
                        "success": False,
                        "message": f"Room '{room_name}' not found",
                        "action_taken": False,
                    }

                routing_service = OutputRoutingService(db)
                decision = await routing_service.get_visual_output_for_room(room.id)

                if decision.reason == "no_output_devices_configured":
                    return {
                        "success": False,
                        "message": f"No visual output device (Smart TV) configured for room '{room.name}'",
                        "action_taken": False,
                    }

                if not decision.output_device:
                    return {
                        "success": False,
                        "message": f"No visual output device available for room '{room.name}'",
                        "action_taken": False,
                    }

                if decision.target_type == "dlna":
                    return {
                        "success": True,
                        "message": f"Found visual DLNA renderer for {room.name}: {decision.output_device.dlna_renderer_name}",
                        "action_taken": True,
                        "data": {
                            "target_type": "dlna",
                            "dlna_renderer_name": decision.output_device.dlna_renderer_name,
                            "room_name": room.name,
                            "device_name": decision.output_device.device_name or decision.output_device.dlna_renderer_name,
                        },
                    }

                return {
                    "success": False,
                    "message": f"Visual output device in room '{room.name}' is not a DLNA renderer",
                    "action_taken": False,
                }

        except Exception as e:
            logger.error(f"Error resolving visual player for '{room_name}': {e}")
            return {
                "success": False,
                "message": f"Error resolving visual player: {e!s}",
                "action_taken": False,
            }

    async def _play_video_on_dlna(self, params: dict) -> dict:
        """
        Play a Jellyfin movie or episode on a DLNA renderer (Smart TV).

        1. Resolve renderer: room_name → visual output → DLNA renderer
        2. Get video stream URL from Jellyfin via get_stream_url
        3. Send to DLNA with media_type="video" for Movie DIDL metadata
        """
        item_id = (params.get("item_id") or "").strip()
        renderer_name = (params.get("renderer_name") or "").strip()
        room_name = (params.get("room_name") or "").strip()
        title = (params.get("title") or "").strip()
        image_url = (params.get("image_url") or "").strip()

        if not item_id:
            return {
                "success": False,
                "message": "Parameter 'item_id' is required",
                "action_taken": False,
            }

        # Resolve renderer_name from room config (visual output) if not provided
        if not renderer_name and room_name:
            resolve_result = await self._resolve_room_visual_player({"room_name": room_name})
            if not resolve_result.get("success"):
                return resolve_result
            data = resolve_result.get("data", {})
            renderer_name = data.get("dlna_renderer_name", "")

        if not renderer_name:
            return {
                "success": False,
                "message": "Either 'renderer_name' or 'room_name' is required",
                "action_taken": False,
            }

        try:
            import json as _json

            from main import app
            mcp_manager = getattr(app.state, "mcp_manager", None)
            if not mcp_manager:
                return {
                    "success": False,
                    "message": "MCP manager not available",
                    "action_taken": False,
                }

            # Step 1: Get video stream URL from Jellyfin
            stream_result = await mcp_manager.execute_tool(
                "mcp.jellyfin.get_stream_url",
                {"item_id": item_id},
            )
            if not stream_result.get("success"):
                return {
                    "success": False,
                    "message": f"Failed to get stream URL: {stream_result.get('message', 'unknown error')}",
                    "action_taken": False,
                }

            # Parse MCP response to extract video_stream URL
            raw_text = ""
            stream_data = stream_result.get("data", [])
            if isinstance(stream_data, list):
                for item in stream_data:
                    if isinstance(item, dict) and item.get("type") == "text":
                        raw_text = item.get("text", "")
                        break
            if not raw_text:
                raw_text = stream_result.get("message", "")

            try:
                parsed = _json.loads(raw_text)
            except (ValueError, TypeError):
                parsed = {}

            video_url = parsed.get("video_stream", "")
            if not video_url:
                return {
                    "success": False,
                    "message": "No video stream URL found for this item",
                    "action_taken": False,
                }

            display_title = title or parsed.get("name", "Video")

            # Step 2: Play via DLNA with video media_type
            dlna_tracks = [{
                "url": video_url,
                "title": display_title,
                "media_type": "video",
            }]
            if image_url:
                dlna_tracks[0]["art_url"] = image_url

            dlna_result = await mcp_manager.execute_tool(
                "mcp.dlna.play_tracks",
                {
                    "renderer_name": renderer_name,
                    "tracks": _json.dumps(dlna_tracks),
                },
            )

            if not dlna_result.get("success"):
                return {
                    "success": False,
                    "message": f"DLNA video playback failed: {dlna_result.get('message', 'unknown error')}",
                    "action_taken": False,
                }

            # Register with Media Follow (reuse album_id field for item_id)
            follow_room = room_name or renderer_name
            await self._register_media_follow(
                params, follow_room, "dlna_video",
                album_id=item_id,
                renderer_name=renderer_name,
                title=display_title,
                media_url=video_url,
            )

            return {
                "success": True,
                "message": f"Playing '{display_title}' on {renderer_name}",
                "action_taken": True,
                "data": {
                    "title": display_title,
                    "renderer": renderer_name,
                    "item_id": item_id,
                    "video_url": video_url,
                },
            }

        except Exception as e:
            logger.error(f"Error playing video on DLNA: {e}")
            return {
                "success": False,
                "message": f"Error playing video on DLNA: {e!s}",
                "action_taken": False,
            }

    @staticmethod
    def _format_last_seen(last_seen: float) -> str:
        """Format a timestamp as human-readable relative time."""
        delta = time.time() - last_seen
        if delta < 60:
            return "just now"
        if delta < 3600:
            minutes = int(delta / 60)
            return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
        if delta < 86400:
            hours = int(delta / 3600)
            return f"{hours} hour{'s' if hours != 1 else ''} ago"
        days = int(delta / 86400)
        return f"{days} day{'s' if days != 1 else ''} ago"

    async def _get_user_location(self, params: dict) -> dict:
        """Get the current or last known room location of a user."""
        from ha_glue.services.presence_service import get_presence_service

        user_name = (params.get("user_name") or "").strip()
        if not user_name:
            return {
                "success": False,
                "message": "Parameter 'user_name' is required",
                "action_taken": False,
            }

        presence_service = get_presence_service()
        user_id = presence_service.find_user_by_name(user_name)

        if user_id is None:
            return {
                "success": False,
                "message": f"User '{user_name}' not found",
                "action_taken": False,
            }

        display_name = presence_service.get_display_name(user_id)
        presence = presence_service.get_user_presence(user_id)

        if presence is None or presence.room_id is None:
            return {
                "success": True,
                "message": f"{display_name} has no known location",
                "action_taken": True,
                "data": {
                    "user_name": display_name,
                    "status": "unknown",
                },
            }

        return {
            "success": True,
            "message": f"{display_name} is in {presence.room_name or 'unknown room'}",
            "action_taken": True,
            "data": {
                "user_name": display_name,
                "status": "present",
                "room_name": presence.room_name,
                "room_id": presence.room_id,
                "last_seen": self._format_last_seen(presence.last_seen),
                "confidence": round(presence.confidence, 2),
            },
        }

    async def _get_all_presence(self, params: dict) -> dict:
        """Get all currently present users and their room locations."""
        from ha_glue.services.presence_service import get_presence_service

        presence_service = get_presence_service()
        all_presence = presence_service.get_all_presence()

        if not all_presence:
            return {
                "success": True,
                "message": "Nobody is currently detected at home",
                "action_taken": True,
                "data": {"users": []},
            }

        users = []
        for user_id, presence in all_presence.items():
            users.append({
                "name": presence_service.get_display_name(user_id),
                "room": presence.room_name or "unknown",
                "last_seen": self._format_last_seen(presence.last_seen),
            })

        return {
            "success": True,
            "message": f"{len(users)} user(s) detected at home",
            "action_taken": True,
            "data": {"users": users},
        }

    async def _presence_history(self, params: dict) -> dict:
        """Query a user's PERSISTED presence history (timeline / last-seen /
        who-was-in-room) — survives restarts, unlike the live presence tools."""
        from datetime import UTC, datetime, timedelta

        from ha_glue.utils.config import ha_glue_settings

        if not ha_glue_settings.presence_history_enabled:
            return {
                "success": False,
                "message": "Presence history is disabled",
                "action_taken": False,
            }

        from ha_glue.services.presence_analytics import _analytics_tz, _to_local
        from ha_glue.services.presence_service import get_presence_service

        query_type = (params.get("query_type") or "timeline").strip().lower()
        user_name = (params.get("user_name") or "").strip()
        room_name = (params.get("room_name") or "").strip()

        # Parse the ISO8601 window (default: last 24h, naive UTC to match storage).
        def _parse_iso(value: str | None) -> datetime | None:
            if not value:
                return None
            try:
                dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            except ValueError:
                return None
            if dt.tzinfo is not None:
                dt = dt.astimezone(UTC).replace(tzinfo=None)
            return dt

        now = datetime.now(UTC).replace(tzinfo=None)
        until = _parse_iso(params.get("until")) or now
        since = _parse_iso(params.get("since")) or (until - timedelta(hours=24))

        tz = _analytics_tz()

        def _fmt(dt: datetime | None) -> str:
            if dt is None:
                return "unknown"
            return _to_local(dt, tz).strftime("%Y-%m-%d %H:%M")

        presence_service = get_presence_service()

        # Resolve the room filter (ILIKE) when given.
        room_id = None
        if room_name:
            from sqlalchemy import select

            from models.database import Room
            from services.database import AsyncSessionLocal

            async with AsyncSessionLocal() as db:
                result = await db.execute(
                    select(Room).where(Room.name.ilike(f"%{room_name}%"))
                )
                room = result.scalars().first()
            if room is None:
                return {
                    "success": False,
                    "message": f"Room '{room_name}' not found",
                    "action_taken": False,
                }
            room_id = room.id

        from ha_glue.services.presence_analytics import PresenceAnalyticsService
        from services.database import AsyncSessionLocal

        if query_type == "who_was_in_room":
            if room_id is None:
                return {
                    "success": False,
                    "message": "Parameter 'room_name' is required for 'who_was_in_room'",
                    "action_taken": False,
                }
            async with AsyncSessionLocal() as db:
                service = PresenceAnalyticsService(db)
                events = await service.get_room_occupancy_window(
                    room_id=room_id, since=since, until=until
                )
            names = []
            for ev in events:
                name = presence_service.get_display_name(ev["user_id"])
                ev["user_name"] = name
                if ev["event_type"] == "enter" and name not in names:
                    names.append(name)
            who = ", ".join(names) if names else "nobody"
            summary = (
                f"Between {_fmt(since)} and {_fmt(until)}, {room_name} was entered by: {who}."
                if names
                else f"No presence events for {room_name} between {_fmt(since)} and {_fmt(until)}."
            )
            return {
                "success": True,
                "message": f"{len(events)} event(s) in {room_name}",
                "action_taken": True,
                "data": {"room_name": room_name, "events": events},
                "summary": summary,
            }

        # timeline / last_seen_by_room both need a resolved user.
        if not user_name:
            return {
                "success": False,
                "message": "Parameter 'user_name' is required",
                "action_taken": False,
            }
        user_id = presence_service.find_user_by_name(user_name)
        if user_id is None:
            return {
                "success": False,
                "message": f"User '{user_name}' not found",
                "action_taken": False,
            }
        display_name = presence_service.get_display_name(user_id)

        if query_type == "last_seen_by_room":
            async with AsyncSessionLocal() as db:
                service = PresenceAnalyticsService(db)
                rows = await service.get_last_seen_by_room(user_id=user_id)
            if not rows:
                return {
                    "success": True,
                    "message": f"No presence history for {display_name}",
                    "action_taken": True,
                    "data": {"user_name": display_name, "rooms": []},
                    "summary": f"There is no recorded presence history for {display_name}.",
                }
            parts = [f"{r['room_name']} ({_fmt(r['last_seen'])})" for r in rows]
            summary = f"{display_name} was last seen in: " + "; ".join(parts) + "."
            return {
                "success": True,
                "message": f"{len(rows)} room(s) for {display_name}",
                "action_taken": True,
                "data": {"user_name": display_name, "rooms": rows},
                "summary": summary,
            }

        # Default: timeline.
        async with AsyncSessionLocal() as db:
            service = PresenceAnalyticsService(db)
            events = await service.get_timeline(
                user_id=user_id,
                since=since,
                until=until,
                room_id=room_id,
                limit=100,
            )
        if not events:
            return {
                "success": True,
                "message": f"No presence events for {display_name} in that window",
                "action_taken": True,
                "data": {"user_name": display_name, "events": events},
                "summary": (
                    f"{display_name} has no recorded presence events between "
                    f"{_fmt(since)} and {_fmt(until)}."
                ),
            }
        lines = [
            f"{_fmt(ev['created_at'])}: {ev['event_type']} {ev['room_name'] or 'unknown room'}"
            for ev in events
        ]
        summary = (
            f"{display_name}'s presence between {_fmt(since)} and {_fmt(until)}:\n"
            + "\n".join(lines)
        )
        return {
            "success": True,
            "message": f"{len(events)} event(s) for {display_name}",
            "action_taken": True,
            "data": {"user_name": display_name, "events": events},
            "summary": summary,
        }

    # ── Radio tools ──────────────────────────────────────────────────────────

    async def _play_radio(self, params: dict) -> dict:
        """
        Play a radio station in a room.

        1. Resolve station_id → stream URL via mcp.radio.get_stream_url
        2. Play the stream URL via _play_in_room
        """
        station_id = (params.get("station_id") or "").strip()
        room_name = (params.get("room_name") or "").strip()
        station_name = (params.get("station_name") or "").strip() or None
        station_image = (params.get("station_image") or "").strip() or None
        force = str(params.get("force", "false")).lower() in ("true", "1", "yes")

        if not station_id:
            return {
                "success": False,
                "message": "Parameter 'station_id' is required",
                "action_taken": False,
            }
        if not room_name:
            return {
                "success": False,
                "message": "Parameter 'room_name' is required",
                "action_taken": False,
            }

        try:
            import json as _json

            from main import app
            mcp_manager = getattr(app.state, "mcp_manager", None)
            if not mcp_manager:
                return {
                    "success": False,
                    "message": "MCP manager not available",
                    "action_taken": False,
                }

            # Step 1: Resolve stream URL via radio MCP
            stream_result = await mcp_manager.execute_tool(
                "mcp.radio.get_stream_url",
                {"station_id": station_id},
            )
            if not stream_result.get("success"):
                return {
                    "success": False,
                    "message": f"Failed to resolve stream URL: {stream_result.get('message', 'unknown error')}",
                    "action_taken": False,
                }

            # Parse MCP response to extract stream_url
            raw_text = ""
            data = stream_result.get("data", [])
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, dict) and item.get("type") == "text":
                        raw_text = item.get("text", "")
                        break
            if not raw_text:
                raw_text = stream_result.get("message", "")

            try:
                parsed = _json.loads(raw_text)
            except (ValueError, TypeError):
                parsed = {}

            stream_url = parsed.get("stream_url", "")

            # Guard against TuneIn's "not compatible" placeholder. An invalid or
            # guessed station_id (the classic failure: the LLM skips
            # mcp.radio.search_stations and copies a schema example or reuses an
            # id from memory) resolves to
            # cdn-cms.tunein.com/service/Audio/notcompatible.<locale>.mp3 — a
            # dead placeholder that "plays" as silence and made the agent loop on
            # a bad id. Scan the WHOLE resolver response (not just the parsed
            # stream_url field) so a response-shape change can't slip it past,
            # and check this BEFORE the empty-url branch so a bad id gets the
            # actionable "search first" message rather than a generic error.
            if "notcompatible" in f"{raw_text} {stream_url}".lower():
                logger.warning(
                    "Radio station_id '%s' resolved to the TuneIn 'notcompatible' "
                    "placeholder — invalid or guessed id (search_stations was likely skipped)",
                    station_id,
                )
                return {
                    "success": False,
                    "message": (
                        f"'{station_id}' is not a valid radio station. Call "
                        "mcp.radio.search_stations(query=...) to find the correct "
                        "station_id, then play that — never guess or reuse an id."
                    ),
                    "action_taken": False,
                }

            if not stream_url:
                return {
                    "success": False,
                    "message": "Could not resolve stream URL for this station",
                    "action_taken": False,
                }

            # Step 2: Resolve room → device (DLNA or HA)
            resolve_result = await self._resolve_room_player({"room_name": room_name})

            if not resolve_result.get("success"):
                # Force-play on busy device
                if force and resolve_result.get("data", {}).get("status") == "busy":
                    resolve_result = {"success": True, "data": resolve_result["data"]}
                else:
                    return resolve_result

            target_type = resolve_result["data"].get("target_type", "homeassistant")
            display_name = station_name or station_id
            resolved_room = resolve_result["data"].get("room_name", room_name)

            if target_type == "dlna":
                # Play radio stream via DLNA renderer
                renderer_name = resolve_result["data"].get("dlna_renderer_name", "")
                dlna_tracks = [{"url": stream_url, "title": display_name}]
                dlna_result = await mcp_manager.execute_tool(
                    "mcp.dlna.play_tracks",
                    {
                        "renderer_name": renderer_name,
                        "tracks": _json.dumps(dlna_tracks),
                    },
                )
                if not dlna_result.get("success"):
                    return {
                        "success": False,
                        "message": f"DLNA playback failed: {dlna_result.get('message', 'unknown error')}",
                        "action_taken": False,
                    }
                await self._register_media_follow(
                    params, resolved_room, "radio",
                    media_url=stream_url, station_id=station_id,
                    station_name=station_name,
                )
                return {
                    "success": True,
                    "message": f"Playing '{display_name}' in {resolved_room}",
                    "action_taken": True,
                    "data": {"room_name": resolved_room, "station": display_name, "stream_url": stream_url},
                }
            else:
                # Play via Home Assistant media player
                play_params = {
                    "media_url": stream_url,
                    "room_name": room_name,
                    "media_type": "music",
                    "force": str(force).lower(),
                    "user_id": params.get("user_id"),
                }
                if station_name:
                    play_params["title"] = station_name
                if station_image:
                    play_params["thumb"] = station_image

                result = await self._play_in_room(play_params)

                if result.get("success"):
                    # Re-register as radio (not single_url) with station metadata
                    await self._register_media_follow(
                        params, resolved_room, "radio",
                        media_url=stream_url, station_id=station_id,
                        station_name=station_name,
                    )
                    if station_name:
                        result["message"] = f"Playing '{station_name}' in {resolved_room}"

                return result

        except Exception as e:
            logger.error(f"Error playing radio station '{station_id}' in '{room_name}': {e}")
            return {
                "success": False,
                "message": f"Error playing radio: {e!s}",
                "action_taken": False,
            }

    async def _save_radio_favorite(self, params: dict) -> dict:
        """Save a radio station as a user favorite. Idempotent (upsert)."""
        station_id = (params.get("station_id") or "").strip()
        station_name = (params.get("station_name") or "").strip()
        station_image = (params.get("station_image") or "").strip() or None
        genre = (params.get("genre") or "").strip() or None
        user_id = params.get("user_id")

        if not station_id:
            return {
                "success": False,
                "message": "Parameter 'station_id' is required",
                "action_taken": False,
            }
        if not station_name:
            return {
                "success": False,
                "message": "Parameter 'station_name' is required",
                "action_taken": False,
            }

        try:
            from sqlalchemy import select as sa_select

            from models.database import RadioFavorite
            from services.database import AsyncSessionLocal

            async with AsyncSessionLocal() as db:
                # Check if already saved (idempotent)
                stmt = sa_select(RadioFavorite).where(
                    RadioFavorite.user_id == user_id,
                    RadioFavorite.station_id == station_id,
                )
                result = await db.execute(stmt)
                existing = result.scalar_one_or_none()

                if existing:
                    return {
                        "success": True,
                        "message": f"'{station_name}' is already in your favorites",
                        "action_taken": False,
                    }

                fav = RadioFavorite(
                    user_id=user_id,
                    station_id=station_id,
                    station_name=station_name,
                    station_image=station_image,
                    genre=genre,
                )
                db.add(fav)
                await db.commit()

                return {
                    "success": True,
                    "message": f"Saved '{station_name}' to your radio favorites",
                    "action_taken": True,
                }

        except Exception as e:
            logger.error(f"Error saving radio favorite: {e}")
            return {
                "success": False,
                "message": f"Error saving favorite: {e!s}",
                "action_taken": False,
            }

    async def _list_radio_favorites(self, params: dict) -> dict:
        """List the current user's radio station favorites."""
        user_id = params.get("user_id")

        try:
            from sqlalchemy import select as sa_select

            from models.database import RadioFavorite
            from services.database import AsyncSessionLocal

            async with AsyncSessionLocal() as db:
                stmt = (
                    sa_select(RadioFavorite)
                    .where(RadioFavorite.user_id == user_id)
                    .order_by(RadioFavorite.created_at.desc())
                )
                result = await db.execute(stmt)
                favorites = result.scalars().all()

                if not favorites:
                    return {
                        "success": True,
                        "message": "No radio favorites saved yet",
                        "action_taken": True,
                        "empty_result": True,
                        "data": {"favorites": []},
                    }

                items = [
                    {
                        "station_id": f.station_id,
                        "station_name": f.station_name,
                        "station_image": f.station_image,
                        "genre": f.genre,
                    }
                    for f in favorites
                ]

                return {
                    "success": True,
                    "message": f"{len(items)} radio favorite(s) found",
                    "action_taken": True,
                    "data": {"favorites": items},
                }

        except Exception as e:
            logger.error(f"Error listing radio favorites: {e}")
            return {
                "success": False,
                "message": f"Error listing favorites: {e!s}",
                "action_taken": False,
            }

    async def _remove_radio_favorite(self, params: dict) -> dict:
        """Remove a radio station from user's favorites."""
        station_id = (params.get("station_id") or "").strip()
        user_id = params.get("user_id")

        if not station_id:
            return {
                "success": False,
                "message": "Parameter 'station_id' is required",
                "action_taken": False,
            }

        try:
            from sqlalchemy import delete as sa_delete

            from models.database import RadioFavorite
            from services.database import AsyncSessionLocal

            async with AsyncSessionLocal() as db:
                stmt = (
                    sa_delete(RadioFavorite)
                    .where(RadioFavorite.user_id == user_id)
                    .where(RadioFavorite.station_id == station_id)
                )
                result = await db.execute(stmt)
                await db.commit()

                if result.rowcount == 0:
                    return {
                        "success": False,
                        "message": f"Station '{station_id}' not found in your favorites",
                        "action_taken": False,
                    }

                return {
                    "success": True,
                    "message": f"Removed station from your favorites",
                    "action_taken": True,
                }

        except Exception as e:
            logger.error(f"Error removing radio favorite: {e}")
            return {
                "success": False,
                "message": f"Error removing favorite: {e!s}",
                "action_taken": False,
            }
