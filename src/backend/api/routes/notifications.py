"""
Notification API Routes — Proaktive Benachrichtigungen

Endpoints:
- POST /webhook          — HA → Renfield (Bearer Token auth)
- GET  /                 — Liste mit Filtern
- PATCH /{id}/acknowledge — Bestätigen
- DELETE /{id}           — Verwerfen (Soft Delete)
- POST /token            — Webhook-Token generieren (Admin)

Phase 2:
- POST /{id}/suppress        — Ähnliche Benachrichtigungen unterdrücken
- GET  /suppressions         — Aktive Suppressions auflisten
- DELETE /suppressions/{id}  — Suppression aufheben

Phase 3:
- GET  /schedules            — Geplante Jobs auflisten
- POST /schedules            — Job erstellen
- PATCH /schedules/{id}      — Job aktualisieren
- DELETE /schedules/{id}     — Job löschen
- POST /reminders            — Erinnerung erstellen
- GET  /reminders            — Erinnerungen auflisten
- DELETE /reminders/{id}     — Erinnerung stornieren
"""

from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from api.routes.notifications_schemas import (
    NotificationListResponse,
    NotificationResponse,
    ReminderListResponse,
    ReminderRequest,
    ReminderResponse,
    ScheduledJobListResponse,
    ScheduledJobRequest,
    ScheduledJobResponse,
    ScheduledJobUpdate,
    SuppressionListResponse,
    SuppressionResponse,
    SuppressRequest,
    TokenResponse,
    WebhookRequest,
    WebhookResponse,
)
from models.database import User
from models.permissions import Permission
from services.api_rate_limiter import limiter
from services.auth_service import get_current_user, require_permission
from services.database import get_db
from services.notification_service import NotificationService
from utils.config import settings

router = APIRouter()


def _notification_to_response(n) -> NotificationResponse:
    """Convert DB Notification to response schema."""
    return NotificationResponse(
        id=n.id,
        event_type=n.event_type,
        title=n.title,
        message=n.message,
        urgency=n.urgency,
        room_name=n.room_name,
        source=n.source,
        status=n.status,
        tts_delivered=n.tts_delivered,
        enriched=getattr(n, "enriched", False) or False,
        original_message=getattr(n, "original_message", None),
        created_at=n.created_at.isoformat() if n.created_at else "",
        delivered_at=n.delivered_at.isoformat() if n.delivered_at else None,
        acknowledged_at=n.acknowledged_at.isoformat() if n.acknowledged_at else None,
    )


# ==========================================================================
# Webhook Endpoint (Token-Authenticated, not user-authenticated)
# ==========================================================================

@router.post("/webhook", response_model=WebhookResponse)
@limiter.limit(settings.api_rate_limit_default)
async def receive_webhook(
    request: Request,
    body: WebhookRequest,
    authorization: str | None = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """
    Receive notification webhook from HA automation.

    Authenticated via Bearer token (stored in SystemSetting, not user JWT).
    """
    if not settings.proactive_enabled:
        raise HTTPException(status_code=503, detail="Proactive notifications are disabled")

    # Validate Bearer token
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    token = authorization.removeprefix("Bearer ").strip()
    service = NotificationService(db)

    if not await service.verify_webhook_token(token):
        raise HTTPException(status_code=403, detail="Invalid webhook token")

    try:
        result = await service.process_webhook(
            event_type=body.event_type,
            title=body.title,
            message=body.message,
            urgency=body.urgency,
            room=body.room,
            tts=body.tts,
            data=body.data,
            enrich=body.enrich,
        )
        return WebhookResponse(**result)
    except ValueError as e:
        # Dedup / suppression
        raise HTTPException(status_code=429, detail=str(e))
    except Exception as e:
        logger.error(f"❌ Webhook processing error: {e}")
        raise HTTPException(status_code=500, detail="Internal error processing notification")


# ==========================================================================
# CRUD Endpoints (User-Authenticated)
# ==========================================================================

@router.get("", response_model=NotificationListResponse)
@limiter.limit(settings.api_rate_limit_chat)
async def list_notifications(
    request: Request,
    room_id: int | None = None,
    urgency: str | None = None,
    status: str | None = None,
    since: str | None = None,
    limit: int = 50,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user),
):
    """List notifications with optional filters."""
    service = NotificationService(db)

    since_dt = None
    if since:
        try:
            since_dt = datetime.fromisoformat(since)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid 'since' datetime format")

    notifications = await service.list_notifications(
        room_id=room_id,
        urgency=urgency,
        status=status,
        since=since_dt,
        limit=min(limit, 200),
        offset=offset,
    )

    return NotificationListResponse(
        notifications=[_notification_to_response(n) for n in notifications],
        total=len(notifications),
        limit=limit,
        offset=offset,
    )


@router.patch("/{notification_id}/acknowledge")
@limiter.limit(settings.api_rate_limit_chat)
async def acknowledge_notification(
    notification_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user),
):
    """Mark a notification as acknowledged."""
    service = NotificationService(db)
    acknowledged_by = current_user.username if current_user else None

    success = await service.acknowledge(notification_id, acknowledged_by=acknowledged_by)
    if not success:
        raise HTTPException(status_code=404, detail="Notification not found")

    return {"success": True, "notification_id": notification_id}


@router.delete("/{notification_id}")
@limiter.limit(settings.api_rate_limit_chat)
async def dismiss_notification(
    notification_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user),
):
    """Dismiss (soft delete) a notification."""
    service = NotificationService(db)

    success = await service.dismiss(notification_id)
    if not success:
        raise HTTPException(status_code=404, detail="Notification not found")

    return {"success": True, "notification_id": notification_id}


# ==========================================================================
# Suppression Endpoints (Phase 2c)
# ==========================================================================

@router.post("/{notification_id}/suppress", response_model=SuppressionResponse)
@limiter.limit(settings.api_rate_limit_chat)
async def suppress_notification(
    notification_id: int,
    request: Request,
    body: SuppressRequest | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user),
):
    """Create a suppression rule from a notification."""
    service = NotificationService(db)
    user_id = current_user.id if current_user else None
    reason = body.reason if body else None

    suppression = await service.suppress_similar(
        notification_id=notification_id,
        reason=reason,
        user_id=user_id,
    )
    if not suppression:
        raise HTTPException(status_code=404, detail="Notification not found")

    return SuppressionResponse(
        id=suppression.id,
        event_pattern=suppression.event_pattern,
        reason=suppression.reason,
        is_active=suppression.is_active,
        created_at=suppression.created_at.isoformat() if suppression.created_at else "",
    )


@router.get("/suppressions", response_model=SuppressionListResponse)
@limiter.limit(settings.api_rate_limit_chat)
async def list_suppressions(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user),
):
    """List active suppression rules."""
    service = NotificationService(db)
    suppressions = await service.list_suppressions()

    return SuppressionListResponse(
        suppressions=[
            SuppressionResponse(
                id=s.id,
                event_pattern=s.event_pattern,
                reason=s.reason,
                is_active=s.is_active,
                created_at=s.created_at.isoformat() if s.created_at else "",
            )
            for s in suppressions
        ]
    )


@router.delete("/suppressions/{suppression_id}")
@limiter.limit(settings.api_rate_limit_chat)
async def delete_suppression(
    suppression_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user),
):
    """Deactivate a suppression rule."""
    service = NotificationService(db)

    success = await service.delete_suppression(suppression_id)
    if not success:
        raise HTTPException(status_code=404, detail="Suppression not found")

    return {"success": True, "suppression_id": suppression_id}


# ==========================================================================
# Scheduler Endpoints (Phase 3a)
# ==========================================================================

@router.get("/schedules", response_model=ScheduledJobListResponse)
@limiter.limit(settings.api_rate_limit_chat)
async def list_schedules(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user),
):
    """List scheduled notification jobs."""
    from sqlalchemy import select

    from models.database import ScheduledJob

    result = await db.execute(
        select(ScheduledJob).order_by(ScheduledJob.created_at.desc())
    )
    jobs = list(result.scalars().all())

    return ScheduledJobListResponse(
        jobs=[
            ScheduledJobResponse(
                id=j.id,
                name=j.name,
                schedule_cron=j.schedule_cron,
                job_type=j.job_type,
                config=j.config,
                is_enabled=j.is_enabled,
                last_run_at=j.last_run_at.isoformat() if j.last_run_at else None,
                next_run_at=j.next_run_at.isoformat() if j.next_run_at else None,
                room_id=j.room_id,
                created_at=j.created_at.isoformat() if j.created_at else "",
            )
            for j in jobs
        ]
    )


@router.post("/schedules", response_model=ScheduledJobResponse, status_code=201)
@limiter.limit(settings.api_rate_limit_chat)
async def create_schedule(
    request: Request,
    body: ScheduledJobRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user),
):
    """Create a scheduled notification job."""
    from models.database import ScheduledJob
    from services.notification_scheduler import NotificationScheduler

    # Validate cron and compute next run
    try:
        next_run = NotificationScheduler.next_run_after(body.schedule_cron, datetime.utcnow())
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    job = ScheduledJob(
        name=body.name,
        schedule_cron=body.schedule_cron,
        job_type=body.job_type,
        config=body.config,
        is_enabled=body.is_enabled,
        next_run_at=next_run,
        room_id=body.room_id,
        user_id=current_user.id if current_user else None,
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

    return ScheduledJobResponse(
        id=job.id,
        name=job.name,
        schedule_cron=job.schedule_cron,
        job_type=job.job_type,
        config=job.config,
        is_enabled=job.is_enabled,
        last_run_at=None,
        next_run_at=job.next_run_at.isoformat() if job.next_run_at else None,
        room_id=job.room_id,
        created_at=job.created_at.isoformat() if job.created_at else "",
    )


@router.patch("/schedules/{schedule_id}", response_model=ScheduledJobResponse)
@limiter.limit(settings.api_rate_limit_chat)
async def update_schedule(
    schedule_id: int,
    request: Request,
    body: ScheduledJobUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user),
):
    """Update a scheduled notification job."""
    from sqlalchemy import select

    from models.database import ScheduledJob

    result = await db.execute(
        select(ScheduledJob).where(ScheduledJob.id == schedule_id)
    )
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Schedule not found")

    if body.schedule_cron is not None:
        from services.notification_scheduler import NotificationScheduler
        try:
            next_run = NotificationScheduler.next_run_after(body.schedule_cron, datetime.utcnow())
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        job.schedule_cron = body.schedule_cron
        job.next_run_at = next_run

    if body.is_enabled is not None:
        job.is_enabled = body.is_enabled

    if body.config is not None:
        job.config = body.config

    await db.commit()
    await db.refresh(job)

    return ScheduledJobResponse(
        id=job.id,
        name=job.name,
        schedule_cron=job.schedule_cron,
        job_type=job.job_type,
        config=job.config,
        is_enabled=job.is_enabled,
        last_run_at=job.last_run_at.isoformat() if job.last_run_at else None,
        next_run_at=job.next_run_at.isoformat() if job.next_run_at else None,
        room_id=job.room_id,
        created_at=job.created_at.isoformat() if job.created_at else "",
    )


@router.delete("/schedules/{schedule_id}")
@limiter.limit(settings.api_rate_limit_chat)
async def delete_schedule(
    schedule_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user),
):
    """Delete a scheduled notification job."""
    from sqlalchemy import select

    from models.database import ScheduledJob

    result = await db.execute(
        select(ScheduledJob).where(ScheduledJob.id == schedule_id)
    )
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Schedule not found")

    await db.delete(job)
    await db.commit()

    return {"success": True, "schedule_id": schedule_id}


# ==========================================================================
# Reminder Endpoints (Phase 3b)
# ==========================================================================

@router.post("/reminders", response_model=ReminderResponse, status_code=201)
@limiter.limit(settings.api_rate_limit_chat)
async def create_reminder(
    request: Request,
    body: ReminderRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user),
):
    """Create a new reminder."""
    from services.reminder_service import ReminderService

    service = ReminderService(db)
    try:
        reminder = await service.create_reminder(
            message=body.message,
            trigger_at_str=body.trigger_at,
            room=body.room,
            user_id=current_user.id if current_user else None,
            session_id=body.session_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return ReminderResponse(
        id=reminder.id,
        message=reminder.message,
        trigger_at=reminder.trigger_at.isoformat() if reminder.trigger_at else "",
        room_name=reminder.room_name,
        status=reminder.status,
        created_at=reminder.created_at.isoformat() if reminder.created_at else "",
        fired_at=reminder.fired_at.isoformat() if reminder.fired_at else None,
    )


@router.get("/reminders", response_model=ReminderListResponse)
@limiter.limit(settings.api_rate_limit_chat)
async def list_reminders(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user),
):
    """List pending reminders."""
    from services.reminder_service import ReminderService

    service = ReminderService(db)
    reminders = await service.list_pending()

    return ReminderListResponse(
        reminders=[
            ReminderResponse(
                id=r.id,
                message=r.message,
                trigger_at=r.trigger_at.isoformat() if r.trigger_at else "",
                room_name=r.room_name,
                status=r.status,
                created_at=r.created_at.isoformat() if r.created_at else "",
                fired_at=r.fired_at.isoformat() if r.fired_at else None,
            )
            for r in reminders
        ]
    )


@router.delete("/reminders/{reminder_id}")
@limiter.limit(settings.api_rate_limit_chat)
async def cancel_reminder(
    reminder_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user),
):
    """Cancel a pending reminder."""
    from services.reminder_service import ReminderService

    service = ReminderService(db)
    success = await service.cancel(reminder_id)
    if not success:
        raise HTTPException(status_code=404, detail="Reminder not found or already fired")

    return {"success": True, "reminder_id": reminder_id}


# ==========================================================================
# Token Management (Admin only)
# ==========================================================================

@router.post("/token", response_model=TokenResponse)
@limiter.limit(settings.api_rate_limit_admin)
async def generate_token(
    request: Request,
    db: AsyncSession = Depends(get_db),
    user=Depends(require_permission(Permission.NOTIFICATIONS_MANAGE)),
):
    """Generate or rotate the webhook authentication token."""
    service = NotificationService(db)
    token = await service.generate_webhook_token()
    return TokenResponse(token=token)
