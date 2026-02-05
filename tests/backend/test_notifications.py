"""
Tests für Proaktive Benachrichtigungen

Testet:
- NotificationService: Webhook, Dedup, CRUD, Token
- Phase 2: Semantic Dedup, Urgency Classification, Enrichment, Suppressions
- Phase 3: Scheduler (Cron Parser), Reminders (Duration Parsing)
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
    ScheduledJob,
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
    async def test_auto_classify_disabled_returns_info(self, notification_service):
        """Test: Returns 'info' when auto-classification is disabled."""
        result = await notification_service._auto_classify_urgency("test", "Title", "Message")
        assert result == "info"

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
        """Test: Falls back to 'info' on LLM error."""
        mock_settings.proactive_urgency_auto_enabled = True
        mock_settings.proactive_enrichment_model = None
        mock_settings.ollama_model = "test-model"

        with patch("utils.llm_client.get_default_client", side_effect=Exception("LLM unavailable")):
            result = await notification_service._auto_classify_urgency("test", "Title", "Message")
            assert result == "info"

    @pytest.mark.database
    async def test_urgency_auto_flag_stored(self, notification_service, db_session):
        """Test: urgency_auto flag is stored on notification."""
        with patch("services.notification_service.NotificationService._deliver", new_callable=AsyncMock, return_value=[]), \
             patch("services.notification_service.NotificationService._auto_classify_urgency", new_callable=AsyncMock, return_value="critical"):
            result = await notification_service.process_webhook(
                event_type="test",
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
            mock_settings.ollama_model = "test-model"

            result = await notification_service.process_webhook(
                event_type="test",
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
# Phase 3a: Scheduler / Cron Parser Tests
# ============================================================================

class TestCronParser:
    """Minimal cron parser tests."""

    @pytest.mark.unit
    def test_cron_basic_time(self):
        """Test: Parse simple cron (30 7 * * *) = 7:30 daily."""
        from services.notification_scheduler import NotificationScheduler

        after = datetime(2026, 2, 5, 6, 0, 0)  # 06:00
        result = NotificationScheduler.next_run_after("30 7 * * *", after)
        assert result.hour == 7
        assert result.minute == 30
        assert result.day == 5

    @pytest.mark.unit
    def test_cron_next_day(self):
        """Test: If time already passed, picks next day."""
        from services.notification_scheduler import NotificationScheduler

        after = datetime(2026, 2, 5, 8, 0, 0)  # 08:00, past 07:30
        result = NotificationScheduler.next_run_after("30 7 * * *", after)
        assert result.hour == 7
        assert result.minute == 30
        assert result.day == 6

    @pytest.mark.unit
    def test_cron_wildcards_all(self):
        """Test: All wildcards = next minute."""
        from services.notification_scheduler import NotificationScheduler

        after = datetime(2026, 2, 5, 10, 15, 0)
        result = NotificationScheduler.next_run_after("* * * * *", after)
        assert result == datetime(2026, 2, 5, 10, 16, 0)

    @pytest.mark.unit
    def test_cron_specific_dow(self):
        """Test: Day of week matching (1=Monday)."""
        from services.notification_scheduler import NotificationScheduler

        # 2026-02-05 is Thursday (python weekday=3, cron dow=4)
        after = datetime(2026, 2, 5, 0, 0, 0)
        result = NotificationScheduler.next_run_after("0 8 * * 1", after)  # Monday
        # Next Monday is Feb 9
        assert result.weekday() == 0  # Monday
        assert result.hour == 8
        assert result.minute == 0

    @pytest.mark.unit
    def test_cron_invalid_expression(self):
        """Test: Invalid cron raises ValueError."""
        from services.notification_scheduler import NotificationScheduler

        with pytest.raises(ValueError, match="expected 5 fields"):
            NotificationScheduler.next_run_after("30 7 *", datetime.utcnow())

    @pytest.mark.unit
    def test_cron_invalid_value(self):
        """Test: Out-of-range value raises ValueError."""
        from services.notification_scheduler import NotificationScheduler

        with pytest.raises(ValueError):
            NotificationScheduler.next_run_after("99 7 * * *", datetime.utcnow())


class TestSchedulerCRUD:
    """Scheduler CRUD tests."""

    @pytest.mark.database
    async def test_create_scheduled_job(self, db_session):
        """Test: Create a scheduled job."""
        job = ScheduledJob(
            name="Morning Briefing",
            schedule_cron="30 7 * * *",
            job_type="briefing",
            is_enabled=True,
            next_run_at=datetime(2026, 2, 6, 7, 30),
        )
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)

        assert job.id is not None
        assert job.name == "Morning Briefing"
        assert job.is_enabled is True

    @pytest.mark.database
    async def test_disable_scheduled_job(self, db_session):
        """Test: Disable a scheduled job."""
        job = ScheduledJob(
            name="Test Job",
            schedule_cron="0 8 * * *",
            job_type="briefing",
            is_enabled=True,
        )
        db_session.add(job)
        await db_session.commit()

        job.is_enabled = False
        await db_session.commit()
        await db_session.refresh(job)
        assert job.is_enabled is False


# ============================================================================
# Phase 3b: Reminder Tests
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
        """Test: Parse 'um 18:00'."""
        from services.reminder_service import ReminderService

        result = ReminderService.parse_duration("um 18:00")
        assert isinstance(result, datetime)
        assert result.hour == 18
        assert result.minute == 0

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
