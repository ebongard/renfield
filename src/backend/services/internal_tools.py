"""
Internal Agent Tools — Provider-agnostic tools for the Agent Loop.

These tools handle cross-cutting concerns that don't belong to any
specific MCP server:
- Room resolution (room name → media_player entity)
- Media playback (any URL → any room's audio device via HA)
- Presence queries (user location via BLE/voice presence)

This keeps playback logic provider-agnostic: Jellyfin, Spotify, or any
future provider just needs to supply a stream URL. The internal tools
handle routing it to the correct room device.
"""
import json
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
                "queue": "JSON array of additional tracks to enqueue after the main track. Each object: {\"url\": \"...\", \"title\": \"...\", \"thumb\": \"...\"}. Optional.",
            },
        },
        "internal.get_user_location": {
            "description": "Get the current or last known room location of a user. Accepts username or first/last name.",
            "parameters": {
                "user_name": "Name of the user to locate (username, first name, or last name)",
            },
        },
        "internal.get_all_presence": {
            "description": "Get all currently present users and their room locations. Use this when asked 'where is everyone?' or 'who is home?'.",
            "parameters": {},
        },
        "internal.knowledge_search": {
            "description": "Search the user's local knowledge base (uploaded documents, invoices, contracts) by semantic similarity. Returns matching text passages with source document info.",
            "parameters": {
                "query": "Search query (required)",
                "top_k": "Maximum number of results to return (optional, default: from server config)",
            },
        },
        "internal.media_control": {
            "description": "Control media playback in a room: stop, pause, resume, next track, previous track.",
            "parameters": {
                "action": "Control action: stop, pause, resume, next, previous (required)",
                "room_name": "Target room name (required)",
            },
        },
    }

    _HANDLERS = {
        "internal.resolve_room_player": "_resolve_room_player",
        "internal.play_in_room": "_play_in_room",
        "internal.get_user_location": "_get_user_location",
        "internal.get_all_presence": "_get_all_presence",
        "internal.knowledge_search": "_knowledge_search",
        "internal.media_control": "_media_control",
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
            from services.output_routing_service import OutputRoutingService
            from services.room_service import RoomService

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
                    return {
                        "success": False,
                        "message": f"The audio device '{device_name}' in room '{room.name}' is currently busy (playing). Ask the user if they want to interrupt the current playback.",
                        "action_taken": False,
                        "data": {
                            "entity_id": busy_device.ha_entity_id if busy_device else None,
                            "room_name": room.name,
                            "device_name": device_name,
                            "status": "busy",
                        },
                    }

                if not decision.output_device:
                    return {
                        "success": False,
                        "message": f"No audio output device available for room '{room.name}'",
                        "action_taken": False,
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

        # Parse queue parameter (JSON array of additional tracks to enqueue)
        queue_tracks: list[dict] = []
        queue_raw = params.get("queue")
        if queue_raw:
            if isinstance(queue_raw, list):
                queue_tracks = queue_raw
            elif isinstance(queue_raw, str):
                try:
                    parsed = json.loads(queue_raw.strip())
                    if isinstance(parsed, list):
                        queue_tracks = parsed
                except (json.JSONDecodeError, TypeError):
                    logger.warning(f"Invalid queue JSON, ignoring: {str(queue_raw)[:200]}")
        has_queue = len(queue_tracks) > 0

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

        entity_id = resolve_result["data"]["entity_id"]
        resolved_room_name = resolve_result["data"]["room_name"]
        device_name = resolve_result["data"]["device_name"]

        # Step 2: Call HA media_player.play_media
        try:
            import asyncio as _asyncio

            from integrations.homeassistant import HomeAssistantClient

            ha_client = HomeAssistantClient()

            # Build service_data with optional metadata for HA media player UI.
            service_data = {
                "media_content_id": media_url,
                "media_content_type": ha_content_type,
            }
            extra = {}
            if title:
                extra["title"] = title
            if thumb:
                extra["thumb"] = thumb
            if has_queue:
                extra["enqueue"] = "play"
            if extra:
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
                # Enqueue remaining tracks if queue was provided
                queued = 0
                if queue_tracks:
                    queued = await self._enqueue_tracks(
                        ha_client, entity_id, ha_content_type, queue_tracks,
                    )
                total = 1 + queued
                msg = (
                    f"Playing {total} track(s) on {device_name} in {resolved_room_name}"
                    if queued
                    else f"Playing on {device_name} in {resolved_room_name}"
                )
                return {
                    "success": True,
                    "message": msg,
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
                if has_queue:
                    transcode_extra["enqueue"] = "play"
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
                    # Enqueue remaining tracks with transcode transformation
                    queued = 0
                    if queue_tracks:
                        queued = await self._enqueue_tracks(
                            ha_client, entity_id, ha_content_type,
                            queue_tracks, transcode=True,
                        )
                    total = 1 + queued
                    msg = (
                        f"Playing {total} track(s) (transcoded) on {device_name} in {resolved_room_name}"
                        if queued
                        else f"Playing (transcoded) on {device_name} in {resolved_room_name}"
                    )
                    return {
                        "success": True,
                        "message": msg,
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

    async def _enqueue_tracks(
        self,
        ha_client,
        entity_id: str,
        content_type: str,
        tracks: list[dict],
        transcode: bool = False,
    ) -> int:
        """Enqueue additional tracks on an already-playing media player."""
        enqueued = 0
        for track in tracks:
            url = (track.get("url") or "").strip()
            if not url:
                continue
            if transcode and "static=true" in url:
                url = url.replace(
                    "static=true",
                    "audioCodec=mp3&audioBitRate=320000",
                )

            extra: dict = {"enqueue": "add"}
            t_title = (track.get("title") or "").strip()
            t_thumb = (track.get("thumb") or "").strip()
            if t_title:
                extra["title"] = t_title
            if t_thumb:
                extra["thumb"] = t_thumb

            try:
                await ha_client.call_service(
                    domain="media_player",
                    service="play_media",
                    entity_id=entity_id,
                    service_data={
                        "media_content_id": url,
                        "media_content_type": content_type,
                        "extra": extra,
                    },
                    timeout=10.0,
                )
                enqueued += 1
            except Exception as exc:
                logger.warning(
                    f"Failed to enqueue track '{t_title or url}': {exc}"
                )
        return enqueued

    _MEDIA_ACTION_MAP = {
        "stop": "media_stop",
        "pause": "media_pause",
        "resume": "media_play",
        "next": "media_next_track",
        "previous": "media_previous_track",
    }

    async def _media_control(self, params: dict) -> dict:
        """
        Control media playback in a room (stop, pause, resume, next, previous).

        1. Validate action + room_name
        2. Resolve room → entity_id (accepts busy devices — we want to control them)
        3. Map action → HA media_player service
        4. Call HA service
        """
        action = (params.get("action") or "").strip().lower()
        room_name = (params.get("room_name") or "").strip()

        if not action:
            return {
                "success": False,
                "message": "Parameter 'action' is required",
                "action_taken": False,
            }

        if action not in self._MEDIA_ACTION_MAP:
            return {
                "success": False,
                "message": f"Invalid action '{action}'. Must be one of: {', '.join(self._MEDIA_ACTION_MAP)}",
                "action_taken": False,
            }

        if not room_name:
            return {
                "success": False,
                "message": "Parameter 'room_name' is required",
                "action_taken": False,
            }

        # Resolve room → entity_id.  For media control we *want* to target a
        # busy device (it's playing and we want to stop/pause/skip it), so if
        # _resolve_room_player returns "busy" we use that entity_id.
        resolve_result = await self._resolve_room_player({"room_name": room_name})

        if resolve_result.get("success"):
            entity_id = resolve_result["data"]["entity_id"]
            resolved_room_name = resolve_result["data"]["room_name"]
        elif resolve_result.get("data", {}).get("status") == "busy":
            entity_id = resolve_result["data"].get("entity_id")
            if not entity_id:
                return resolve_result
            resolved_room_name = resolve_result["data"]["room_name"]
        else:
            return resolve_result

        ha_service = self._MEDIA_ACTION_MAP[action]

        try:
            from integrations.homeassistant import HomeAssistantClient

            ha_client = HomeAssistantClient()
            await ha_client.call_service(
                domain="media_player",
                service=ha_service,
                entity_id=entity_id,
            )

            return {
                "success": True,
                "message": f"Media {action} executed on {resolved_room_name}",
                "action_taken": True,
                "data": {
                    "entity_id": entity_id,
                    "room_name": resolved_room_name,
                    "action": action,
                },
            }

        except Exception as e:
            logger.error(f"Error executing media {action} in '{room_name}': {e}")
            return {
                "success": False,
                "message": f"Error executing media {action}: {e!s}",
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
        from services.presence_service import get_presence_service

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
        from services.presence_service import get_presence_service

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

    async def _knowledge_search(self, params: dict) -> dict:
        """Search the local knowledge base (RAG) by semantic similarity."""
        query = (params.get("query") or "").strip()
        if not query:
            return {
                "success": False,
                "message": "Parameter 'query' is required",
                "action_taken": False,
            }

        # top_k: use parameter if provided, otherwise fall back to settings
        top_k = None
        if params.get("top_k"):
            try:
                top_k = int(params["top_k"])
            except (ValueError, TypeError):
                pass

        try:
            from services.database import AsyncSessionLocal
            from services.rag_service import RAGService

            async with AsyncSessionLocal() as db:
                rag = RAGService(db)
                results = await rag.search(query=query, top_k=top_k)

            if results:
                context_parts = []
                for r in results:
                    content = (
                        r.get("chunk", {}).get("content", "")
                        if isinstance(r.get("chunk"), dict)
                        else r.get("content", "")
                    )
                    source = (
                        r.get("document", {}).get("filename", "")
                        if isinstance(r.get("document"), dict)
                        else r.get("filename", "")
                    )
                    if content:
                        context_parts.append(f"[{source}] {content[:500]}")

                return {
                    "success": True,
                    "message": f"Knowledge base results ({len(results)} hits)",
                    "action_taken": True,
                    "data": {
                        "query": query,
                        "results_count": len(results),
                        "context": "\n\n".join(context_parts),
                    },
                }
            else:
                return {
                    "success": True,
                    "message": f"No results in knowledge base for: {query}",
                    "action_taken": True,
                    "empty_result": True,
                    "data": {"query": query, "results_count": 0},
                }

        except Exception as e:
            logger.error(f"Error in knowledge_search: {e}")
            return {
                "success": False,
                "message": f"Knowledge base search error: {e!s}",
                "action_taken": False,
            }
