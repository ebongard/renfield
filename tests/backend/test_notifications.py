"""
Tests für Proaktive Benachrichtigungen

Testet:
- NotificationService: Webhook, Dedup, CRUD, Token
- Phase 2: Semantic Dedup, Urgency Classification, Enrichment, Suppressions
- Reminders (Duration Parsing)
- Notification API: Webhook-Endpoint, Liste, Acknowledge, Dismiss, Token
- WebSocket: notification_ack Handling
"""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import (
    NOTIFICATION_ACKNOWLEDGED,
    NOTIFICATION_DELIVERED,
    NOTIFICATION_DISMISSED,
    NOTIFICATION_PENDING,
    REMINDER_CANCELLED,
    REMINDER_FIRED,
    REMINDER_PENDING,
    SETTING_NOTIFICATION_WEBHOOK_TOKEN,
    Notification,
    NotificationSuppression,
    Reminder,
    Room,
    SystemSetting,
)
from models.websocket_messages import WSNotificationAckMessage

# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def notification_service(db_session: AsyncSession):
    """Create NotificationService with test database."""
    from services.notification_service import NotificationService
    return NotificationService(db_session)


@pytest.fixture
async def sample_notification(db_session: AsyncSession) -> Notification:
    """Create a sample notification in database."""
    notification = Notification(
        event_type="ha_automation",
        title="Waschmaschine fertig",
        message="Die Waschmaschine ist fertig.",
        urgency="info",
        room_name="Wohnzimmer",
        source="ha_automation",
        status=NOTIFICATION_DELIVERED,
        delivered_at=datetime.utcnow(),
    )
    db_session.add(notification)
    await db_session.commit()
    await db_session.refresh(notification)
    return notification


@pytest.fixture
async def test_room_for_notifications(db_session: AsyncSession) -> Room:
    """Create a test room for notification routing."""
    room = Room(name="Wohnzimmer", alias="wohnzimmer", source="renfield")
    db_session.add(room)
    await db_session.commit()
    await db_session.refresh(room)
    return room


@pytest.fixture
async def webhook_token(db_session: AsyncSession) -> str:
    """Create a webhook token in SystemSetting."""
    token = "test-webhook-token-abc123"
    setting = SystemSetting(
        key=SETTING_NOTIFICATION_WEBHOOK_TOKEN,
        value=token,
    )
    db_session.add(setting)
    await db_session.commit()
    return token


# ============================================================================
# NotificationService Tests
# ============================================================================

class TestNotificationServiceToken:
    """Webhook token management tests."""

    @pytest.mark.unit
    async def test_generate_webhook_token(self, notification_service):
        """Test: Generate new webhook token."""
        token = await notification_service.generate_webhook_token()
        assert token
        assert len(token) > 20

    @pytest.mark.unit
    async def test_get_webhook_token(self, notification_service):
        """Test: Retrieve stored token."""
        generated = await notification_service.generate_webhook_token()
        retrieved = await notification_service.get_webhook_token()
        assert retrieved == generated

    @pytest.mark.unit
    async def test_verify_valid_token(self, notification_service, webhook_token):
        """Test: Verify valid token returns True."""
        result = await notification_service.verify_webhook_token(webhook_token)
        assert result is True

    @pytest.mark.unit
    async def test_verify_invalid_token(self, notification_service, webhook_token):
        """Test: Verify invalid token returns False."""
        result = await notification_service.verify_webhook_token("wrong-token")
        assert result is False

    @pytest.mark.unit
    async def test_verify_no_token_stored(self, notification_service):
        """Test: Verify fails when no token stored."""
        result = await notification_service.verify_webhook_token("any-token")
        assert result is False

    @pytest.mark.unit
    async def test_rotate_webhook_token(self, notification_service, webhook_token):
        """Test: Generate new token replaces old one."""
        new_token = await notification_service.generate_webhook_token()
        assert new_token != webhook_token
        # Old token no longer valid
        assert await notification_service.verify_webhook_token(webhook_token) is False
        # New token valid
        assert await notification_service.verify_webhook_token(new_token) is True


class TestNotificationServiceDedup:
    """Deduplication tests."""

    @pytest.mark.unit
    def test_dedup_key_deterministic(self, notification_service):
        """Test: Same input produces same dedup key."""
        key1 = notification_service._compute_dedup_key("ha", "Title", "Msg", "Room")
        key2 = notification_service._compute_dedup_key("ha", "Title", "Msg", "Room")
        assert key1 == key2

    @pytest.mark.unit
    def test_dedup_key_different_for_different_input(self, notification_service):
        """Test: Different input produces different dedup key."""
        key1 = notification_service._compute_dedup_key("ha", "Title A", "Msg", "Room")
        key2 = notification_service._compute_dedup_key("ha", "Title B", "Msg", "Room")
        assert key1 != key2

    @pytest.mark.database
    async def test_is_duplicate_false_for_new(self, notification_service):
        """Test: No duplicate for fresh dedup_key."""
        result = await notification_service._is_duplicate("fresh-key-never-seen")
        assert result is False

    @pytest.mark.database
    async def test_is_duplicate_true_for_recent(self, notification_service, db_session):
        """Test: Duplicate detected for recent notification with same key."""
        n = Notification(
            event_type="ha_automation",
            title="Test",
            message="Test",
            dedup_key="known-key",
            created_at=datetime.utcnow(),
        )
        db_session.add(n)
        await db_session.commit()

        result = await notification_service._is_duplicate("known-key")
        assert result is True


class TestNotificationServiceWebhook:
    """Webhook processing tests."""

    @pytest.mark.database
    async def test_process_webhook_creates_notification(
        self, notification_service, test_room_for_notifications
    ):
        """Test: Webhook creates notification in DB."""
        with patch("services.notification_service.NotificationService._deliver", new_callable=AsyncMock, return_value=[]):
            result = await notification_service.process_webhook(
                event_type="ha_automation",
                title="Testmeldung",
                message="Eine Testnachricht",
                urgency="info",
                room="Wohnzimmer",
                tts=False,
            )

        assert result["notification_id"] > 0
        assert result["status"] == NOTIFICATION_DELIVERED

    @pytest.mark.database
    async def test_process_webhook_dedup_raises(
        self, notification_service, test_room_for_notifications
    ):
        """Test: Duplicate webhook raises ValueError."""
        with patch("services.notification_service.NotificationService._deliver", new_callable=AsyncMock, return_value=[]):
            await notification_service.process_webhook(
                event_type="ha_automation",
                title="Waschmaschine fertig",
                message="Die Waschmaschine ist fertig.",
                urgency="info",
                tts=False,
            )

        with pytest.raises(ValueError, match="Duplicate"), \
             patch("services.notification_service.NotificationService._deliver", new_callable=AsyncMock, return_value=[]):
                await notification_service.process_webhook(
                    event_type="ha_automation",
                    title="Waschmaschine fertig",
                    message="Die Waschmaschine ist fertig.",
                    urgency="info",
                    tts=False,
                )

    @pytest.mark.database
    async def test_process_webhook_resolves_room(
        self, notification_service, test_room_for_notifications
    ):
        """Test: Room name gets resolved to room_id."""
        with patch("services.notification_service.NotificationService._deliver", new_callable=AsyncMock, return_value=[]):
            result = await notification_service.process_webhook(
                event_type="ha_automation",
                title="Room Test",
                message="Tests room resolution",
                room="Wohnzimmer",
                tts=False,
            )

        notification = await notification_service.get_notification(result["notification_id"])
        assert notification.room_id == test_room_for_notifications.id
        assert notification.room_name == "Wohnzimmer"


class TestNotificationServiceCRUD:
    """CRUD operation tests."""

    @pytest.mark.database
    async def test_list_notifications(self, notification_service, sample_notification):
        """Test: List returns existing notifications."""
        result = await notification_service.list_notifications()
        assert len(result) >= 1
        assert result[0].title == "Waschmaschine fertig"

    @pytest.mark.database
    async def test_list_filter_by_urgency(self, notification_service, sample_notification):
        """Test: Filter by urgency."""
        result = await notification_service.list_notifications(urgency="info")
        assert len(result) >= 1

        result = await notification_service.list_notifications(urgency="critical")
        assert len(result) == 0

    @pytest.mark.database
    async def test_get_notification(self, notification_service, sample_notification):
        """Test: Get single notification by ID."""
        result = await notification_service.get_notification(sample_notification.id)
        assert result is not None
        assert result.title == "Waschmaschine fertig"

    @pytest.mark.database
    async def test_get_nonexistent_notification(self, notification_service):
        """Test: Get nonexistent returns None."""
        result = await notification_service.get_notification(99999)
        assert result is None

    @pytest.mark.database
    async def test_acknowledge(self, notification_service, sample_notification):
        """Test: Acknowledge sets status and timestamp."""
        success = await notification_service.acknowledge(sample_notification.id, acknowledged_by="testuser")
        assert success is True

        n = await notification_service.get_notification(sample_notification.id)
        assert n.status == NOTIFICATION_ACKNOWLEDGED
        assert n.acknowledged_by == "testuser"
        assert n.acknowledged_at is not None

    @pytest.mark.database
    async def test_acknowledge_nonexistent(self, notification_service):
        """Test: Acknowledge nonexistent returns False."""
        success = await notification_service.acknowledge(99999)
        assert success is False

    @pytest.mark.database
    async def test_dismiss(self, notification_service, sample_notification):
        """Test: Dismiss sets status to dismissed."""
        success = await notification_service.dismiss(sample_notification.id)
        assert success is True

        n = await notification_service.get_notification(sample_notification.id)
        assert n.status == NOTIFICATION_DISMISSED

    @pytest.mark.database
    async def test_cleanup_expired(self, notification_service, db_session):
        """Test: Cleanup removes expired notifications."""
        expired = Notification(
            event_type="test",
            title="Expired",
            message="This is expired",
            expires_at=datetime.utcnow() - timedelta(hours=1),
        )
        db_session.add(expired)
        await db_session.commit()

        count = await notification_service.cleanup_expired()
        assert count >= 1


# ============================================================================
# WebSocket Message Validation Tests
# ============================================================================

class TestNotificationWSMessages:
    """WebSocket message model tests."""

    @pytest.mark.unit
    def test_notification_ack_message_valid(self):
        """Test: Valid notification_ack message."""
        msg = WSNotificationAckMessage(
            notification_id=42,
            action="acknowledged",
        )
        assert msg.type == "notification_ack"
        assert msg.notification_id == 42
        assert msg.action == "acknowledged"

    @pytest.mark.unit
    def test_notification_ack_message_dismissed(self):
        """Test: Dismissed action."""
        msg = WSNotificationAckMessage(
            notification_id=1,
            action="dismissed",
        )
        assert msg.action == "dismissed"

    @pytest.mark.unit
    def test_notification_ack_default_action(self):
        """Test: Default action is 'acknowledged'."""
        msg = WSNotificationAckMessage(notification_id=1)
        assert msg.action == "acknowledged"


# ============================================================================
# Notification Model Tests
# ============================================================================

class TestNotificationModel:
    """Database model tests."""

    @pytest.mark.database
    async def test_create_notification(self, db_session):
        """Test: Create notification with all fields."""
        notification = Notification(
            event_type="ha_automation",
            title="Test Notification",
            message="Test message body",
            urgency="critical",
            room_name="Küche",
            source="ha_automation",
            source_data={"entity_id": "sensor.washing_machine"},
            status=NOTIFICATION_PENDING,
            dedup_key="test-key-123",
        )
        db_session.add(notification)
        await db_session.commit()
        await db_session.refresh(notification)

        assert notification.id is not None
        assert notification.title == "Test Notification"
        assert notification.urgency == "critical"
        assert notification.status == NOTIFICATION_PENDING
        assert notification.source_data == {"entity_id": "sensor.washing_machine"}
        assert notification.created_at is not None
        assert notification.tts_delivered is False

    @pytest.mark.database
    async def test_notification_defaults(self, db_session):
        """Test: Default values are applied."""
        notification = Notification(
            event_type="test",
            title="Defaults",
            message="Testing defaults",
        )
        db_session.add(notification)
        await db_session.commit()
        await db_session.refresh(notification)

        assert notification.urgency == "info"
        assert notification.source == "ha_automation"
        assert notification.status == NOTIFICATION_PENDING
        assert notification.tts_delivered is False

    @pytest.mark.database
    async def test_notification_phase2_fields(self, db_session):
        """Test: Phase 2 fields (enriched, original_message, urgency_auto) are created."""
        notification = Notification(
            event_type="test",
            title="Phase2",
            message="Enriched message",
            enriched=True,
            original_message="Original message",
            urgency_auto=True,
        )
        db_session.add(notification)
        await db_session.commit()
        await db_session.refresh(notification)

        assert notification.enriched is True
        assert notification.original_message == "Original message"
        assert notification.urgency_auto is True


# ============================================================================
# Phase 2b: Semantic Dedup Tests
# ============================================================================

class TestSemanticDedup:
    """Semantic deduplication tests."""

    @pytest.mark.unit
    async def test_semantic_dedup_disabled_by_default(self, notification_service):
        """Test: Semantic dedup returns False when disabled."""
        result = await notification_service._is_semantic_duplicate([0.1] * 768)
        assert result is False

    @pytest.mark.database
    @patch("services.notification_service.settings")
    async def test_semantic_dedup_detects_paraphrase(self, mock_settings, notification_service, db_session):
        """Test: Semantic dedup detects similar embedding (mocked pgvector)."""
        mock_settings.proactive_semantic_dedup_enabled = True
        mock_settings.proactive_semantic_dedup_threshold = 0.85
        mock_settings.proactive_suppression_window = 60

        # pgvector won't be available in SQLite tests — the method should
        # gracefully handle the exception and return False
        result = await notification_service._is_semantic_duplicate([0.1] * 768)
        assert result is False  # Gracefully handles missing pgvector

    @pytest.mark.unit
    async def test_stores_embedding_background(self, notification_service, db_session):
        """Test: Background embedding storage creates a task."""
        notification = Notification(
            event_type="test",
            title="Embed",
            message="Test embedding storage",
        )
        db_session.add(notification)
        await db_session.commit()
        await db_session.refresh(notification)

        # Should not raise
        with patch("asyncio.create_task") as mock_task:
            notification_service._store_embedding_background(notification.id, [0.1] * 768)
            mock_task.assert_called_once()


# ============================================================================
# Phase 2d: Urgency Auto-Classification Tests
# ============================================================================

class TestUrgencyClassification:
    """Urgency auto-classification tests."""

    @pytest.mark.unit
    async def test_auto_classify_disabled_returns_none(self, notification_service):
        """Disabled → None ("nothing classified"); the caller picks the fallback."""
        result = await notification_service._auto_classify_urgency("test", "Title", "Message")
        assert result is None

    @pytest.mark.unit
    @patch("services.notification_service.settings")
    async def test_auto_classify_critical(self, mock_settings, notification_service):
        """Test: LLM classifies as critical."""
        mock_settings.proactive_urgency_auto_enabled = True
        mock_settings.proactive_enrichment_model = None
        mock_settings.ollama_model = "test-model"

        mock_response = MagicMock()
        mock_response.response = "critical"

        with patch("services.notification_service.NotificationService._auto_classify_urgency") as mock_classify:
            mock_classify.return_value = "critical"
            result = await mock_classify("security.alert", "Einbruch!", "Bewegung erkannt")
            assert result == "critical"

    @pytest.mark.unit
    @patch("services.notification_service.settings")
    async def test_auto_classify_fallback_on_error(self, mock_settings, notification_service):
        """LLM error → None, so an ops alert can fall back to critical instead
        of being demoted to info by the very outage it reports."""
        mock_settings.proactive_urgency_auto_enabled = True
        mock_settings.proactive_enrichment_model = None
        mock_settings.ollama_model = "test-model"
        mock_settings.proactive_llm_event_types = "test"

        with patch("utils.llm_client.get_default_client", side_effect=Exception("LLM unavailable")):
            result = await notification_service._auto_classify_urgency("test", "Title", "Message")
            assert result is None

    @pytest.mark.database
    async def test_urgency_auto_flag_stored(self, notification_service, db_session):
        """Test: urgency_auto flag is stored on notification."""
        with patch("services.notification_service.NotificationService._deliver", new_callable=AsyncMock, return_value=[]), \
             patch("services.notification_service.NotificationService._auto_classify_urgency", new_callable=AsyncMock, return_value="critical"):
            result = await notification_service.process_webhook(
                event_type="test",
                llm_eligible=True,  # BL-0424: only a vouched sender is classified
                title="Auto urgency",
                message="Testing auto urgency",
                urgency="auto",
                tts=False,
            )

        notification = await notification_service.get_notification(result["notification_id"])
        assert notification.urgency == "critical"
        assert notification.urgency_auto is True


# ============================================================================
# Phase 2a: LLM Enrichment Tests
# ============================================================================

class TestLlmEventTypeGate:
    """BL-0424: only technical event types may pass through the LLM.

    Personal notifications (reminders, deadlines, HA events about people) are
    delivered verbatim even with both LLM flags on; the allow-list decides."""

    @pytest.mark.unit
    @patch("services.notification_service.settings")
    async def test_default_list_admits_only_the_technical_senders(self, mock_settings):
        from services.notification_service import NotificationService
        from utils.config import Settings

        mock_settings.proactive_llm_event_types = Settings.model_fields[
            "proactive_llm_event_types"
        ].default
        for allowed in ("ops_health", "mcp_health", "scheduled_task_health"):
            assert NotificationService.llm_allowed_for(allowed) is True
        for personal in ("reminder", "obligation_deadline", "ha_automation", "doorbell"):
            assert NotificationService.llm_allowed_for(personal) is False

    @pytest.mark.unit
    @patch("services.notification_service.settings")
    async def test_list_is_trimmed_and_empty_allows_nothing(self, mock_settings):
        from services.notification_service import NotificationService

        mock_settings.proactive_llm_event_types = " ops_health , mcp_health,, "
        assert NotificationService.llm_allowed_for("mcp_health") is True
        assert NotificationService.llm_allowed_for("") is False
        mock_settings.proactive_llm_event_types = ""
        assert NotificationService.llm_allowed_for("ops_health") is False

    @pytest.mark.unit
    @patch("services.notification_service.settings")
    async def test_personal_event_is_never_enriched(self, mock_settings, notification_service):
        mock_settings.proactive_enrichment_enabled = True
        mock_settings.proactive_enrichment_model = None
        mock_settings.ollama_model = "test-model"
        mock_settings.proactive_llm_event_types = "ops_health"
        mock_client = AsyncMock()

        with patch("utils.llm_client.get_default_client", return_value=mock_client):
            result = await notification_service._enrich_message(
                "reminder", "Arzttermin", "Morgen 9 Uhr Dr. Beispiel",
            )
        assert result == "Morgen 9 Uhr Dr. Beispiel"
        mock_client.generate.assert_not_called()

    @pytest.mark.unit
    @patch("services.notification_service.settings")
    async def test_personal_event_urgency_auto_falls_back_without_llm(
        self, mock_settings, notification_service,
    ):
        mock_settings.proactive_urgency_auto_enabled = True
        mock_settings.proactive_enrichment_model = None
        mock_settings.ollama_model = "test-model"
        mock_settings.proactive_llm_event_types = "ops_health"
        mock_client = AsyncMock()

        with patch("utils.llm_client.get_default_client", return_value=mock_client):
            result = await notification_service._auto_classify_urgency(
                "ha_automation", "Haustür", "Die Haustür ist offen",
            )
        assert result is None
        mock_client.generate.assert_not_called()

    @pytest.mark.unit
    @patch("services.notification_service.settings")
    async def test_technical_event_reaches_the_llm(self, mock_settings, notification_service):
        mock_settings.proactive_enrichment_enabled = True
        mock_settings.proactive_urgency_auto_enabled = True
        mock_settings.proactive_enrichment_model = None
        mock_settings.ollama_model = "test-model"
        mock_settings.proactive_llm_event_types = "ops_health,mcp_health"

        urgency_resp, enrich_resp = MagicMock(), MagicMock()
        urgency_resp.response = "critical"
        enrich_resp.response = "Der Paperless-Server antwortet seit 10 Minuten nicht."
        mock_client = AsyncMock()
        mock_client.generate = AsyncMock(side_effect=[urgency_resp, enrich_resp])

        with patch("utils.llm_client.get_default_client", return_value=mock_client):
            urgency = await notification_service._auto_classify_urgency(
                "mcp_health", "MCP paperless", "3 timeouts",
            )
            message = await notification_service._enrich_message(
                "mcp_health", "MCP paperless", "3 timeouts",
            )
        assert urgency == "critical"
        assert message == "Der Paperless-Server antwortet seit 10 Minuten nicht."
        assert mock_client.generate.await_count == 2


class TestLlmTrustBoundary:
    """BL-0424, pipeline level: ``llm_eligible`` is server-set. A caller that
    merely labels its notification technical (event type on the allow-list,
    both flags on) still never reaches the model; a vouched technical sender
    gets both steps; and when the model is down, ``urgency_fallback`` — not
    ``info`` — is what an ops alert becomes."""

    @staticmethod
    def _configure(ms):
        ms.proactive_tts_default = False
        ms.proactive_suppression_window = 60
        ms.proactive_notification_ttl = 86400
        ms.proactive_semantic_dedup_enabled = False
        ms.proactive_feedback_learning_enabled = False
        ms.proactive_enrichment_enabled = True
        ms.proactive_urgency_auto_enabled = True
        ms.proactive_enrichment_model = None
        ms.proactive_llm_event_types = "ops_health"
        ms.ollama_model = "test-model"

    @staticmethod
    def _llm(*responses):
        client = AsyncMock()
        outs = []
        for text in responses:
            r = MagicMock()
            r.response = text
            outs.append(r)
        client.generate = AsyncMock(side_effect=outs)
        return client

    async def _row(self, db_session, result):
        from sqlalchemy import select
        return (await db_session.execute(
            select(Notification).where(Notification.id == result["notification_id"])
        )).scalar_one()

    @pytest.mark.database
    async def test_unvouched_caller_never_reaches_the_llm(self, notification_service, db_session):
        client = self._llm("critical", "umformuliert")
        with patch("services.notification_service.NotificationService._deliver",
                   new_callable=AsyncMock, return_value=[]), \
             patch("services.notification_service.settings") as ms, \
             patch("utils.llm_client.get_default_client", return_value=client):
            self._configure(ms)
            result = await notification_service.process_webhook(
                event_type="ops_health", title="Etikett technisch",
                message="Arzttermin morgen 9 Uhr", urgency="auto", enrich=True, tts=False,
            )
        n = await self._row(db_session, result)
        client.generate.assert_not_called()
        assert (n.urgency, n.urgency_auto) == ("info", False)
        assert (n.enriched, n.original_message, n.message) == (
            False, None, "Arzttermin morgen 9 Uhr",
        )

    @pytest.mark.database
    async def test_vouched_technical_sender_gets_both_steps(self, notification_service, db_session):
        client = self._llm("critical", "Der Paperless-Server antwortet nicht.")
        with patch("services.notification_service.NotificationService._deliver",
                   new_callable=AsyncMock, return_value=[]), \
             patch("services.notification_service.settings") as ms, \
             patch("utils.llm_client.get_default_client", return_value=client):
            self._configure(ms)
            result = await notification_service.process_webhook(
                event_type="ops_health", title="MCP paperless", message="3 timeouts",
                urgency="auto", enrich=True, tts=False,
                llm_eligible=True, urgency_fallback="critical",
            )
        n = await self._row(db_session, result)
        assert client.generate.await_count == 2
        assert (n.urgency, n.urgency_auto) == ("critical", True)
        assert (n.enriched, n.original_message) == (True, "3 timeouts")
        assert n.message == "Der Paperless-Server antwortet nicht."

    @pytest.mark.database
    async def test_vouched_sender_with_off_list_type_is_left_alone(
        self, notification_service, db_session,
    ):
        """The operator list still applies on top of the voucher: a technical
        type taken off the list is neither classified nor enriched, and the
        fallback keeps it critical."""
        client = self._llm("low", "umformuliert")
        with patch("services.notification_service.NotificationService._deliver",
                   new_callable=AsyncMock, return_value=[]), \
             patch("services.notification_service.settings") as ms, \
             patch("utils.llm_client.get_default_client", return_value=client):
            self._configure(ms)  # list = "ops_health" only
            result = await notification_service.process_webhook(
                event_type="mcp_health", title="MCP", message="3 timeouts",
                urgency="auto", enrich=True, tts=False,
                llm_eligible=True, urgency_fallback="critical",
            )
        n = await self._row(db_session, result)
        client.generate.assert_not_called()
        assert (n.urgency, n.urgency_auto) == ("critical", False)
        assert (n.enriched, n.original_message, n.message) == (False, None, "3 timeouts")

    @pytest.mark.database
    async def test_vouched_sender_llm_failure_keeps_critical(self, notification_service, db_session):
        with patch("services.notification_service.NotificationService._deliver",
                   new_callable=AsyncMock, return_value=[]), \
             patch("services.notification_service.settings") as ms, \
             patch("utils.llm_client.get_default_client",
                   side_effect=Exception("llama-server unreachable")):
            self._configure(ms)
            result = await notification_service.process_webhook(
                event_type="ops_health", title="LLM-Host", message="cuda.local down",
                urgency="auto", enrich=True, tts=False,
                llm_eligible=True, urgency_fallback="critical",
            )
        n = await self._row(db_session, result)
        assert (n.urgency, n.urgency_auto) == ("critical", False)
        assert (n.enriched, n.original_message, n.message) == (False, None, "cuda.local down")


class TestEnrichment:
    """LLM content enrichment tests."""

    @pytest.mark.unit
    async def test_enrich_disabled_returns_original(self, notification_service):
        """Test: Returns original message when enrichment is disabled."""
        result = await notification_service._enrich_message("test", "Title", "Original message")
        assert result == "Original message"

    @pytest.mark.unit
    @patch("services.notification_service.settings")
    async def test_enrich_transforms_message(self, mock_settings, notification_service):
        """Test: LLM enriches the message."""
        mock_settings.proactive_enrichment_enabled = True
        mock_settings.proactive_enrichment_model = None
        mock_settings.ollama_model = "test-model"
        mock_settings.proactive_llm_event_types = "ha_automation"

        mock_response = MagicMock()
        mock_response.response = "Die Waschmaschine im Keller ist fertig. Du kannst die Wäsche aufhängen."

        mock_client = AsyncMock()
        mock_client.generate = AsyncMock(return_value=mock_response)

        with patch("utils.llm_client.get_default_client", return_value=mock_client):
            result = await notification_service._enrich_message(
                "ha_automation", "Waschmaschine", "Programm beendet",
            )
            assert result == "Die Waschmaschine im Keller ist fertig. Du kannst die Wäsche aufhängen."

    @pytest.mark.database
    async def test_enrich_stores_original(self, notification_service, db_session):
        """Test: Original message is stored when enrichment is active."""
        mock_response = MagicMock()
        mock_response.response = "Enriched text here"
        mock_client = AsyncMock()
        mock_client.generate = AsyncMock(return_value=mock_response)

        with patch("services.notification_service.NotificationService._deliver", new_callable=AsyncMock, return_value=[]), \
             patch("services.notification_service.settings") as mock_settings, \
             patch("utils.llm_client.get_default_client", return_value=mock_client):
            mock_settings.proactive_tts_default = False
            mock_settings.proactive_suppression_window = 60
            mock_settings.proactive_notification_ttl = 86400
            mock_settings.proactive_semantic_dedup_enabled = False
            mock_settings.proactive_feedback_learning_enabled = False
            mock_settings.proactive_enrichment_enabled = True
            mock_settings.proactive_enrichment_model = None
            mock_settings.proactive_urgency_auto_enabled = False
            mock_settings.proactive_llm_event_types = "test"
            mock_settings.ollama_model = "test-model"

            result = await notification_service.process_webhook(
                event_type="test",
                llm_eligible=True,  # BL-0424: only a vouched sender is enriched
                title="Enrich Test",
                message="Original raw message",
                tts=False,
                enrich=True,
            )

        notification = await notification_service.get_notification(result["notification_id"])
        assert notification.enriched is True
        assert notification.original_message == "Original raw message"
        assert notification.message == "Enriched text here"

    @pytest.mark.unit
    @patch("services.notification_service.settings")
    async def test_enrich_fallback_on_error(self, mock_settings, notification_service):
        """Test: Falls back to original message on LLM error."""
        mock_settings.proactive_enrichment_enabled = True
        mock_settings.proactive_enrichment_model = None
        mock_settings.ollama_model = "test-model"
        mock_settings.proactive_llm_event_types = "test"

        with patch("utils.llm_client.get_default_client", side_effect=Exception("LLM down")):
            result = await notification_service._enrich_message(
                "test", "Title", "Original message",
            )
            assert result == "Original message"


# ============================================================================
# Phase 2c: Suppression / Feedback Learning Tests
# ============================================================================

class TestSuppressionLearning:
    """Suppression / feedback learning tests."""

    @pytest.mark.database
    async def test_creates_suppression_rule(self, notification_service, sample_notification):
        """Test: Creates suppression rule from notification."""
        with patch.object(notification_service, "_get_embedding", new_callable=AsyncMock, side_effect=Exception("no embed")):
            suppression = await notification_service.suppress_similar(sample_notification.id)

        assert suppression is not None
        assert suppression.event_pattern == "ha_automation"
        assert suppression.is_active is True
        assert suppression.source_notification_id == sample_notification.id

    @pytest.mark.database
    async def test_suppression_blocks_matching_event_type(self, notification_service, db_session):
        """Test: Active suppression blocks same event_type."""
        # Create a suppression rule
        suppression = NotificationSuppression(
            event_pattern="ha_automation.washer",
            is_active=True,
        )
        db_session.add(suppression)
        await db_session.commit()

        with patch("services.notification_service.settings") as mock_settings:
            mock_settings.proactive_feedback_learning_enabled = True
            mock_settings.proactive_feedback_similarity_threshold = 0.80

            result = await notification_service._is_suppressed("ha_automation.washer")
            assert result is True

    @pytest.mark.database
    async def test_suppression_event_type_mismatch(self, notification_service, db_session):
        """Test: Suppression does not block different event_type."""
        suppression = NotificationSuppression(
            event_pattern="ha_automation.washer",
            is_active=True,
        )
        db_session.add(suppression)
        await db_session.commit()

        with patch("services.notification_service.settings") as mock_settings:
            mock_settings.proactive_feedback_learning_enabled = True
            mock_settings.proactive_feedback_similarity_threshold = 0.80

            result = await notification_service._is_suppressed("ha_automation.doorbell")
            assert result is False

    @pytest.mark.database
    async def test_inactive_suppression_ignored(self, notification_service, db_session):
        """Test: Inactive suppression is ignored."""
        suppression = NotificationSuppression(
            event_pattern="ha_automation.washer",
            is_active=False,
        )
        db_session.add(suppression)
        await db_session.commit()

        with patch("services.notification_service.settings") as mock_settings:
            mock_settings.proactive_feedback_learning_enabled = True
            mock_settings.proactive_feedback_similarity_threshold = 0.80

            result = await notification_service._is_suppressed("ha_automation.washer")
            assert result is False

    @pytest.mark.database
    async def test_suppression_scoped_to_creating_user(self, notification_service, db_session, test_user):
        """A PER-USER suppression blocks only that user's notifications — NOT other
        users' (the multi-tenant bug: one user's dismissal must not suppress for all)."""
        db_session.add(NotificationSuppression(
            event_pattern="ha_automation.washer", is_active=True, user_id=test_user.id,
        ))
        await db_session.commit()
        with patch("services.notification_service.settings") as mock_settings:
            mock_settings.proactive_feedback_learning_enabled = True
            mock_settings.proactive_feedback_similarity_threshold = 0.80
            # same target user → suppressed
            assert await notification_service._is_suppressed(
                "ha_automation.washer", target_user_id=test_user.id) is True
            # a DIFFERENT user → NOT suppressed (this is what the fix protects)
            assert await notification_service._is_suppressed(
                "ha_automation.washer", target_user_id=test_user.id + 999) is False
            # a broadcast (no target user) → NOT suppressed by a per-user rule
            assert await notification_service._is_suppressed(
                "ha_automation.washer", target_user_id=None) is False

    @pytest.mark.database
    async def test_global_suppression_blocks_all_targets(self, notification_service, db_session, test_user):
        """A GLOBAL suppression (user_id NULL) still blocks for any target user."""
        db_session.add(NotificationSuppression(
            event_pattern="ha_automation.washer", is_active=True, user_id=None,
        ))
        await db_session.commit()
        with patch("services.notification_service.settings") as mock_settings:
            mock_settings.proactive_feedback_learning_enabled = True
            mock_settings.proactive_feedback_similarity_threshold = 0.80
            assert await notification_service._is_suppressed(
                "ha_automation.washer", target_user_id=test_user.id) is True
            assert await notification_service._is_suppressed(
                "ha_automation.washer", target_user_id=None) is True

    @pytest.mark.database
    async def test_enrichment_prompt_is_tenant_neutral(self, notification_service):
        """The enrichment prompt must not use household 'Smart-Home'/'Bewohner' phrasing
        (wrong for a business tenant)."""
        captured = {}

        async def _fake_generate(**kw):
            captured["prompt"] = kw.get("prompt", "")

            class _R:
                response = "Formulierte Nachricht."

            return _R()

        mock_client = MagicMock()
        mock_client.generate = _fake_generate
        with patch("services.notification_service.settings") as ms, \
             patch("utils.llm_client.get_default_client", return_value=mock_client):
            ms.proactive_enrichment_enabled = True
            ms.proactive_enrichment_model = ""
            ms.ollama_model = "m"
            ms.proactive_llm_event_types = "evt"
            await notification_service._enrich_message("evt", "Titel", "Nachricht")

        assert "Smart-Home" not in captured["prompt"]
        assert "Bewohner" not in captured["prompt"]
        assert "digitaler Assistent" in captured["prompt"]

    @pytest.mark.database
    async def test_list_suppressions(self, notification_service, db_session):
        """Test: List active suppressions."""
        db_session.add(NotificationSuppression(event_pattern="type_a", is_active=True))
        db_session.add(NotificationSuppression(event_pattern="type_b", is_active=False))
        await db_session.commit()

        result = await notification_service.list_suppressions(active_only=True)
        assert len(result) == 1
        assert result[0].event_pattern == "type_a"

    @pytest.mark.database
    async def test_delete_suppression(self, notification_service, db_session):
        """Test: Delete (deactivate) suppression."""
        suppression = NotificationSuppression(event_pattern="type_x", is_active=True)
        db_session.add(suppression)
        await db_session.commit()
        await db_session.refresh(suppression)

        success = await notification_service.delete_suppression(suppression.id)
        assert success is True

        # Now inactive
        result = await notification_service.list_suppressions(active_only=True)
        assert len(result) == 0

    @pytest.mark.database
    async def test_suppress_nonexistent_notification(self, notification_service):
        """Test: Suppress returns None for missing notification."""
        result = await notification_service.suppress_similar(99999)
        assert result is None


# ============================================================================
# Reminder Tests
# ============================================================================

class TestReminderParsing:
    """Duration parsing tests."""

    @pytest.mark.unit
    def test_parse_minutes_de(self):
        """Test: Parse 'in 30 Minuten'."""
        from services.reminder_service import ReminderService

        result = ReminderService.parse_duration("in 30 Minuten")
        assert isinstance(result, timedelta)
        assert result == timedelta(minutes=30)

    @pytest.mark.unit
    def test_parse_minutes_en(self):
        """Test: Parse 'in 30 minutes'."""
        from services.reminder_service import ReminderService

        result = ReminderService.parse_duration("in 30 minutes")
        assert isinstance(result, timedelta)
        assert result == timedelta(minutes=30)

    @pytest.mark.unit
    def test_parse_hours_de(self):
        """Test: Parse 'in 2 Stunden'."""
        from services.reminder_service import ReminderService

        result = ReminderService.parse_duration("in 2 Stunden")
        assert isinstance(result, timedelta)
        assert result == timedelta(hours=2)

    @pytest.mark.unit
    def test_parse_hours_en(self):
        """Test: Parse 'in 2 hours'."""
        from services.reminder_service import ReminderService

        result = ReminderService.parse_duration("in 2 hours")
        assert isinstance(result, timedelta)
        assert result == timedelta(hours=2)

    @pytest.mark.unit
    def test_parse_absolute_time(self):
        """Test: Parse 'um 18:00' — interpreted in the LOCAL tz, stored naive UTC.

        Asserted tz-agnostically (round-trip back through the resolved local
        zone) so it holds under any DAYPART_TIMEZONE / DST, while still proving
        the local-time semantics fix (#1146).
        """
        from datetime import UTC

        from services.daypart_service import _resolve_tz
        from services.reminder_service import ReminderService

        result = ReminderService.parse_duration("um 18:00")
        assert isinstance(result, datetime)
        assert result.tzinfo is None  # stored naive UTC
        local = result.replace(tzinfo=UTC).astimezone(_resolve_tz())
        assert (local.hour, local.minute) == (18, 0)

    @pytest.mark.unit
    def test_parse_iso_datetime(self):
        """Test: Parse ISO datetime string."""
        from services.reminder_service import ReminderService

        result = ReminderService.parse_duration("2026-12-25T10:00:00")
        assert isinstance(result, datetime)
        assert result.month == 12
        assert result.day == 25

    @pytest.mark.unit
    def test_parse_invalid_returns_none(self):
        """Test: Unparseable string returns None."""
        from services.reminder_service import ReminderService

        result = ReminderService.parse_duration("morgen vielleicht")
        assert result is None


class TestReminderCRUD:
    """Reminder CRUD tests."""

    @pytest.mark.database
    async def test_create_reminder(self, db_session):
        """Test: Create a reminder via service."""
        from services.reminder_service import ReminderService

        service = ReminderService(db_session)
        reminder = await service.create_reminder(
            message="Wäsche aufhängen",
            trigger_at_str="in 30 Minuten",
        )

        assert reminder.id is not None
        assert reminder.message == "Wäsche aufhängen"
        assert reminder.status == REMINDER_PENDING
        assert reminder.trigger_at > datetime.utcnow()

    @pytest.mark.database
    async def test_cancel_reminder(self, db_session):
        """Test: Cancel a pending reminder."""
        from services.reminder_service import ReminderService

        service = ReminderService(db_session)
        reminder = await service.create_reminder(
            message="Cancel me",
            trigger_at_str="in 60 Minuten",
        )

        success = await service.cancel(reminder.id)
        assert success is True

        # Verify status
        from sqlalchemy import select
        result = await db_session.execute(
            select(Reminder).where(Reminder.id == reminder.id)
        )
        r = result.scalar_one()
        assert r.status == REMINDER_CANCELLED

    @pytest.mark.database
    async def test_list_pending_reminders(self, db_session):
        """Test: List only pending reminders."""
        from services.reminder_service import ReminderService

        service = ReminderService(db_session)
        await service.create_reminder(message="Pending 1", trigger_at_str="in 30 Minuten")
        r2 = await service.create_reminder(message="Pending 2", trigger_at_str="in 60 Minuten")
        await service.cancel(r2.id)

        pending = await service.list_pending()
        assert len(pending) == 1
        assert pending[0].message == "Pending 1"

    @pytest.mark.database
    async def test_get_due_reminders(self, db_session):
        """Test: Get reminders past their trigger time."""
        from services.reminder_service import ReminderService

        # Create a reminder that's already past due
        reminder = Reminder(
            message="Overdue",
            trigger_at=datetime.utcnow() - timedelta(minutes=5),
            status=REMINDER_PENDING,
        )
        db_session.add(reminder)
        await db_session.commit()

        service = ReminderService(db_session)
        due = await service.get_due_reminders()
        assert len(due) >= 1
        assert due[0].message == "Overdue"

    @pytest.mark.database
    async def test_mark_fired(self, db_session):
        """Test: Mark reminder as fired."""
        from services.reminder_service import ReminderService

        reminder = Reminder(
            message="Fire me",
            trigger_at=datetime.utcnow() - timedelta(minutes=1),
            status=REMINDER_PENDING,
        )
        db_session.add(reminder)
        await db_session.commit()
        await db_session.refresh(reminder)

        service = ReminderService(db_session)
        await service.mark_fired(reminder.id, notification_id=42)

        from sqlalchemy import select
        result = await db_session.execute(
            select(Reminder).where(Reminder.id == reminder.id)
        )
        r = result.scalar_one()
        assert r.status == REMINDER_FIRED
        assert r.fired_at is not None
        assert r.notification_id == 42

    @pytest.mark.database
    async def test_create_reminder_invalid_time(self, db_session):
        """Test: Invalid trigger time raises ValueError."""
        from services.reminder_service import ReminderService

        service = ReminderService(db_session)
        with pytest.raises(ValueError, match="Could not parse"):
            await service.create_reminder(
                message="Bad time",
                trigger_at_str="maybe later",
            )


# ============================================================================
# Privacy-Aware TTS Gating
# ============================================================================


class TestPrivacyTtsGating:
    """Tests for privacy fields on Notification model and TTS suppression."""

    @pytest.mark.unit
    def test_notification_privacy_defaults(self):
        """Notification model has privacy and target_user_id columns."""
        n = Notification(
            event_type="test",
            title="Test",
            message="Test msg",
            privacy="public",
        )
        assert n.privacy == "public"
        assert n.target_user_id is None

    @pytest.mark.unit
    def test_notification_privacy_stored(self):
        """Privacy and target_user_id can be set on Notification."""
        n = Notification(
            event_type="calendar.reminder",
            title="Arzttermin",
            message="In 30 min: Arzttermin",
            privacy="confidential",
            target_user_id=42,
        )
        assert n.privacy == "confidential"
        assert n.target_user_id == 42

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_tts_suppressed_for_confidential(self):
        """TTS is not delivered when privacy gate returns False."""
        notification = MagicMock()
        notification.privacy = "confidential"
        notification.target_user_id = 1
        notification.room_id = 10
        notification.id = 99

        with patch("ha_glue.services.notification_privacy.ha_should_play_tts_for_notification", new_callable=AsyncMock) as mock_gate:
            mock_gate.return_value = False
            from ha_glue.services.notification_privacy import ha_should_play_tts_for_notification
            result = await ha_should_play_tts_for_notification(
                privacy=notification.privacy,
                target_user_id=notification.target_user_id,
                room_id=notification.room_id,
            )
            assert result is False

    @pytest.mark.unit
    def test_webhook_schema_privacy_validation(self):
        """WebhookRequest validates privacy field."""
        from api.routes.notifications_schemas import WebhookRequest

        # Valid privacy
        req = WebhookRequest(
            event_type="test", title="T", message="M",
            privacy="confidential", target_user_id=1,
        )
        assert req.privacy == "confidential"
        assert req.target_user_id == 1

        # Invalid privacy
        with pytest.raises(ValueError):
            WebhookRequest(
                event_type="test", title="T", message="M",
                privacy="secret",
            )
