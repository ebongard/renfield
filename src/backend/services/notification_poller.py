"""
Generic MCP Notification Poller.

Periodically polls MCP servers that expose a `get_pending_notifications` tool
and feeds the results into NotificationService for delivery (WebSocket + TTS).

Each MCP server can opt-in via mcp_servers.yaml:

    - name: calendar
      notifications:
        enabled: true
        poll_interval: 900          # seconds (15 min)
        tool: get_pending_notifications
        lookahead_minutes: 45

The poller is started from lifecycle.py after MCP servers are connected.
"""

import asyncio
import json
import logging

from utils.config import settings

logger = logging.getLogger(__name__)


class NotificationPollerService:
    """Polls MCP servers for pending notifications."""

    def __init__(self, mcp_manager):
        self._mcp_manager = mcp_manager
        self._tasks: list[asyncio.Task] = []
        self._seen_keys: set[str] = set()  # In-memory dedup within poll cycles

    def get_pollable_servers(self) -> list[dict]:
        """Return list of server configs that have notifications enabled."""
        result = []
        for state in self._mcp_manager._servers.values():
            cfg = state.config
            if cfg.notifications and cfg.notifications.get("enabled") and state.connected:
                result.append({
                    "name": cfg.name,
                    "poll_interval": cfg.notifications.get("poll_interval", 900),
                    "tool": cfg.notifications.get("tool", "get_pending_notifications"),
                    "lookahead_minutes": cfg.notifications.get("lookahead_minutes", 45),
                })
        return result

    async def start(self):
        """Start polling loops for all configured servers."""
        servers = self.get_pollable_servers()
        if not servers:
            logger.info("No MCP servers configured for notification polling")
            return

        # Initial delay to let MCP servers stabilize
        startup_delay = settings.notification_poller_startup_delay
        if startup_delay > 0:
            logger.info(
                "Notification poller starting in %ds for %d server(s): %s",
                startup_delay,
                len(servers),
                [s["name"] for s in servers],
            )
            await asyncio.sleep(startup_delay)

        for server in servers:
            task = asyncio.create_task(
                self._poll_loop(server),
                name=f"notification-poll-{server['name']}",
            )
            self._tasks.append(task)
            logger.info(
                "Notification poller started for '%s' (interval=%ds, tool=%s)",
                server["name"],
                server["poll_interval"],
                server["tool"],
            )

    async def stop(self):
        """Cancel all polling tasks."""
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        logger.info("Notification poller stopped")

    async def _poll_loop(self, server: dict):
        """Polling loop for a single MCP server."""
        name = server["name"]
        interval = server["poll_interval"]
        tool_name = f"mcp.{name}.{server['tool']}"
        lookahead = server["lookahead_minutes"]

        while True:
            try:
                await self._poll_once(name, tool_name, lookahead)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.warning("Poll failed for '%s'", name, exc_info=True)

            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                break

    async def _poll_once(self, server_name: str, tool_name: str, lookahead_minutes: int):
        """Execute a single poll cycle for one server."""
        result = await self._mcp_manager.execute_tool(
            tool_name,
            {"lookahead_minutes": lookahead_minutes},
        )

        if not result.get("success"):
            logger.warning(
                "Poll tool call failed for '%s': %s",
                server_name,
                result.get("message", "unknown error"),
            )
            return

        # Parse the response — MCP returns {"success": True, "message": "<json>", "data": [...]}
        notifications = self._parse_poll_result(result)
        if not notifications:
            return

        logger.debug("Poll '%s': %d notification(s) received", server_name, len(notifications))

        for notification in notifications:
            await self._process_notification(server_name, notification)

    def _parse_poll_result(self, result: dict) -> list[dict]:
        """Extract notification list from MCP tool result."""
        # The MCP response message contains JSON text
        message = result.get("message", "")
        if not message:
            return []

        try:
            parsed = json.loads(message)
        except (json.JSONDecodeError, TypeError):
            # Not JSON — might be a plain text response
            return []

        # The tool returns a list of notification objects directly,
        # or wrapped in a {"notifications": [...]} envelope
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict) and "notifications" in parsed:
            return parsed["notifications"]

        return []

    async def _process_notification(self, server_name: str, notification: dict):
        """Process a single notification from poll results."""
        dedup_key = notification.get("dedup_key", "")
        if not dedup_key:
            logger.warning("Notification from '%s' missing dedup_key, skipping", server_name)
            return

        # In-memory dedup (avoids re-processing within same session)
        if dedup_key in self._seen_keys:
            return
        self._seen_keys.add(dedup_key)

        # Prune seen_keys to prevent unbounded growth (keep last 1000)
        if len(self._seen_keys) > 1000:
            # Convert to list, keep last 500
            keys_list = list(self._seen_keys)
            self._seen_keys = set(keys_list[-500:])

        try:
            from services.database import AsyncSessionLocal
            from services.notification_service import NotificationService

            async with AsyncSessionLocal() as db_session:
                service = NotificationService(db_session)
                await service.process_webhook(
                    event_type=notification.get("event_type", f"{server_name}.notification"),
                    title=notification.get("title", "Notification"),
                    message=notification.get("message", ""),
                    urgency=notification.get("urgency", "info"),
                    room=notification.get("room"),
                    tts=notification.get("tts", True),
                    data=notification.get("data"),
                    source=f"mcp_poll:{server_name}",
                    privacy=notification.get("privacy", "public"),
                    target_user_id=notification.get("target_user_id"),
                )
                logger.info(
                    "Notification delivered from '%s': %s",
                    server_name,
                    notification.get("title", "?"),
                )
        except ValueError as e:
            # Dedup/suppression — expected, not an error
            logger.debug("Notification suppressed for '%s': %s", server_name, e)
        except Exception:
            logger.warning(
                "Failed to process notification from '%s'",
                server_name,
                exc_info=True,
            )
