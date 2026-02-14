"""Presence webhook dispatcher — forwards presence events to external URL (e.g. n8n)."""

import httpx
from loguru import logger

from utils.config import settings
from utils.hooks import register_hook

_client: httpx.AsyncClient | None = None


async def _dispatch(event: str, **kwargs):
    """POST event to webhook URL. Fire-and-forget with error logging."""
    global _client
    if not _client:
        _client = httpx.AsyncClient(timeout=10.0)

    headers = {"Content-Type": "application/json"}
    if settings.presence_webhook_secret:
        headers["X-Webhook-Secret"] = settings.presence_webhook_secret

    payload = {"event": event, **kwargs}
    try:
        resp = await _client.post(settings.presence_webhook_url, json=payload, headers=headers)
        resp.raise_for_status()
        logger.debug(f"Presence webhook: {event} → {resp.status_code}")
    except Exception:
        logger.opt(exception=True).warning(f"Presence webhook failed for {event}")


def register_presence_webhooks():
    """Register hook handlers for all presence events."""
    if not settings.presence_webhook_url:
        return

    for event in (
        "presence_enter_room",
        "presence_leave_room",
        "presence_first_arrived",
        "presence_last_left",
    ):
        async def handler(ev=event, **kwargs):
            await _dispatch(ev, **kwargs)

        register_hook(event, handler)

    logger.info(f"Presence webhooks registered → {settings.presence_webhook_url}")
