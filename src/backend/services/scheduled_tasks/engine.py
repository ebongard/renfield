"""Scheduled Tasks engine (#1137, docs/design/scheduled-tasks.md).

A single loop (``run_engine_tick``, one ``_spawn_periodic_task`` in lifecycle)
that each tick selects enabled tasks due within their ``[start_at, end_at]``
window and **spawns each as its own asyncio.Task** (Review C1 — spawn, not
inline await, so a slow handler never stalls the others), bounded by a small
Semaphore (Review D1 — the pool also serves request traffic).

Single-flight is a per-task advisory lock on a DEDICATED connection
(``0x5354``, held for the handler's whole duration — Review H5) PLUS an
in-process ``_inflight`` set that keeps a still-running task from being
re-spawned on the next tick. The lock is the cross-pod correctness guarantee;
``_inflight`` just avoids same-pod spawn churn.

Boot pass (Review C2 / #678): every enabled ``run_at_boot`` task has its
``next_run_at`` forced to now at startup, regardless of the persisted value.

Unknown ``handler_key`` is a SKIP, not a crash or a perpetual-error spin
(Review D3/D4): record it once, back the row off, don't re-select it every tick.
"""
from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from loguru import logger
from sqlalchemy import or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from models.database import (
    SCHEDULE_KIND_CRON,
    SCHEDULED_TASK_STATUS_ERROR,
    SCHEDULED_TASK_STATUS_OK,
    SCHEDULED_TASK_STATUS_SKIPPED,
    ScheduledTask,
)
from services.scheduled_tasks.registry import get_handler
from utils.config import settings

# NOTE: AsyncSessionLocal + the engine are imported at CALL time inside each
# function (not bound at module import) so a test can monkeypatch
# services.database.AsyncSessionLocal / .engine onto a per-test engine — the
# same pattern the other reconcilers use (paperless_finalize_reconciler).

if TYPE_CHECKING:
    from fastapi import FastAPI

# Fixed advisory-lock namespace for scheduled-task single-flight ("ST"). Distinct
# from the existing namespaces (0x4B47 KG, 0x4F42/0x4F43/0x4F44 obligations,
# 0x5341 fact-override reindex). objid = the task id.
_SCHEDULED_TASK_LOCK_NS = 0x5354

# Back-off applied when a task's next_run_at can't be computed (bad cron / no
# interval) so an unschedulable row doesn't re-select every engine tick.
_UNSCHEDULABLE_BACKOFF_SECONDS = 3600

# In-process guard: task ids currently spawned (running or queued on the
# Semaphore). Prevents re-spawn churn while a task overruns its interval.
_inflight: set[int] = set()

# Handles to the spawned per-task runs, so graceful shutdown can cancel + await
# them (the tick loop is tracked by _spawn_periodic_task, but the runs it spawns
# are not — without this they'd be abandoned on pod recycle). See drain_running_tasks.
_running_tasks: set[asyncio.Task] = set()

# Concurrency bound. Constructed lazily so tests can reset it via reset_state().
_semaphore: asyncio.Semaphore | None = None


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(max(1, settings.scheduled_tasks_max_concurrent))
    return _semaphore


def reset_state() -> None:
    """Test hook — clear the in-flight set, running-task handles, and Semaphore."""
    global _semaphore
    _inflight.clear()
    _running_tasks.clear()
    _semaphore = None


async def drain_running_tasks(timeout: float = 10.0) -> None:
    """Cancel + await any in-flight spawned task runs on shutdown, so a pod
    recycle doesn't abandon a mid-flight handler ("Task was destroyed but it is
    pending"). The tick loop itself is cancelled by _cancel_startup_tasks; this
    drains the runs it spawned. Best-effort within ``timeout`` — a handler blocked
    past it is left to the loop teardown (the session-level advisory lock releases
    when its connection drops, so no lock leaks)."""
    tasks = [t for t in _running_tasks if not t.done()]
    if not tasks:
        return
    logger.info(f"scheduled tasks: draining {len(tasks)} in-flight run(s) on shutdown")
    for t in tasks:
        t.cancel()
    try:
        await asyncio.wait(tasks, timeout=timeout)
    except Exception as e:  # noqa: BLE001 — shutdown drain must never raise
        logger.warning(f"scheduled-tasks drain: {type(e).__name__}: {e}")


def _naive_utcnow() -> datetime:
    """Current UTC as a naive datetime (matches the DateTime columns)."""
    return datetime.now(UTC).replace(tzinfo=None)


def _cron_next(expr: str, after_naive_utc: datetime) -> datetime | None:
    """Next fire time strictly after ``after`` for a cron expression, evaluated in
    the configured local timezone (Review D2) and returned as naive UTC. Reuses
    the day/night service's tz resolution (``daypart_timezone`` → ha_glue → UTC).
    Returns None on a bad expression (the engine then backs the row off)."""
    try:
        from croniter import croniter

        from services.daypart_service import _resolve_tz

        tz = _resolve_tz()
        local_after = after_naive_utc.replace(tzinfo=UTC).astimezone(tz)
        nxt_local = croniter(expr, local_after).get_next(datetime)
        return nxt_local.astimezone(UTC).replace(tzinfo=None)
    except Exception as e:  # noqa: BLE001 — bad cron / missing dep must not crash the engine
        logger.warning(f"scheduled-task cron '{expr}' next-run failed: {type(e).__name__}: {e}")
        return None


def compute_next_run(task: ScheduledTask, *, after: datetime) -> datetime | None:
    """Compute the next run strictly after ``after``. None = unschedulable
    (bad cron / non-positive interval), which the caller backs off."""
    if task.schedule_kind == SCHEDULE_KIND_CRON and task.cron_expr:
        return _cron_next(task.cron_expr, after)
    secs = task.interval_seconds or 0
    if secs <= 0:
        return None
    return after + timedelta(seconds=secs)


def _validate_interval_floor(interval_seconds: int | None) -> None:
    """Reject an interval below the engine tick (Review M6). Sub-tick jobs stay on
    the legacy _spawn_periodic_task path — the engine can't fire faster than a tick."""
    tick = settings.scheduled_tasks_engine_tick_seconds
    if interval_seconds is not None and interval_seconds < tick:
        raise ValueError(
            f"interval_seconds={interval_seconds} is below the engine tick ({tick}s); "
            "sub-tick jobs are not supported by the scheduled-tasks engine"
        )


# ---------------------------------------------------------------------------
# Seeding + boot pass
# ---------------------------------------------------------------------------

async def ensure_builtin_tasks() -> int:
    """Create any missing built-in task rows — INSERT ... ON CONFLICT (name) DO
    NOTHING (Review M8): create-if-missing, NEVER clobber admin edits, race-safe
    across a rolling deploy's two pods. Returns the number of rows inserted."""
    from services.scheduled_tasks.builtins import builtin_task_seeds

    from services.database import AsyncSessionLocal

    seeds = builtin_task_seeds()
    if not seeds:
        return 0
    now = _naive_utcnow()
    inserted = 0
    async with AsyncSessionLocal() as session:
        dialect = session.bind.dialect.name if session.bind is not None else ""
        for seed in seeds:
            # Seed next_run_at so a non-boot task waits one interval before its
            # first run (matches the legacy sleep-then-work cadence); a run_at_boot
            # task is due now (and the boot pass re-forces it anyway).
            if seed.run_at_boot:
                first_run = now
            else:
                first_run = _seed_first_run(seed, now)
            row = {
                "name": seed.name,
                "handler_key": seed.handler_key,
                "schedule_kind": seed.schedule_kind,
                "interval_seconds": seed.interval_seconds,
                "cron_expr": seed.cron_expr,
                "params": dict(seed.params or {}),
                "enabled": seed.enabled,
                "run_at_boot": seed.run_at_boot,
                "next_run_at": first_run,
                "is_builtin": True,
            }
            if dialect == "postgresql":
                stmt = pg_insert(ScheduledTask).values(**row).on_conflict_do_nothing(
                    index_elements=["name"]
                )
                result = await session.execute(stmt)
                inserted += result.rowcount or 0
            else:
                exists = await session.scalar(
                    select(ScheduledTask.id).where(ScheduledTask.name == seed.name)
                )
                if not exists:
                    session.add(ScheduledTask(**row))
                    inserted += 1
        await session.commit()
    if inserted:
        logger.info(f"scheduled tasks: seeded {inserted} built-in task(s)")
    return inserted


def _seed_first_run(seed, now: datetime) -> datetime | None:
    if seed.schedule_kind == SCHEDULE_KIND_CRON and seed.cron_expr:
        return _cron_next(seed.cron_expr, now)
    secs = seed.interval_seconds or 0
    return now + timedelta(seconds=secs) if secs > 0 else now


async def force_run_at_boot_tasks() -> int:
    """Boot pass (#678 / Review C2): force ``next_run_at = now`` on every enabled
    ``run_at_boot`` task so a pod that recycles faster than a task's interval
    still fires it. Returns the number of rows forced."""
    from services.database import AsyncSessionLocal

    now = _naive_utcnow()
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(
            select(ScheduledTask).where(
                ScheduledTask.enabled.is_(True),
                ScheduledTask.run_at_boot.is_(True),
            )
        )).scalars().all()
        for task in rows:
            task.next_run_at = now
        await session.commit()
        forced = len(rows)
    if forced:
        logger.info(f"scheduled tasks: boot-forced {forced} run_at_boot task(s)")
    return forced


# ---------------------------------------------------------------------------
# Tick + run
# ---------------------------------------------------------------------------

async def run_engine_tick(app: "FastAPI") -> None:
    """One engine tick: select due tasks and spawn each under the Semaphore. Does
    NOT await the spawned runs (Review C1)."""
    from services.database import AsyncSessionLocal

    now = _naive_utcnow()
    async with AsyncSessionLocal() as session:
        stmt = select(ScheduledTask.id).where(
            ScheduledTask.enabled.is_(True),
            or_(ScheduledTask.next_run_at.is_(None), ScheduledTask.next_run_at <= now),
            or_(ScheduledTask.start_at.is_(None), ScheduledTask.start_at <= now),
            or_(ScheduledTask.end_at.is_(None), ScheduledTask.end_at >= now),
        )
        due_ids = list((await session.execute(stmt)).scalars().all())

    for task_id in due_ids:
        if task_id in _inflight:
            continue  # still running from a prior tick — the lock would skip it anyway
        _inflight.add(task_id)
        t = asyncio.create_task(_bounded_run(app, task_id))
        _running_tasks.add(t)
        t.add_done_callback(_running_tasks.discard)


async def _bounded_run(app: "FastAPI", task_id: int) -> None:
    sem = _get_semaphore()
    try:
        async with sem:
            await _run_one(app, task_id)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 — a runner crash must not kill the engine
        logger.warning(f"scheduled-task runner (id={task_id}) crashed: {type(e).__name__}: {e}")
    finally:
        _inflight.discard(task_id)


async def _run_one(app: "FastAPI", task_id: int) -> None:
    """Acquire the per-task advisory lock (skip if held), then execute the task on
    a dedicated lock connection held for the handler's whole duration."""
    from services.database import engine as app_engine

    dialect = app_engine.dialect.name
    if dialect != "postgresql":
        # sqlite test shim / unknown: no advisory locks — _inflight already
        # prevents same-process double-run, which is all a single test engine needs.
        await _execute_task(app, task_id)
        return

    async with app_engine.connect() as lock_conn:
        got = (await lock_conn.execute(
            text("SELECT pg_try_advisory_lock(:ns, :oid)"),
            {"ns": _SCHEDULED_TASK_LOCK_NS, "oid": task_id},
        )).scalar()
        if not got:
            return  # another runner holds this task's lock — skip silently
        try:
            await _execute_task(app, task_id)
        finally:
            await lock_conn.execute(
                text("SELECT pg_advisory_unlock(:ns, :oid)"),
                {"ns": _SCHEDULED_TASK_LOCK_NS, "oid": task_id},
            )


async def _execute_task(app: "FastAPI", task_id: int) -> None:
    from services.database import AsyncSessionLocal

    started = time.monotonic()
    async with AsyncSessionLocal() as session:
        task = await session.get(ScheduledTask, task_id)
        if task is None or not task.enabled:
            return

        spec = get_handler(task.handler_key)
        now = _naive_utcnow()

        if spec is None:
            # Unknown handler_key (Review D3/D4): record once + back off, so a
            # removed handler or a mid-rollout ordering gap doesn't error every tick.
            task.last_run_at = now
            task.last_status = SCHEDULED_TASK_STATUS_SKIPPED
            task.last_error = f"unknown handler_key: {task.handler_key}"
            task.next_run_at = compute_next_run(task, after=now) or (
                now + timedelta(seconds=_UNSCHEDULABLE_BACKOFF_SECONDS)
            )
            await session.commit()
            logger.warning(
                f"scheduled task '{task.name}': unknown handler_key "
                f"'{task.handler_key}' — skipped + backed off"
            )
            return

        prev_status = task.last_status
        params = dict(task.params or {})
        status = SCHEDULED_TASK_STATUS_OK
        err: str | None = None
        detail: str | None = None
        try:
            detail = await spec.handler(app, params)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — a handler failure is recorded, not fatal
            status = SCHEDULED_TASK_STATUS_ERROR
            err = f"{type(e).__name__}: {e}"
            logger.warning(f"scheduled task '{task.name}' failed: {err}")

        end = _naive_utcnow()
        task.last_run_at = end
        task.last_status = status
        task.last_error = err
        task.last_duration_ms = int((time.monotonic() - started) * 1000)
        task.next_run_at = compute_next_run(task, after=end) or (
            end + timedelta(seconds=_UNSCHEDULABLE_BACKOFF_SECONDS)
        )
        await session.commit()

        # Status-transition observability (Review D5): the last_* columns keep only
        # the most recent run, so surface a transition so an intermittently-failing
        # task is visible without opening the UI.
        if status != prev_status:
            logger.info(
                f"scheduled task '{task.name}' status {prev_status or 'none'} -> {status}"
                + (f" ({detail})" if detail else "")
                + (f": {err}" if err else "")
            )
