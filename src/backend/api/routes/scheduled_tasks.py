"""Scheduled Tasks management API (#1137, docs/design/scheduled-tasks.md).

ADMIN-gated CRUD over the ``scheduled_tasks`` table that the engine
(services/scheduled_tasks/engine.py) runs. Built-ins are edit-not-delete; custom
tasks are fully deletable. Writes validate the schedule (interval floor / cron
parse) and the handler ``params``, and recompute ``next_run_at`` on a schedule
change. Backs the "Geplante Aufgaben" admin page.
"""
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, status
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import (
    SCHEDULE_KIND_CRON,
    SCHEDULE_KIND_INTERVAL,
    ScheduledTask,
    User,
)
from models.permissions import Permission
from services.auth_service import require_permission
from services.database import get_db
from services.scheduled_tasks import registry
from services.scheduled_tasks.engine import (
    _cron_next,
    _validate_interval_floor,
    compute_next_run,
)

router = APIRouter()

_VALID_KINDS = {SCHEDULE_KIND_INTERVAL, SCHEDULE_KIND_CRON}


def _naive_utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


# =============================================================================
# Schemas
# =============================================================================

class ScheduledTaskResponse(BaseModel):
    id: int
    name: str
    handler_key: str
    schedule_kind: str
    interval_seconds: int | None
    cron_expr: str | None
    params: dict
    enabled: bool
    run_at_boot: bool
    start_at: datetime | None
    end_at: datetime | None
    next_run_at: datetime | None
    last_run_at: datetime | None
    last_status: str | None
    last_error: str | None
    last_duration_ms: int | None
    is_builtin: bool
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ScheduledTaskRunResponse(BaseModel):
    id: int
    started_at: datetime
    finished_at: datetime | None
    status: str
    duration_ms: int | None
    detail: str | None
    error: str | None

    class Config:
        from_attributes = True


class ScheduledTaskList(BaseModel):
    tasks: list[ScheduledTaskResponse]
    # The handler_keys a custom task may bind to (for the create form).
    available_handlers: list[str]
    # The engine tick = the interval floor (a custom interval must be >= this).
    engine_tick_seconds: int


class CreateScheduledTaskRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=255)
    handler_key: str = Field(..., min_length=1, max_length=100)
    schedule_kind: str = SCHEDULE_KIND_INTERVAL
    interval_seconds: int | None = None
    cron_expr: str | None = Field(None, max_length=120)
    params: dict = Field(default_factory=dict)
    run_at_boot: bool = False
    enabled: bool = True
    start_at: datetime | None = None
    end_at: datetime | None = None


class UpdateScheduledTaskRequest(BaseModel):
    """All fields optional — only the provided ones change (PATCH semantics)."""
    enabled: bool | None = None
    schedule_kind: str | None = None
    interval_seconds: int | None = None
    cron_expr: str | None = None
    params: dict | None = None
    run_at_boot: bool | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None


# =============================================================================
# Validation helpers
# =============================================================================

def _validate_schedule(kind: str, interval_seconds: int | None, cron_expr: str | None) -> None:
    """Raise HTTP 400 on an invalid schedule (bad kind, missing/extra field,
    sub-tick interval, or unparseable cron)."""
    if kind not in _VALID_KINDS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Ungültiger schedule_kind: {kind!r}")
    if kind == SCHEDULE_KIND_INTERVAL:
        if interval_seconds is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "interval_seconds ist für schedule_kind=interval erforderlich")
        try:
            _validate_interval_floor(interval_seconds)
        except ValueError as e:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e
    else:  # cron
        if not cron_expr:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "cron_expr ist für schedule_kind=cron erforderlich")
        if _cron_next(cron_expr, _naive_utcnow()) is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Ungültiger cron-Ausdruck: {cron_expr!r}")


def _validate_params(handler_key: str, params: dict) -> None:
    try:
        registry.validate_params(handler_key, params)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e


# =============================================================================
# Routes
# =============================================================================

@router.get("", response_model=ScheduledTaskList)
async def list_scheduled_tasks(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_permission(Permission.ADMIN)),
) -> ScheduledTaskList:
    rows = (await db.execute(
        select(ScheduledTask).order_by(ScheduledTask.name)
    )).scalars().all()
    from utils.config import settings

    return ScheduledTaskList(
        tasks=[ScheduledTaskResponse.model_validate(r) for r in rows],
        available_handlers=registry.all_handler_keys(),
        engine_tick_seconds=settings.scheduled_tasks_engine_tick_seconds,
    )


@router.get("/{task_id}", response_model=ScheduledTaskResponse)
async def get_scheduled_task(
    task_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_permission(Permission.ADMIN)),
) -> ScheduledTaskResponse:
    task = await db.get(ScheduledTask, task_id)
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Aufgabe nicht gefunden")
    return ScheduledTaskResponse.model_validate(task)


@router.get("/{task_id}/runs", response_model=list[ScheduledTaskRunResponse])
async def list_scheduled_task_runs(
    task_id: int,
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_permission(Permission.ADMIN)),
) -> list[ScheduledTaskRunResponse]:
    """Per-run history for a task (newest first) — the UI's "log of each run"."""
    from models.database import ScheduledTaskRun

    if await db.get(ScheduledTask, task_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Aufgabe nicht gefunden")
    limit = max(1, min(limit, 200))
    rows = (await db.execute(
        select(ScheduledTaskRun)
        .where(ScheduledTaskRun.task_id == task_id)
        .order_by(ScheduledTaskRun.started_at.desc(), ScheduledTaskRun.id.desc())
        .limit(limit)
    )).scalars().all()
    return [ScheduledTaskRunResponse.model_validate(r) for r in rows]


@router.post("", response_model=ScheduledTaskResponse, status_code=status.HTTP_201_CREATED)
async def create_scheduled_task(
    body: CreateScheduledTaskRequest,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_permission(Permission.ADMIN)),
) -> ScheduledTaskResponse:
    if registry.get_handler(body.handler_key) is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Unbekannter handler_key: {body.handler_key!r} (verfügbar: {registry.all_handler_keys()})",
        )
    _validate_schedule(body.schedule_kind, body.interval_seconds, body.cron_expr)
    _validate_params(body.handler_key, body.params)

    # Name must be unique (the seed/idempotency key).
    exists = await db.scalar(select(ScheduledTask.id).where(ScheduledTask.name == body.name))
    if exists:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Eine Aufgabe mit dem Namen {body.name!r} existiert bereits")

    now = _naive_utcnow()
    task = ScheduledTask(
        name=body.name,
        handler_key=body.handler_key,
        schedule_kind=body.schedule_kind,
        interval_seconds=body.interval_seconds if body.schedule_kind == SCHEDULE_KIND_INTERVAL else None,
        cron_expr=body.cron_expr if body.schedule_kind == SCHEDULE_KIND_CRON else None,
        params=dict(body.params or {}),
        enabled=body.enabled,
        run_at_boot=body.run_at_boot,
        start_at=body.start_at,
        end_at=body.end_at,
        is_builtin=False,
    )
    # First run: now if run_at_boot, else one interval / next cron out.
    task.next_run_at = now if body.run_at_boot else compute_next_run(task, after=now)
    db.add(task)
    await db.commit()
    await db.refresh(task)
    logger.info(f"scheduled task created: {task.name} (handler={task.handler_key})")
    return ScheduledTaskResponse.model_validate(task)


@router.patch("/{task_id}", response_model=ScheduledTaskResponse)
async def update_scheduled_task(
    task_id: int,
    body: UpdateScheduledTaskRequest,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_permission(Permission.ADMIN)),
) -> ScheduledTaskResponse:
    task = await db.get(ScheduledTask, task_id)
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Aufgabe nicht gefunden")

    data = body.model_dump(exclude_unset=True)

    # Resolve the effective schedule (new values fall back to current) and
    # re-validate it whenever any schedule field is touched.
    schedule_touched = any(k in data for k in ("schedule_kind", "interval_seconds", "cron_expr"))
    if schedule_touched:
        kind = data.get("schedule_kind", task.schedule_kind)
        interval = data.get("interval_seconds", task.interval_seconds)
        cron = data.get("cron_expr", task.cron_expr)
        _validate_schedule(kind, interval, cron)
        task.schedule_kind = kind
        task.interval_seconds = interval if kind == SCHEDULE_KIND_INTERVAL else None
        task.cron_expr = cron if kind == SCHEDULE_KIND_CRON else None

    if "params" in data:
        _validate_params(task.handler_key, data["params"])
        task.params = dict(data["params"] or {})
    if "enabled" in data:
        task.enabled = data["enabled"]
    if "run_at_boot" in data:
        task.run_at_boot = data["run_at_boot"]
    if "start_at" in data:
        task.start_at = data["start_at"]
    if "end_at" in data:
        task.end_at = data["end_at"]

    # A schedule change re-anchors next_run_at from now (else keep it).
    if schedule_touched:
        task.next_run_at = compute_next_run(task, after=_naive_utcnow())

    await db.commit()
    await db.refresh(task)
    logger.info(f"scheduled task updated: {task.name} ({sorted(data)})")
    return ScheduledTaskResponse.model_validate(task)


@router.post("/{task_id}/run-now", response_model=ScheduledTaskResponse)
async def run_scheduled_task_now(
    task_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_permission(Permission.ADMIN)),
) -> ScheduledTaskResponse:
    """Schedule the task to run on the next engine tick (sets next_run_at=now).
    Not an immediate execution — there's up to one engine tick of latency, and if
    a run is already in flight the advisory lock skips the duplicate."""
    task = await db.get(ScheduledTask, task_id)
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Aufgabe nicht gefunden")
    if not task.enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, "Deaktivierte Aufgabe kann nicht ausgeführt werden")
    # The engine's due-selection also gates on the [start_at, end_at] window, so
    # refuse run-now outside it — else we'd 200 with next_run_at=now for a run the
    # engine will never perform (a silent no-op that looks scheduled in the UI).
    now = _naive_utcnow()
    if task.start_at is not None and task.start_at > now:
        raise HTTPException(status.HTTP_409_CONFLICT, "Aufgabe ist noch nicht aktiv (Startzeitpunkt liegt in der Zukunft)")
    if task.end_at is not None and task.end_at < now:
        raise HTTPException(status.HTTP_409_CONFLICT, "Aufgabe ist nicht mehr aktiv (Endzeitpunkt überschritten)")
    task.next_run_at = now
    await db.commit()
    await db.refresh(task)
    return ScheduledTaskResponse.model_validate(task)


@router.delete("/{task_id}")
async def delete_scheduled_task(
    task_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_permission(Permission.ADMIN)),
) -> dict:
    task = await db.get(ScheduledTask, task_id)
    if task is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Aufgabe nicht gefunden")
    if task.is_builtin:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Eingebaute Aufgaben können nicht gelöscht werden (nur deaktivieren)",
        )
    name = task.name
    await db.delete(task)
    await db.commit()
    logger.info(f"scheduled task deleted: {name}")
    return {"success": True, "deleted": name}
