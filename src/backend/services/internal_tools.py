"""
Internal Agent Tools — Provider-agnostic tools for the Agent Loop.

These tools handle cross-cutting concerns that don't belong to any
specific MCP server:
- Room resolution (room name → media_player entity)
- Media playback (any URL → any room's audio device via HA)

This keeps playback logic provider-agnostic: Jellyfin, Spotify, or any
future provider just needs to supply a stream URL. The internal tools
handle routing it to the correct room device.
"""
from typing import Dict, Optional
from loguru import logger


class InternalToolService:
    """Provider-agnostic internal tools for the Agent Loop."""

    TOOLS: Dict[str, Dict] = {
        "internal.resolve_room_player": {
            "description": "Find the media_player entity for a room by name",
            "parameters": {
                "room_name": "Room name to look up (required)",
            },
        },
        "internal.play_in_room": {
            "description": "Play a media URL on the audio device in a room via Home Assistant",
            "parameters": {
                "media_url": "Playable media URL (required)",
                "room_name": "Target room name (required)",
                "media_type": "Content type: music, video, playlist (default: music)",
            },
        },
    }

    _HANDLERS = {
        "internal.resolve_room_player": "_resolve_room_player",
        "internal.play_in_room": "_play_in_room",
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
            from services.room_service import RoomService
            from services.output_routing_service import OutputRoutingService

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

                if not decision.output_device:
                    return {
                        "success": False,
                        "message": f"No audio output device configured for room '{room.name}'",
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
                "message": f"Error resolving room: {str(e)}",
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
            return resolve_result

        entity_id = resolve_result["data"]["entity_id"]
        resolved_room_name = resolve_result["data"]["room_name"]
        device_name = resolve_result["data"]["device_name"]

        # Step 2: Call HA media_player.play_media
        try:
            from integrations.homeassistant import HomeAssistantClient

            ha_client = HomeAssistantClient()
            success = await ha_client.call_service(
                domain="media_player",
                service="play_media",
                entity_id=entity_id,
                service_data={
                    "media_content_id": media_url,
                    "media_content_type": media_type,
                },
                timeout=30.0,
            )

            if success:
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
            else:
                return {
                    "success": False,
                    "message": f"Home Assistant failed to play media on {entity_id}",
                    "action_taken": False,
                }

        except Exception as e:
            logger.error(f"Error playing media in '{room_name}': {e}")
            return {
                "success": False,
                "message": f"Error playing media: {str(e)}",
                "action_taken": False,
            }
