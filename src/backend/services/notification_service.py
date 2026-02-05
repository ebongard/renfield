"""
Notification Service — Proaktive Benachrichtigungen

Empfängt Webhooks (z.B. von HA-Automationen), dedupliziert,
speichert in DB und liefert an Geräte aus (WebSocket + TTS).
"""

import hashlib
import secrets
from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy import delete, select
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
from utils.config import settings


class NotificationService:
    """
    Core notification service: webhook processing, dedup, delivery.
    """

    def __init__(self, db: AsyncSession):
        self.db = db

    # ------------------------------------------------------------------
    # Webhook Token Management
    # ------------------------------------------------------------------

    async def get_webhook_token(self) -> str | None:
        """Retrieve stored webhook token from SystemSetting."""
        result = await self.db.execute(
            select(SystemSetting).where(
                SystemSetting.key == SETTING_NOTIFICATION_WEBHOOK_TOKEN
            )
        )
        setting = result.scalar_one_or_none()
        return setting.value if setting else None

    async def generate_webhook_token(self) -> str:
        """Generate a new webhook token and store in SystemSetting."""
        token = secrets.token_urlsafe(48)

        result = await self.db.execute(
            select(SystemSetting).where(
                SystemSetting.key == SETTING_NOTIFICATION_WEBHOOK_TOKEN
            )
        )
        existing = result.scalar_one_or_none()

        if existing:
            existing.value = token
        else:
            self.db.add(SystemSetting(
                key=SETTING_NOTIFICATION_WEBHOOK_TOKEN,
                value=token,
            ))

        await self.db.commit()
        logger.info("🔑 Neuen Webhook-Token generiert")
        return token

    async def verify_webhook_token(self, token: str) -> bool:
        """Check if a given Bearer token matches the stored webhook token."""
        stored = await self.get_webhook_token()
        if not stored:
            return False
        return secrets.compare_digest(stored, token)

    # ------------------------------------------------------------------
    # Deduplication
    # ------------------------------------------------------------------

    def _compute_dedup_key(
        self,
        event_type: str,
        title: str,
        message: str,
        room_name: str | None,
    ) -> str:
        """Hash-based dedup key from event content."""
        raw = f"{event_type}:{title}:{message}:{room_name or ''}"
        return hashlib.sha256(raw.encode()).hexdigest()[:40]

    async def _is_duplicate(self, dedup_key: str) -> bool:
        """Check if a notification with the same dedup_key was sent recently."""
        window = datetime.utcnow() - timedelta(seconds=settings.proactive_suppression_window)
        result = await self.db.execute(
            select(Notification.id).where(
                Notification.dedup_key == dedup_key,
                Notification.created_at >= window,
            ).limit(1)
        )
        return result.scalar_one_or_none() is not None

    # ------------------------------------------------------------------
    # Room Resolution
    # ------------------------------------------------------------------

    async def _resolve_room(self, room_name: str | None) -> tuple[int | None, str | None]:
        """Resolve room name to (room_id, room_name). Returns (None, None) if not found."""
        if not room_name:
            return None, None

        result = await self.db.execute(
            select(Room).where(Room.name == room_name)
        )
        room = result.scalar_one_or_none()
        if room:
            return room.id, room.name

        # Try case-insensitive alias match
        result = await self.db.execute(
            select(Room).where(Room.alias == room_name.lower())
        )
        room = result.scalar_one_or_none()
        if room:
            return room.id, room.name

        return None, room_name

    # ------------------------------------------------------------------
    # Core: Process Webhook
    # ------------------------------------------------------------------

    async def process_webhook(
        self,
        event_type: str,
        title: str,
        message: str,
        urgency: str = "info",
        room: str | None = None,
        tts: bool | None = None,
        data: dict | None = None,
    ) -> dict:
        """
        Process an incoming webhook notification.

        Returns dict with notification_id, status, delivered_to.
        Raises ValueError on dedup suppression.
        """
        # Default TTS from config
        if tts is None:
            tts = settings.proactive_tts_default

        # Dedup check
        dedup_key = self._compute_dedup_key(event_type, title, message, room)
        if await self._is_duplicate(dedup_key):
            logger.info(f"🔇 Notification suppressed (duplicate): {title}")
            raise ValueError("Duplicate notification suppressed")

        # Resolve room
        room_id, room_name = await self._resolve_room(room)

        # Compute expiry
        expires_at = datetime.utcnow() + timedelta(seconds=settings.proactive_notification_ttl)

        # Create notification
        notification = Notification(
            event_type=event_type,
            title=title,
            message=message,
            urgency=urgency,
            room_id=room_id,
            room_name=room_name,
            source="ha_automation",
            source_data=data,
            status=NOTIFICATION_PENDING,
            tts_delivered=False,
            dedup_key=dedup_key,
            expires_at=expires_at,
        )
        self.db.add(notification)
        await self.db.commit()
        await self.db.refresh(notification)

        logger.info(f"📨 Notification #{notification.id} erstellt: {title} (urgency={urgency})")

        # Deliver
        delivered_to = await self._deliver(notification, tts=tts)

        # Update status
        notification.status = NOTIFICATION_DELIVERED
        notification.delivered_at = datetime.utcnow()
        notification.delivered_to = delivered_to
        await self.db.commit()

        return {
            "notification_id": notification.id,
            "status": NOTIFICATION_DELIVERED,
            "delivered_to": delivered_to,
        }

    # ------------------------------------------------------------------
    # Delivery
    # ------------------------------------------------------------------

    async def _deliver(self, notification: Notification, tts: bool = True) -> list[str]:
        """
        Deliver notification to connected devices via WebSocket broadcast
        and optionally via TTS.

        Returns list of device IDs that received the notification.
        """
        from services.device_manager import get_device_manager

        device_manager = get_device_manager()
        delivered_ids: list[str] = []

        # Build WS message
        ws_message = {
            "type": "notification",
            "notification_id": notification.id,
            "title": notification.title,
            "message": notification.message,
            "urgency": notification.urgency,
            "source": notification.source,
            "room": notification.room_name,
            "tts_handled": False,
            "created_at": notification.created_at.isoformat() if notification.created_at else None,
        }

        # Determine target devices
        if notification.room_name:
            # Room-specific: broadcast to room devices
            devices = device_manager.get_devices_in_room(notification.room_name)
            if notification.room_id:
                devices = devices or device_manager.get_devices_in_room_by_id(notification.room_id)
        else:
            # Global: broadcast to all devices
            devices = list(device_manager.devices.values())

        # Send to display-capable devices with notification support
        for device in devices:
            if device.capabilities.supports_notifications or device.capabilities.has_display:
                try:
                    await device.websocket.send_json(ws_message)
                    delivered_ids.append(device.device_id)
                except Exception as e:
                    logger.warning(f"⚠️ Notification delivery failed for {device.device_id}: {e}")

        logger.info(f"📤 Notification #{notification.id} an {len(delivered_ids)} Geräte gesendet")

        # TTS delivery
        if tts:
            tts_delivered = await self._deliver_tts(notification)
            if tts_delivered:
                notification.tts_delivered = True
                ws_message["tts_handled"] = True

        return delivered_ids

    async def _deliver_tts(self, notification: Notification) -> bool:
        """Generate TTS and route to best audio output device for the room."""
        try:
            from services.piper_service import PiperService

            piper = PiperService()
            tts_audio = await piper.synthesize_to_bytes(notification.message)

            if not tts_audio:
                logger.warning(f"⚠️ TTS synthesis failed for notification #{notification.id}")
                return False

            # Route via OutputRoutingService if room is known
            if notification.room_id:
                from services.audio_output_service import get_audio_output_service
                from services.database import AsyncSessionLocal
                from services.output_routing_service import OutputRoutingService

                async with AsyncSessionLocal() as db_session:
                    routing_service = OutputRoutingService(db_session)
                    audio_output_service = get_audio_output_service()

                    decision = await routing_service.get_audio_output_for_room(
                        room_id=notification.room_id,
                    )

                    if decision.output_device and not decision.fallback_to_input:
                        success = await audio_output_service.play_audio(
                            audio_bytes=tts_audio,
                            output_device=decision.output_device,
                        )
                        if success:
                            logger.info(f"🔊 TTS für Notification #{notification.id} abgespielt")
                            return True

            # Fallback: send TTS to all speakers in the target room (or all rooms)
            from services.device_manager import get_device_manager

            device_manager = get_device_manager()
            if notification.room_name:
                devices = device_manager.get_devices_in_room(notification.room_name)
            else:
                devices = list(device_manager.devices.values())

            import base64
            audio_b64 = base64.b64encode(tts_audio).decode("utf-8")

            for device in devices:
                if device.capabilities.has_speaker:
                    try:
                        await device.websocket.send_json({
                            "type": "tts_audio",
                            "session_id": f"notification-{notification.id}",
                            "audio": audio_b64,
                            "is_final": True,
                        })
                        logger.info(f"🔊 TTS an {device.device_id} gesendet")
                        return True
                    except Exception as e:
                        logger.warning(f"⚠️ TTS delivery to {device.device_id} failed: {e}")

            return False

        except Exception as e:
            logger.error(f"❌ TTS delivery failed for notification #{notification.id}: {e}")
            return False

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def list_notifications(
        self,
        room_id: int | None = None,
        urgency: str | None = None,
        status: str | None = None,
        since: datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Notification]:
        """List notifications with optional filters."""
        query = select(Notification).order_by(Notification.created_at.desc())

        if room_id is not None:
            query = query.where(Notification.room_id == room_id)
        if urgency:
            query = query.where(Notification.urgency == urgency)
        if status:
            query = query.where(Notification.status == status)
        if since:
            query = query.where(Notification.created_at >= since)

        # Filter out expired
        query = query.where(
            (Notification.expires_at.is_(None)) | (Notification.expires_at > datetime.utcnow())
        )

        query = query.offset(offset).limit(limit)
        result = await self.db.execute(query)
        return list(result.scalars().all())

    async def get_notification(self, notification_id: int) -> Notification | None:
        """Get a single notification by ID."""
        result = await self.db.execute(
            select(Notification).where(Notification.id == notification_id)
        )
        return result.scalar_one_or_none()

    async def acknowledge(self, notification_id: int, acknowledged_by: str | None = None) -> bool:
        """Mark a notification as acknowledged."""
        notification = await self.get_notification(notification_id)
        if not notification:
            return False

        notification.status = NOTIFICATION_ACKNOWLEDGED
        notification.acknowledged_at = datetime.utcnow()
        notification.acknowledged_by = acknowledged_by
        await self.db.commit()
        return True

    async def dismiss(self, notification_id: int) -> bool:
        """Soft-delete (dismiss) a notification."""
        notification = await self.get_notification(notification_id)
        if not notification:
            return False

        notification.status = NOTIFICATION_DISMISSED
        await self.db.commit()
        return True

    async def cleanup_expired(self) -> int:
        """Delete expired notifications. Returns count of deleted rows."""
        result = await self.db.execute(
            delete(Notification).where(
                Notification.expires_at.isnot(None),
                Notification.expires_at < datetime.utcnow(),
            )
        )
        await self.db.commit()
        count = result.rowcount
        if count:
            logger.info(f"🗑️ {count} abgelaufene Notifications gelöscht")
        return count
