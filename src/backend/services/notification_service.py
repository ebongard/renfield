"""
Notification Service — Proaktive Benachrichtigungen

Empfängt Webhooks (z.B. von HA-Automationen), dedupliziert,
speichert in DB und liefert an Geräte aus (WebSocket + TTS).

Phase 2: Semantic dedup, urgency classification, LLM enrichment, feedback learning.
"""

import asyncio
import hashlib
import secrets
from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import (
    NOTIFICATION_ACKNOWLEDGED,
    NOTIFICATION_DELIVERED,
    NOTIFICATION_DISMISSED,
    NOTIFICATION_PENDING,
    SETTING_NOTIFICATION_WEBHOOK_TOKEN,
    Notification,
    NotificationSuppression,
    Room,
    SystemSetting,
)
from utils.config import settings


class NotificationService:
    """
    Core notification service: webhook processing, dedup, delivery,
    semantic intelligence, and suppression learning.
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
    # Deduplication (Hash-based)
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
    # Embedding Generation (Phase 2)
    # ------------------------------------------------------------------

    async def _get_embedding(self, text_input: str) -> list[float]:
        """Generate embedding via Ollama (same pattern as IntentFeedbackService)."""
        from utils.llm_client import get_default_client

        client = get_default_client()
        response = await client.embeddings(
            model=settings.ollama_embed_model,
            prompt=text_input,
        )
        return response.embedding

    # ------------------------------------------------------------------
    # Semantic Deduplication (Phase 2b)
    # ------------------------------------------------------------------

    async def _is_semantic_duplicate(
        self, embedding: list[float], window_seconds: int | None = None,
    ) -> bool:
        """Check if a semantically similar notification exists within the suppression window."""
        if not settings.proactive_semantic_dedup_enabled:
            return False

        window = window_seconds or settings.proactive_suppression_window
        threshold = settings.proactive_semantic_dedup_threshold
        since = datetime.utcnow() - timedelta(seconds=window)

        try:
            result = await self.db.execute(
                text("""
                    SELECT id,
                           1 - (embedding <=> CAST(:embedding AS vector)) as similarity
                    FROM notifications
                    WHERE embedding IS NOT NULL
                      AND created_at >= :since
                    ORDER BY embedding <=> CAST(:embedding AS vector)
                    LIMIT 1
                """),
                {"embedding": str(embedding), "since": since},
            )
            row = result.first()
            if row and row.similarity >= threshold:
                logger.info(
                    f"🔇 Semantic duplicate detected (similarity={row.similarity:.3f}, "
                    f"threshold={threshold})"
                )
                return True
        except Exception as e:
            logger.debug(f"Semantic dedup check skipped (pgvector not available): {e}")

        return False

    # Track background tasks to prevent GC
    _background_tasks: set = set()

    def _store_embedding_background(self, notification_id: int, embedding: list[float]) -> None:
        """Store embedding on notification in background task."""

        async def _store():
            try:
                from services.database import AsyncSessionLocal

                async with AsyncSessionLocal() as db_session:
                    result = await db_session.execute(
                        select(Notification).where(Notification.id == notification_id)
                    )
                    notification = result.scalar_one_or_none()
                    if notification:
                        notification.embedding = embedding
                        await db_session.commit()
            except Exception as e:
                logger.warning(f"⚠️ Failed to store embedding for notification #{notification_id}: {e}")

        task = asyncio.create_task(_store())
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    # ------------------------------------------------------------------
    # Urgency Auto-Classification (Phase 2d)
    # ------------------------------------------------------------------

    async def _auto_classify_urgency(
        self, event_type: str, title: str, message: str,
    ) -> str:
        """Use LLM to classify urgency into critical/info/low."""
        if not settings.proactive_urgency_auto_enabled:
            return "info"

        try:
            from utils.llm_client import get_default_client

            client = get_default_client()
            prompt = (
                "Classify the urgency of this notification. "
                "Reply with EXACTLY one word: critical, info, or low.\n\n"
                f"Event: {event_type}\n"
                f"Title: {title}\n"
                f"Message: {message}\n\n"
                "Urgency:"
            )
            response = await client.generate(
                model=settings.proactive_enrichment_model or settings.ollama_model,
                prompt=prompt,
                options={"temperature": 0.0, "num_predict": 10},
            )
            result = response.response.strip().lower()
            if result in ("critical", "info", "low"):
                return result
        except Exception as e:
            logger.warning(f"⚠️ Urgency auto-classification failed: {e}")

        return "info"

    # ------------------------------------------------------------------
    # LLM Content Enrichment (Phase 2a)
    # ------------------------------------------------------------------

    async def _enrich_message(
        self,
        event_type: str,
        title: str,
        message: str,
        data: dict | None = None,
    ) -> str:
        """Enrich notification message with natural language context via LLM."""
        if not settings.proactive_enrichment_enabled:
            return message

        try:
            from utils.llm_client import get_default_client

            client = get_default_client()
            context = ""
            if data:
                context = f"\nZusätzliche Daten: {data}"

            prompt = (
                "Du bist ein Smart-Home-Assistent. Formuliere die folgende Benachrichtigung "
                "als natürlich-sprachliche, hilfreiche Nachricht für den Bewohner. "
                "Halte dich kurz (1-2 Sätze). Antworte NUR mit der formulierten Nachricht.\n\n"
                f"Event: {event_type}\n"
                f"Titel: {title}\n"
                f"Nachricht: {message}{context}\n\n"
                "Formulierte Nachricht:"
            )
            response = await client.generate(
                model=settings.proactive_enrichment_model or settings.ollama_model,
                prompt=prompt,
                options={"temperature": 0.3, "num_predict": 200},
            )
            enriched = response.response.strip()
            if enriched:
                return enriched
        except Exception as e:
            logger.warning(f"⚠️ Message enrichment failed: {e}")

        return message

    # ------------------------------------------------------------------
    # Suppression Check (Phase 2c)
    # ------------------------------------------------------------------

    async def _is_suppressed(
        self, event_type: str, embedding: list[float] | None = None,
    ) -> bool:
        """Check if this notification type is suppressed via feedback learning."""
        if not settings.proactive_feedback_learning_enabled:
            return False

        # Exact event_type match
        result = await self.db.execute(
            select(NotificationSuppression.id).where(
                NotificationSuppression.event_pattern == event_type,
                NotificationSuppression.is_active.is_(True),
            ).limit(1)
        )
        if result.scalar_one_or_none() is not None:
            logger.info(f"🔇 Notification suppressed by event_type pattern: {event_type}")
            return True

        # Semantic similarity check (if embedding available)
        if embedding:
            threshold = settings.proactive_feedback_similarity_threshold
            try:
                result = await self.db.execute(
                    text("""
                        SELECT id,
                               1 - (embedding <=> CAST(:embedding AS vector)) as similarity
                        FROM notification_suppressions
                        WHERE is_active = true
                          AND embedding IS NOT NULL
                        ORDER BY embedding <=> CAST(:embedding AS vector)
                        LIMIT 1
                    """),
                    {"embedding": str(embedding)},
                )
                row = result.first()
                if row and row.similarity >= threshold:
                    logger.info(
                        f"🔇 Notification suppressed by semantic match "
                        f"(similarity={row.similarity:.3f})"
                    )
                    return True
            except Exception as e:
                logger.debug(f"Semantic suppression check skipped: {e}")

        return False

    async def suppress_similar(
        self,
        notification_id: int,
        reason: str | None = None,
        user_id: int | None = None,
    ) -> NotificationSuppression | None:
        """Create a suppression rule from an existing notification."""
        notification = await self.get_notification(notification_id)
        if not notification:
            return None

        # Generate embedding for semantic matching
        embedding = None
        try:
            combined = f"{notification.title} {notification.message}"
            embedding = await self._get_embedding(combined)
        except Exception as e:
            logger.warning(f"⚠️ Could not generate suppression embedding: {e}")

        suppression = NotificationSuppression(
            event_pattern=notification.event_type,
            embedding=embedding,
            source_notification_id=notification.id,
            user_id=user_id,
            reason=reason,
            is_active=True,
        )
        self.db.add(suppression)
        await self.db.commit()
        await self.db.refresh(suppression)

        logger.info(
            f"🔕 Suppression rule #{suppression.id} created for event_type={notification.event_type}"
        )
        return suppression

    async def list_suppressions(self, active_only: bool = True) -> list[NotificationSuppression]:
        """List suppression rules."""
        query = select(NotificationSuppression).order_by(NotificationSuppression.created_at.desc())
        if active_only:
            query = query.where(NotificationSuppression.is_active.is_(True))
        result = await self.db.execute(query)
        return list(result.scalars().all())

    async def delete_suppression(self, suppression_id: int) -> bool:
        """Deactivate a suppression rule."""
        result = await self.db.execute(
            select(NotificationSuppression).where(NotificationSuppression.id == suppression_id)
        )
        suppression = result.scalar_one_or_none()
        if not suppression:
            return False

        suppression.is_active = False
        await self.db.commit()
        return True

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
        enrich: bool = False,
    ) -> dict:
        """
        Process an incoming webhook notification.

        Returns dict with notification_id, status, delivered_to.
        Raises ValueError on dedup/suppression.
        """
        # Default TTS from config
        if tts is None:
            tts = settings.proactive_tts_default

        # 1. Hash-based dedup (fast first-pass)
        dedup_key = self._compute_dedup_key(event_type, title, message, room)
        if await self._is_duplicate(dedup_key):
            logger.info(f"🔇 Notification suppressed (duplicate): {title}")
            raise ValueError("Duplicate notification suppressed")

        # 2. Generate embedding (reused for semantic dedup + suppression)
        embedding = None
        if settings.proactive_semantic_dedup_enabled or settings.proactive_feedback_learning_enabled:
            try:
                combined = f"{title} {message}"
                embedding = await self._get_embedding(combined)
            except Exception as e:
                logger.warning(f"⚠️ Embedding generation failed: {e}")

        # 3. Suppression check (feedback learning)
        if await self._is_suppressed(event_type, embedding):
            raise ValueError("Notification suppressed by feedback rule")

        # 4. Semantic dedup check
        if embedding and await self._is_semantic_duplicate(embedding):
            raise ValueError("Semantic duplicate notification suppressed")

        # 5. Auto-classify urgency
        urgency_auto = False
        if urgency == "auto":
            urgency = await self._auto_classify_urgency(event_type, title, message)
            urgency_auto = True

        # 6. LLM enrichment
        original_message = None
        enriched = False
        if enrich and settings.proactive_enrichment_enabled:
            original_message = message
            message = await self._enrich_message(event_type, title, message, data)
            enriched = message != original_message

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
            enriched=enriched,
            original_message=original_message,
            urgency_auto=urgency_auto,
        )
        self.db.add(notification)
        await self.db.commit()
        await self.db.refresh(notification)

        logger.info(f"📨 Notification #{notification.id} erstellt: {title} (urgency={urgency})")

        # Store embedding in background
        if embedding:
            self._store_embedding_background(notification.id, embedding)

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
                            session_id=f"notification-{notification.id}",
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
