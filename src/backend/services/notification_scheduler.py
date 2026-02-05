"""
Notification Scheduler — Cron-based scheduled jobs (e.g. morning briefing)

Minimal cron parser (no croniter dependency). Supports:
  minute hour day_of_month month day_of_week
  with * (any) and integer values.

Background loop checks for due jobs at configurable intervals.
"""

import asyncio
from datetime import datetime, timedelta

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.database import ScheduledJob
from utils.config import settings


class NotificationScheduler:
    """Background scheduler for notification jobs."""

    def __init__(self):
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self):
        """Start the scheduler background loop."""
        if not settings.proactive_scheduler_enabled:
            logger.info("⏭️  Notification Scheduler deaktiviert")
            return

        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info(
            f"✅ Notification Scheduler gestartet "
            f"(interval={settings.proactive_scheduler_check_interval}s)"
        )

    async def stop(self):
        """Stop the scheduler."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self):
        """Main scheduler loop."""
        while self._running:
            try:
                await asyncio.sleep(settings.proactive_scheduler_check_interval)
                await self._check_due_jobs()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"⚠️ Scheduler loop error: {e}")

    async def _check_due_jobs(self):
        """Find and execute due jobs."""
        from services.database import AsyncSessionLocal

        now = datetime.utcnow()

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(ScheduledJob).where(
                    ScheduledJob.is_enabled.is_(True),
                    ScheduledJob.next_run_at <= now,
                )
            )
            jobs = list(result.scalars().all())

            for job in jobs:
                try:
                    await self._execute_job(job, db)

                    # Update timing
                    job.last_run_at = now
                    job.next_run_at = self.next_run_after(job.schedule_cron, now)
                    await db.commit()

                    logger.info(f"✅ Scheduled job '{job.name}' executed, next: {job.next_run_at}")
                except Exception as e:
                    logger.error(f"❌ Scheduled job '{job.name}' failed: {e}")

    async def _execute_job(self, job: ScheduledJob, db: AsyncSession):
        """Execute a single scheduled job."""
        if job.job_type == "briefing":
            await self._run_briefing(job, db)
        else:
            logger.warning(f"Unknown job type: {job.job_type}")

    async def _run_briefing(self, job: ScheduledJob, db: AsyncSession):
        """Generate a briefing notification via LLM and deliver it."""
        from services.notification_service import NotificationService

        # Build briefing prompt from config
        config = job.config or {}
        topics = config.get("topics", ["weather", "calendar", "news"])
        language = config.get("language", "de")

        try:
            from utils.llm_client import get_default_client

            client = get_default_client()
            prompt = (
                f"Generate a brief morning briefing in {'German' if language == 'de' else 'English'}. "
                f"Topics to cover: {', '.join(topics)}. "
                "Keep it concise (3-5 sentences). "
                "Start with a friendly greeting appropriate for the time of day."
            )
            response = await client.generate(
                model=settings.proactive_enrichment_model or settings.ollama_model,
                prompt=prompt,
                options={"temperature": 0.5, "num_predict": 300},
            )
            briefing_text = response.response.strip()
        except Exception as e:
            logger.warning(f"⚠️ Briefing generation failed, using fallback: {e}")
            briefing_text = "Guten Morgen! Das Briefing konnte heute nicht erstellt werden."

        # Deliver as notification
        service = NotificationService(db)
        room_name = None
        if job.room_id:
            from models.database import Room
            room_result = await db.execute(select(Room).where(Room.id == job.room_id))
            room = room_result.scalar_one_or_none()
            if room:
                room_name = room.name

        await service.process_webhook(
            event_type="scheduled.briefing",
            title=job.name,
            message=briefing_text,
            urgency="info",
            room=room_name,
            tts=True,
        )

    # ------------------------------------------------------------------
    # Minimal Cron Parser
    # ------------------------------------------------------------------

    @staticmethod
    def next_run_after(cron_expr: str, after_dt: datetime) -> datetime:
        """
        Compute the next run time after `after_dt` for a cron expression.

        Supports: minute hour day_of_month month day_of_week
        Each field can be '*' (any) or an integer.

        Raises ValueError on invalid expressions.
        """
        parts = cron_expr.strip().split()
        if len(parts) != 5:
            raise ValueError(
                f"Invalid cron expression '{cron_expr}': expected 5 fields "
                "(minute hour day_of_month month day_of_week)"
            )

        def parse_field(field: str, min_val: int, max_val: int) -> int | None:
            if field == "*":
                return None  # any
            try:
                val = int(field)
                if val < min_val or val > max_val:
                    raise ValueError(
                        f"Value {val} out of range [{min_val}-{max_val}]"
                    )
                return val
            except ValueError as e:
                if "out of range" in str(e):
                    raise
                raise ValueError(f"Invalid cron field '{field}'") from e

        cron_minute = parse_field(parts[0], 0, 59)
        cron_hour = parse_field(parts[1], 0, 23)
        cron_dom = parse_field(parts[2], 1, 31)
        cron_month = parse_field(parts[3], 1, 12)
        cron_dow = parse_field(parts[4], 0, 6)  # 0=Sunday

        # Start from next minute
        candidate = after_dt.replace(second=0, microsecond=0) + timedelta(minutes=1)

        # Search up to 366 days ahead
        max_iterations = 366 * 24 * 60  # worst case: check every minute for a year
        for _ in range(max_iterations):
            # Python weekday: 0=Monday, cron: 0=Sunday
            python_dow = (candidate.weekday() + 1) % 7

            matches = True
            if cron_month is not None and candidate.month != cron_month:
                matches = False
            if cron_dom is not None and candidate.day != cron_dom:
                matches = False
            if cron_dow is not None and python_dow != cron_dow:
                matches = False
            if cron_hour is not None and candidate.hour != cron_hour:
                matches = False
            if cron_minute is not None and candidate.minute != cron_minute:
                matches = False

            if matches:
                return candidate

            # Skip ahead efficiently
            if cron_hour is not None and candidate.hour != cron_hour:
                # Jump to the right hour
                candidate = candidate.replace(minute=0) + timedelta(hours=1)
            elif cron_minute is not None and candidate.minute != cron_minute:
                candidate += timedelta(minutes=1)
            else:
                candidate += timedelta(minutes=1)

        raise ValueError(f"Could not find next run for cron '{cron_expr}' within 366 days")
