"""
Tests for presence webhook dispatcher.
"""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.unit
class TestPresenceWebhook:
    @pytest.mark.asyncio
    async def test_dispatch_posts_to_url(self):
        """Webhook POSTs event payload to configured URL."""
        mock_settings = type("S", (), {
            "presence_webhook_url": "http://n8n.local/webhook/presence",
            "presence_webhook_secret": "",
        })()

        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = lambda: None

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch("services.presence_webhook.settings", mock_settings):
            import services.presence_webhook as mod
            mod._client = mock_client

            await mod._dispatch("presence_enter_room", user_id=1, room_id=10)

        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        assert call_args[0][0] == "http://n8n.local/webhook/presence"
        payload = call_args[1]["json"]
        assert payload["event"] == "presence_enter_room"
        assert payload["user_id"] == 1
        assert payload["room_id"] == 10

        # Cleanup
        mod._client = None

    @pytest.mark.asyncio
    async def test_dispatch_includes_secret_header(self):
        """When secret configured, X-Webhook-Secret header is sent."""
        mock_settings = type("S", (), {
            "presence_webhook_url": "http://n8n.local/webhook/presence",
            "presence_webhook_secret": "my-secret-token",
        })()

        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = lambda: None

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch("services.presence_webhook.settings", mock_settings):
            import services.presence_webhook as mod
            mod._client = mock_client

            await mod._dispatch("presence_leave_room", user_id=1, room_id=10)

        call_args = mock_client.post.call_args
        headers = call_args[1]["headers"]
        assert headers["X-Webhook-Secret"] == "my-secret-token"

        # Cleanup
        mod._client = None

    @pytest.mark.asyncio
    async def test_dispatch_error_logged_not_raised(self):
        """Network error doesn't propagate (fire-and-forget)."""
        mock_settings = type("S", (), {
            "presence_webhook_url": "http://n8n.local/webhook/presence",
            "presence_webhook_secret": "",
        })()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=Exception("Connection refused"))

        with patch("services.presence_webhook.settings", mock_settings):
            import services.presence_webhook as mod
            mod._client = mock_client

            # Should not raise
            await mod._dispatch("presence_enter_room", user_id=1, room_id=10)

        # Cleanup
        mod._client = None

    def test_register_skipped_when_no_url(self):
        """No URL configured → no hooks registered."""
        mock_settings = type("S", (), {
            "presence_webhook_url": "",
            "presence_webhook_secret": "",
        })()

        with patch("services.presence_webhook.settings", mock_settings), \
             patch("services.presence_webhook.register_hook") as mock_register:
            import services.presence_webhook as mod
            mod.register_presence_webhooks()

        mock_register.assert_not_called()
