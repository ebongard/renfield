"""
Tests für Proaktive Benachrichtigungen

Testet:
- NotificationService: Webhook, Dedup, CRUD, Token
- Notification API: Webhook-Endpoint, Liste, Acknowledge, Dismiss, Token
- WebSocket: notification_ack Handling
"""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import (
    NOTIFICATION_ACKNOWLEDGED,
    NOTIFICATION_DELIVERED,
    NOTIFICATION_DISMISSED,
    NOTIFICATION_PENDING,
    SETTING_NOTIFICATION_WEBHOOK_TOKEN,
    Notification,
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
