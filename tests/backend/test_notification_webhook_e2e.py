"""
E2E Tests for Notification Webhook Endpoint

Tests the full HTTP flow: POST /api/notifications/webhook → DB → response.
Uses httpx AsyncClient against the real FastAPI app with test database.

Mock strategy:
- _deliver always mocked (no real device manager / WebSocket)
- LLM mocked for enrichment and auto-urgency tests
- Dual-patch of settings at route-level + service-level for feature toggles
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import (
    NOTIFICATION_DELIVERED,
    SETTING_NOTIFICATION_WEBHOOK_TOKEN,
    Notification,
    Room,
    SystemSetting,
)

# ============================================================================
# Fixtures
# ============================================================================

WEBHOOK_URL = "/api/notifications/webhook"
TEST_TOKEN = "e2e-test-webhook-token-xyz789"


@pytest.fixture
async def e2e_webhook_token(db_session: AsyncSession) -> str:
    """Store a webhook token in SystemSetting for E2E tests."""
    setting = SystemSetting(
        key=SETTING_NOTIFICATION_WEBHOOK_TOKEN,
        value=TEST_TOKEN,
    )
    db_session.add(setting)
    await db_session.commit()
    return TEST_TOKEN


@pytest.fixture
async def e2e_room(db_session: AsyncSession) -> Room:
    """Create a room for webhook routing tests."""
    room = Room(name="Schlafzimmer", alias="schlafzimmer", source="renfield")
    db_session.add(room)
    await db_session.commit()
    await db_session.refresh(room)
    return room


def _auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _briefing_payload(**overrides) -> dict:
    payload = {
        "event_type": "scheduled.morning_briefing",
        "title": "Morgenbriefing",
        "message": "Wetter: sonnig, 18°C. Innentemperatur: 22°C.",
        "urgency": "info",
        "room": "Schlafzimmer",
        "tts": False,
        "data": {"indoor_temp": "22", "weather": "sonnig"},
    }
    payload.update(overrides)
    return payload


# ============================================================================
# E2E Webhook Tests
# ============================================================================

class TestWebhookE2E:
    """End-to-end tests for POST /api/notifications/webhook."""

    @pytest.mark.integration
    async def test_full_webhook_flow(
        self, async_client: AsyncClient, e2e_webhook_token: str, e2e_room: Room,
    ):
        """POST → 200, notification_id present, status=delivered."""
        with patch(
            "services.notification_service.NotificationService._deliver",
            new_callable=AsyncMock,
            return_value=[],
        ):
            response = await async_client.post(
                WEBHOOK_URL,
                json=_briefing_payload(),
                headers=_auth_header(e2e_webhook_token),
            )

        assert response.status_code == 200
        data = response.json()
        assert data["notification_id"] > 0
        assert data["status"] == NOTIFICATION_DELIVERED

    @pytest.mark.integration
    async def test_notification_fields_in_db(
        self,
        async_client: AsyncClient,
        db_session: AsyncSession,
        e2e_webhook_token: str,
        e2e_room: Room,
    ):
        """All DB fields match the webhook payload."""
        payload = _briefing_payload(
            data={"indoor_temp": "22", "outdoor_temp": "18"},
        )

        with patch(
            "services.notification_service.NotificationService._deliver",
            new_callable=AsyncMock,
            return_value=["device-1"],
        ):
            response = await async_client.post(
                WEBHOOK_URL,
                json=payload,
                headers=_auth_header(e2e_webhook_token),
            )

        nid = response.json()["notification_id"]
        result = await db_session.execute(
            select(Notification).where(Notification.id == nid)
        )
        n = result.scalar_one()

        assert n.event_type == payload["event_type"]
        assert n.title == payload["title"]
        assert n.message == payload["message"]
        assert n.urgency == payload["urgency"]
        assert n.room_name == "Schlafzimmer"
        assert n.room_id == e2e_room.id
        assert n.source_data == payload["data"]
        assert n.dedup_key is not None
        assert n.status == NOTIFICATION_DELIVERED

    @pytest.mark.integration
    async def test_dedup_rejects_duplicate(
        self, async_client: AsyncClient, e2e_webhook_token: str, e2e_room: Room,
    ):
        """Second identical POST → 429."""
        payload = _briefing_payload(
            event_type="scheduled.dedup_test",
            title="Dedup Test",
            message="This should be deduplicated on second call.",
        )

        with patch(
            "services.notification_service.NotificationService._deliver",
            new_callable=AsyncMock,
            return_value=[],
        ):
            first = await async_client.post(
                WEBHOOK_URL,
                json=payload,
                headers=_auth_header(e2e_webhook_token),
            )
            assert first.status_code == 200

            second = await async_client.post(
                WEBHOOK_URL,
                json=payload,
                headers=_auth_header(e2e_webhook_token),
            )

        assert second.status_code == 429

    @pytest.mark.integration
    async def test_503_when_disabled(
        self, async_client: AsyncClient, e2e_webhook_token: str,
    ):
        """proactive_enabled=false → 503."""
        with patch("api.routes.notifications.settings") as mock_settings:
            mock_settings.proactive_enabled = False
            mock_settings.api_rate_limit_default = "100/minute"

            response = await async_client.post(
                WEBHOOK_URL,
                json=_briefing_payload(),
                headers=_auth_header(e2e_webhook_token),
            )

        assert response.status_code == 503

    @pytest.mark.integration
    async def test_401_missing_auth(self, async_client: AsyncClient):
        """No Authorization header → 401."""
        with patch(
            "api.routes.notifications.settings"
        ) as mock_route_settings:
            mock_route_settings.proactive_enabled = True
            mock_route_settings.api_rate_limit_default = "100/minute"

            response = await async_client.post(
                WEBHOOK_URL,
                json=_briefing_payload(),
            )

        assert response.status_code == 401

    @pytest.mark.integration
    async def test_403_invalid_token(
        self, async_client: AsyncClient, e2e_webhook_token: str,
    ):
        """Wrong Bearer token → 403."""
        response = await async_client.post(
            WEBHOOK_URL,
            json=_briefing_payload(),
            headers=_auth_header("wrong-token-absolutely-invalid"),
        )

        assert response.status_code == 403

    @pytest.mark.integration
    async def test_enrichment_stores_original(
        self,
        async_client: AsyncClient,
        db_session: AsyncSession,
        e2e_webhook_token: str,
        e2e_room: Room,
    ):
        """enrich=true → original_message stored, enriched message from mocked LLM."""
        mock_llm_response = MagicMock()
        mock_llm_response.response = "Guten Morgen! Draußen ist es sonnig bei 18 Grad."
        mock_llm_client = AsyncMock()
        mock_llm_client.generate = AsyncMock(return_value=mock_llm_response)

        payload = _briefing_payload(enrich=True)
        original_msg = payload["message"]

        with patch(
            "services.notification_service.NotificationService._deliver",
            new_callable=AsyncMock,
            return_value=[],
        ), patch(
            "services.notification_service.settings",
        ) as mock_svc_settings, patch(
            "utils.llm_client.get_default_client",
            return_value=mock_llm_client,
        ):
            mock_svc_settings.proactive_tts_default = False
            mock_svc_settings.proactive_suppression_window = 60
            mock_svc_settings.proactive_notification_ttl = 86400
            mock_svc_settings.proactive_semantic_dedup_enabled = False
            mock_svc_settings.proactive_feedback_learning_enabled = False
            mock_svc_settings.proactive_enrichment_enabled = True
            mock_svc_settings.proactive_enrichment_model = None
            mock_svc_settings.proactive_urgency_auto_enabled = False
            mock_svc_settings.ollama_model = "test-model"

            response = await async_client.post(
                WEBHOOK_URL,
                json=payload,
                headers=_auth_header(e2e_webhook_token),
            )

        assert response.status_code == 200
        nid = response.json()["notification_id"]

        result = await db_session.execute(
            select(Notification).where(Notification.id == nid)
        )
        n = result.scalar_one()

        assert n.enriched is True
        assert n.original_message == original_msg
        assert n.message == "Guten Morgen! Draußen ist es sonnig bei 18 Grad."

    @pytest.mark.integration
    async def test_auto_urgency(
        self,
        async_client: AsyncClient,
        db_session: AsyncSession,
        e2e_webhook_token: str,
        e2e_room: Room,
    ):
        """urgency=auto → LLM classifies, urgency_auto=True in DB."""
        mock_llm_response = MagicMock()
        mock_llm_response.response = "critical"
        mock_llm_client = AsyncMock()
        mock_llm_client.generate = AsyncMock(return_value=mock_llm_response)

        payload = _briefing_payload(
            event_type="security.motion_detected",
            title="Bewegung erkannt",
            message="Bewegung an der Haustür erkannt.",
            urgency="auto",
        )

        with patch(
            "services.notification_service.NotificationService._deliver",
            new_callable=AsyncMock,
            return_value=[],
        ), patch(
            "services.notification_service.settings",
        ) as mock_svc_settings, patch(
            "utils.llm_client.get_default_client",
            return_value=mock_llm_client,
        ):
            mock_svc_settings.proactive_tts_default = False
            mock_svc_settings.proactive_suppression_window = 60
            mock_svc_settings.proactive_notification_ttl = 86400
            mock_svc_settings.proactive_semantic_dedup_enabled = False
            mock_svc_settings.proactive_feedback_learning_enabled = False
            mock_svc_settings.proactive_enrichment_enabled = False
            mock_svc_settings.proactive_urgency_auto_enabled = True
            mock_svc_settings.proactive_enrichment_model = None
            mock_svc_settings.ollama_model = "test-model"

            response = await async_client.post(
                WEBHOOK_URL,
                json=payload,
                headers=_auth_header(e2e_webhook_token),
            )

        assert response.status_code == 200
        nid = response.json()["notification_id"]

        result = await db_session.execute(
            select(Notification).where(Notification.id == nid)
        )
        n = result.scalar_one()

        assert n.urgency == "critical"
        assert n.urgency_auto is True
